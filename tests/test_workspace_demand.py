import os
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.demand import next_video_task, relevant_rows
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.storage import read_json
from cowmata_tailring.workspace.work import SessionWork
from cowmata_tailring.workspace.worker import IndexWorker


def video(i, batch="view/batch", start=None, end=None):
    return {"path": f"{batch}/{i:04}.mp4", "kind": "video", "asset_id": str(i).zfill(64) if start is not None else None,
            "state": "ready" if start is not None else "pending", "stamp": "test", "metadata": {
                "camera": batch.split("/")[0], "intervals": [] if start is None else [
                    {"wall_start": start, "wall_end": end, "media_start": 0, "media_end": end-start}]}}


def test_fast_scan_reads_no_media_and_hash_identity_is_still_full(tmp_path, monkeypatch):
    cat = Catalog(tmp_path, stability_seconds=0)
    for i in range(50):
        (tmp_path / f"{i}.mp4").write_bytes(str(i).encode())
    import cowmata_tailring.workspace.catalog as module
    real = module.digest_file
    monkeypatch.setattr(module, "digest_file", lambda *_a, **_kw: pytest.fail("scan read file content"))
    assert cat.scan(fast=True).inspected == 50
    monkeypatch.setattr(module, "digest_file", real)
    row = cat.index_one("1.mp4", lambda *_: {"intervals": [], "needs_review": True})
    assert row["asset_id"] == real(tmp_path / "1.mp4")
    assert len(cat.pending()) == 49
    cat.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-mode probe")
def test_eager_current_file_keeps_write_handle_and_post_read_guards(tmp_path):
    cat = Catalog(tmp_path, stability_seconds=999)
    path = tmp_path / "one.json"
    path.write_bytes(b"fixture")
    cat.scan(fast=True)
    assert not cat.pending() and cat.pending(eager=True)
    with path.open("r+b"):
        assert cat.index_one(path.name, lambda *_: pytest.fail("reading a busy source"), eager=True) is None
    assert cat.rows()[0]["state"] == "pending"
    held = []
    def start_writing(*_):
        held.append(path.open("r+b"))
        return {}
    try:
        assert cat.index_one(path.name, start_writing, eager=True) is None
        assert cat.rows()[0]["state"] == "pending"
    finally:
        for stream in held:
            stream.close()
    assert cat.index_one(path.name, lambda *_: {"fixture":True}, eager=True)["state"] == "ready"
    cat.close()


def test_hint_never_becomes_evidence_and_replacement_invalidates_it(tmp_path):
    cat = Catalog(tmp_path, stability_seconds=0)
    path = tmp_path / "001.mp4"
    path.write_bytes(b"old")
    cat.scan(fast=True)
    old = cat.rows()[0]
    assert cat.save_video_hint(old["path"], old["stamp"], {"start_ms": 1000})
    assert cat.video_hints()["001.mp4"]["start_ms"] == 1000
    assert cat.rows()[0]["asset_id"] is None
    path.write_bytes(b"replacement")
    cat.scan(fast=True)
    assert cat.video_hints() == {}
    assert not cat.save_video_hint(old["path"], old["stamp"], {"start_ms": 1000})
    cat.close()


def test_idle_worker_does_not_index_entire_project(tmp_path):
    cat = Catalog(tmp_path, stability_seconds=0)
    (tmp_path / "001.mp4").write_bytes(b"not read")
    (tmp_path / "a.json").write_text("{}")
    cat.scan(fast=True)
    worker = IndexWorker(cat)
    pending = cat.pending()
    assert worker.next_task(pending) is None
    worker.focus_path = "a.json"
    assert worker.next_task(pending)[1]["path"] == "a.json"
    cat.close()


def test_partial_overlap_previous_next_midnight_and_camera_offset():
    rows = [video(0, start=0, end=2000), video(1, start=2000, end=4000), video(2, start=4000, end=8000),
            video(3, start=8001, end=9000)]
    assert [r["path"] for r in relevant_rows(rows, 1000, 6000)] == [r["path"] for r in rows[:3]]
    mapping = ClockMap([Anchor(0, -10000), Anchor(10000, 0)])
    assert len(relevant_rows(rows, -9000, -4000, {"view": mapping})) == 3
    assert not relevant_rows(rows, -9000, -4000)


def test_sparse_search_finds_boundary_clips_without_full_scanning_1000():
    rows = [video(i) for i in range(1000)]
    hints, full, attempts = {}, [], set()
    target = (500 * 60000 + 15000, 560 * 60000 + 15000)
    discoveries = 0
    for _ in range(260):
        task = next_video_task(rows, hints, *target, attempted=attempts)
        if task is None:
            break
        mode, row = task
        number = int(row["path"].rsplit("/", 1)[-1][:-4])
        start = number * 60000
        if mode == "hint":
            hints[row["path"]] = {"start_ms": start, "end_ms": None}
            discoveries += 1
        else:
            full.append(number)
            row.update(state="ready", asset_id=str(number).zfill(64),
                       metadata={"camera": "view", "intervals": [{"wall_start": start, "wall_end": start + 60000,
                           "media_start": 0, "media_end": 60000}]})
        if all(i in full for i in range(500, 561)):
            break
    assert all(i in full for i in range(500, 561))
    assert discoveries < 100
    assert len(full) < 70


def test_counter_reset_and_unknown_order_are_not_assumed_no_video():
    rows = [video(i, batch=batch) for batch in ("A/first", "A/reset", "B/batch") for i in range(4)]
    hints = {r["path"]: {"start_ms": None, "reason": "OCR failed"} for r in rows}
    task = next_video_task(rows, hints, 100, 200)
    assert task[0] == "full"
    assert task[1]["path"] == rows[0]["path"]
    assert next_video_task(rows, hints, 100, 200, explore=False) is None
    assert all(r["state"] == "pending" and r["asset_id"] is None for r in rows)
    from cowmata_tailring.workspace.clocks import VideoTimeline
    from cowmata_tailring.workspace.coverage import video_coverage
    assert video_coverage(VideoTimeline([]), rows, 150, [], scan_complete=True, aligned=True).code == "indexing"


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.board.timer.stop()
    w.source_timer.stop()
    w.save_timer.stop()
    w.catalog = Catalog(tmp_path, stability_seconds=0)
    yield w
    w.close()
    w.catalog.close()
    app.processEvents()


def imu(path, asset="a", state="ready"):
    return {"path": path, "kind": "imu", "asset_id": asset*64 if state == "ready" else None,
            "state": state, "stamp": "test", "metadata": {"device": "D", "duration_ms": 10000}}


def test_restore_skips_done_and_does_not_configure_all_video(window, monkeypatch):
    first, second = imu("one.json"), imu("two.json", "b", "pending")
    window.rows = [first, second, video(1, start=0, end=10000)]
    window.settings = {"current_path": "one.json", "current_asset": first["asset_id"],
                       "review_progress": {"one.json": {"stamp": "test", "status": "done"}}}
    calls = []
    monkeypatch.setattr(window, "select_record", lambda item, *_: calls.append(item.data(Qt.ItemDataRole.UserRole)["path"]))
    # Signal was connected at construction; the pending branch only queues a
    # request and never opens videos or decodes records.
    window.refresh_lists()
    assert window.records.currentItem().data(Qt.ItemDataRole.UserRole)["path"] == "two.json"
    assert not window.board.timeline.intervals
    assert not window.board.selected
    window.rows[0]["stamp"] = "replacement"
    assert window.record_status(window.rows[0]) == "new"


def test_save_progress_is_only_in_project_metadata(window):
    row = imu("nine.json")
    window.current_row = row
    window.current_stamp = "test"
    window.work = SessionWork(row["asset_id"])
    window.imu_ms = 4321
    window.work.progress["status"] = "in_progress"
    window.save_current()
    saved = read_json(window.catalog.meta / "project.json", {})
    assert saved["review_progress"]["nine.json"]["imu_ms"] == 4321
    assert saved["review_progress"]["nine.json"]["status"] == "in_progress"
    assert read_json(window.catalog.work_path(row["asset_id"]), {})["progress"]["imu_ms"] == 4321
    assert sorted(p.name for p in window.catalog.root.iterdir()) == ["标注工程"]


def test_native_menu_bar_tooltips_checkmark_and_update_location(window):
    titles = [a.text().split("(")[0] for a in window.menuBar().actions()]
    assert titles == ["文件", "编辑", "视图", "工具", "数据整理", "数据集构建", "行为识别", "健康与繁殖", "帮助"]
    assert not window.menuBar().isHidden()
    assert window.banner.isHidden() and window.alignment_label.isHidden()
    assert "check_visible.svg" in window.styleSheet()
    from cowmata_tailring.app.update_ui import UpdateController
    window.updater = UpdateController(window, automatic=False)
    assert window.updater.button.isHidden()


def test_completion_is_explicit_and_drafts_are_not_promoted(window, monkeypatch):
    row = imu("record.json")
    window.current_row = row
    window.current_stamp = "test"
    window.work = SessionWork(row["asset_id"])
    window.source_available = True
    window.work.add_draft(0, 100, 200, [])
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: None)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda dialog: next(b for b in dialog.buttons() if b.text() == "保存并完成，下一份"))
    monkeypatch.setattr(window, "next_record", lambda: None)
    window.finish_record()
    assert window.work.progress["status"] == "done"
    assert not window.work.project.events
    assert len(window.work.drafts) == 1


def test_completed_old_callbacks_cannot_reopen_database(window, monkeypatch):
    window._closed = True
    monkeypatch.setattr(window.catalog, "rows", lambda: pytest.fail("closed callback read DB"))
    window.scan_completed(None)
    monkeypatch.setattr(window, "finish_record", lambda: pytest.fail("stale completion prompt"))
    window.work = SessionWork("a"*64)
    window.prompt_record_end(window.load_generation, window.work.asset_id)
    window._closed = False
    window.prompt_record_end(window.load_generation-1, window.work.asset_id)
    window.work = None


def test_guided_search_continues_after_exploration_budget():
    rows = [video(i) for i in range(100)]
    hints = {rows[0]["path"]: {"start_ms": 0}, rows[-1]["path"]: {"start_ms": 99000}}
    task = next_video_task(rows, hints, 40000, 60000, explore=False)
    assert task[0] == "hint" and task[1]["_guided"]
    assert 0 < int(task[1]["path"].rsplit("/",1)[1][:-4]) < 99


def test_routing_single_vote_is_explicitly_not_verified():
    from PIL import Image

    from cowmata_tailring.workspace.ocr import TimestampOCR
    ocr = TimestampOCR.__new__(TimestampOCR)
    calls = []
    def engine(*_args, **_kwargs):
        calls.append(1)
        return [[[[2,2],[100,2],[100,14],[2,14]], "2026-08-03 12:34:56", .99]], None
    ocr.engine = engine
    result = ocr.routing_read(Image.new("RGB", (640,360), "white"))
    assert result["routing_only"] and result["success"] and len(calls) == 1
    assert "verified" not in result and "intervals" not in result


def test_cheap_routing_checks_every_corner_with_bounded_inference():
    from PIL import Image

    from cowmata_tailring.workspace.ocr import TimestampOCR
    ocr = TimestampOCR.__new__(TimestampOCR)
    calls = []
    def engine(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 4:
            return [[[[2, 2], [200, 2], [200, 20], [2, 20]], "2026-09-01 07:41:21", .99]], None
        return [], None
    ocr.engine = engine
    result = ocr.routing_read(Image.new("RGB", (640, 360)), raw_only=True)
    assert result["success"] and result["routing_only"]
    assert result["roi"][0] >= .2 and result["roi"][1] >= .7  # detection includes a padded margin
    assert len(calls) == 4


def test_routing_cache_is_rechecked_by_full_recognizer(tmp_path, monkeypatch):
    from PIL import Image

    import cowmata_tailring.workspace.probe as probe
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.catalog import file_stamp
    path = tmp_path/"v.mp4"
    path.write_bytes(b"synthetic video")
    meta = tmp_path/"meta"
    meta.mkdir()
    inspect = probe.SourceInspector(tmp_path, meta)
    frame = Image.new("RGB", (640,360))
    bad_hint = {"routing_only":True,"success":True,"wall_ms":99900000,"roi":[0,0,.8,.2]}
    inspect.opening_cache = {"path":path,"stamp":file_stamp(path),"frames":{0:(frame,0,bad_hint),1200:(frame,1200,bad_hint)}}
    calls = []
    def strong(_frame, **kwargs):
        target = float(kwargs["filename"].split("@")[1][:-2])
        calls.append(target)
        return {"success":True,"wall_ms":100000+target,"roi":[0,0,.8,.2],"metadata":{}}
    inspect.ocr = SimpleNamespace(recognize=strong, signature="test")
    timeline = MediaTimelineIndex("synthetic",1,0,0,100,(TimelineSegment(0,10000,0,10000),),())
    monkeypatch.setattr(probe,"probe_media",lambda _, **kw:{"streams":[{"codec_type":"video","width":640,"height":360}]})
    monkeypatch.setattr(probe,"find_ffmpeg",lambda:("ffmpeg","ffprobe"))
    monkeypatch.setattr(probe,"probe_media_timeline",lambda *_a,**_kw:timeline)
    monkeypatch.setattr(probe,"extract_frame",lambda _p,target,*_a,**_kw:(frame,target))
    result = inspect.video(path,"a"*64)
    assert 0 in calls and 1200 in calls
    assert all(s["wall_start"] < 200000 for s in result["intervals"])


def test_ocr_cancels_between_inference_calls():
    from cowmata_tailring.workspace.rapid_backend import RapidV6Adapter
    adapter = RapidV6Adapter.__new__(RapidV6Adapter)
    adapter.cancelled = lambda: True
    adapter.engine = lambda *_a,**_kw: pytest.fail("cancelled OCR executed")
    with pytest.raises(InterruptedError):
        adapter(None)


def test_active_clip_search_is_not_starved_by_playback(tmp_path):
    cat = Catalog(tmp_path)
    worker = IndexWorker(cat)
    worker.playback_busy.set()
    task = ("full", video(1))
    assert not worker.may_run(task)
    worker.window = (10000, 20000, {})
    assert worker.may_run(task)
    assert worker.may_run(("hint", {**video(1), "_guided":True}))
    assert not worker.may_run(("hint", video(1)))
    cat.close()


def test_play_before_first_video_does_not_stop_search_or_advance_past_record(window):
    worker = IndexWorker(window.catalog)
    window.worker = worker
    worker.window = (1000, 10000, {})
    window.board.reference_ms = 1000
    window.board.play(True)
    assert worker.may_run(("hint", video(1)))
    assert not window.board.playing
    assert window.board.reference_ms == 1000


def test_device_export_suffix_keeps_unread_records_visible(window):
    prefix = "数据工程/九轴/0C3D5EA22E36-20225N3/2026-09-01/motion/"
    first = imu(prefix + "2026-09-01_07_41_21.json")
    first["metadata"]["device"] = "0C3D5EA22E36"
    second = {**imu(prefix + "2026-09-01_08_49_11.json", state="pending"), "metadata": {}}
    window.rows = [first, second]
    window.current_row = first
    window.work = SessionWork(first["asset_id"])
    window.source_available = True
    window.refresh_lists()
    assert window.devices.count() == 1
    assert window.devices.currentData() == "0C3D5EA22E36"
    assert window.records.count() == 2


def test_old_short_device_folder_keeps_all_records_after_first_decode(window):
    prefix = "数据工程/九轴/07D5-21014-R/2026-09-01/motion/"
    first = imu(prefix + "2026-09-01_07_41_21.json")
    first["metadata"]["device"] = "546C50CA07D5"
    second = {**imu(prefix + "2026-09-01_08_49_11.json", state="pending"), "metadata": {}}
    window.rows = [first, second]
    window.current_row = first
    window.work = SessionWork(first["asset_id"])
    window.source_available = True
    window.refresh_lists()
    assert window.devices.count() == 1
    assert window.records.count() == 2
    assert second["metadata"] == {}  # grouping is not an inferred data identity


@pytest.mark.parametrize("folder,metadata,want", [
    ("0c3d5ea22e36_20225N3", {}, "0C3D5EA22E36"),
    ("0C3D5EA22E36-20225N3", {"device": "AABBCCDDEEFF"}, "AABBCCDDEEFF"),
    ("牛-01", {}, "牛-01"),
])
def test_device_folder_normalization_does_not_override_real_identity(folder, metadata, want):
    from cowmata_tailring.workspace.demand import device_name
    assert device_name({"path": f"九轴/{folder}/day/motion/a.json", "metadata": metadata}) == want


def test_old_project_intervals_route_recheck_without_becoming_playable(tmp_path):
    from cowmata_tailring.workspace.clocks import intervals_from_rows
    cat = Catalog(tmp_path, stability_seconds=0)
    try:
        (tmp_path / "old.mp4").write_bytes(b"unchanged old video")
        cat.scan(fast=True)
        cat.index_one("old.mp4", lambda *_: {"ocr_engine": "legacy", "camera": "view", "intervals": [
            {"wall_start": 1000, "wall_end": 10000, "media_start": 0, "media_end": 9000}]})
        assert cat.queue_ocr_upgrade("current") == 1
        worker = IndexWorker(cat)
        worker.window = (2000, 4000, {})
        assert not intervals_from_rows(cat.rows())
        mode, row = worker.next_task(cat.pending(eager=True))
        assert mode == "full" and row["path"] == "old.mp4"
        (tmp_path / "old.mp4").write_bytes(b"replaced source recording")
        cat.scan(fast=True)
        assert worker.next_task(cat.pending(eager=True))[0] == "native"
    finally:
        cat.close()


def test_old_video_recheck_prepares_bounded_opening_cache_before_full_ocr(tmp_path, monkeypatch):
    import cowmata_tailring.workspace.worker as module
    cat = Catalog(tmp_path, stability_seconds=0)
    try:
        (tmp_path / "old.mp4").write_bytes(b"source")
        cat.scan(fast=True)
        cat.index_one("old.mp4", lambda *_: {"ocr_engine": "legacy", "intervals": [
            {"wall_start": 1000, "wall_end": 10000, "media_start": 0, "media_end": 9000}]})
        worker = IndexWorker(cat)
        worker.window = (2000, 4000, {})
        class Inspector:
            def __init__(self, *_):
                self.prepared = False
            def video_hint(self, path):
                self.prepared = True
            def __call__(self, *_):
                assert self.prepared, "legacy full OCR missed cheap later-frame discovery"
                worker.stop.set()
                return {"intervals": []}
        monkeypatch.setattr(module, "SourceInspector", Inspector)
        worker.start()
        worker.thread.join(3)
        assert not worker.thread.is_alive()
        assert cat.rows()[0]["state"] == "ready"
    finally:
        worker.cancel()
        worker.thread.join(3)
        cat.close()


def test_failed_video_opening_falls_back_once_and_yields_to_playback(tmp_path):
    cat = Catalog(tmp_path, stability_seconds=0)
    try:
        (tmp_path / "bad-opening.mp4").write_bytes(b"synthetic source")
        cat.scan(fast=True)
        row = cat.rows()[0]
        cat.save_video_hint(row["path"], row["stamp"], {"start_ms": None, "hint_only": True, "reason": "首帧无时间"})
        worker = IndexWorker(cat)
        worker.window = (1000, 2000, {})
        task = worker.next_task(cat.pending(eager=True))
        assert task is not None and task[0] == "full"
        worker.playback_busy.set()
        assert not worker.may_run(task)
        worker.playback_busy.clear()
        assert worker.may_run(task)
        worker.attempted.add(row["path"])
        assert worker.next_task(cat.pending(eager=True)) is None
        worker.attempted.clear()
        worker.hints_used = worker.budget
        assert worker.next_task(cat.pending(eager=True)) is None
    finally:
        cat.close()


def test_replacing_failed_video_allows_retry_in_same_open_project(tmp_path, monkeypatch):
    import time

    import cowmata_tailring.workspace.worker as module
    cat = Catalog(tmp_path, stability_seconds=0)
    worker = IndexWorker(cat)
    path = tmp_path / "copy.mp4"
    path.write_bytes(b"unfinished")
    cat.scan(fast=True)
    row = cat.rows()[0]
    cat.save_video_hint(row["path"], row["stamp"], {"start_ms": 1000, "end_ms": 4000})
    class Inspector:
        def __init__(self, *_):
            self.timezone_minutes = 480
        def __call__(self, source, *_):
            if source.read_bytes() == b"unfinished":
                raise ValueError("incomplete recording")
            worker.stop.set()
            return {"intervals": []}
    monkeypatch.setattr(module, "SourceInspector", Inspector)
    worker.window = (1500, 3000, {})
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while path.name not in worker.attempted and time.monotonic() < deadline:
            time.sleep(.01)
        assert path.name in worker.attempted
        path.write_bytes(b"completed replacement recording")
        cat.scan(fast=True)
        row = cat.rows()[0]
        cat.save_video_hint(row["path"], row["stamp"], {"start_ms": 1000, "end_ms": 4000})
        worker.request("scan")
        worker.thread.join(3)
        assert cat.rows()[0]["state"] == "ready"
    finally:
        worker.cancel()
        worker.thread.join(3)
        cat.close()


def test_source_inspection_dialog_refreshes_completed_background_index(window, monkeypatch):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog, QTableWidget
    row = video(1)
    row["error"] = "等待文件稳定及可读性检查"
    window.rows = [row]
    def inspect(dialog):
        table = dialog.findChild(QTableWidget)
        table.selectRow(0)
        before = table.item(0, 2).text()
        window.rows = [{**row, "state": "ready", "error": "", "metadata": {"start_display": "2026-09-01 07_41_21"}}]
        QTest.qWait(650)
        assert table.item(0, 2).text() != before
        assert table.item(0, 3).text() == "2026-09-01 07_41_21"
        assert table.currentRow() == 0
    monkeypatch.setattr(QDialog, "exec", inspect)
    window.source_manager()


def test_opening_without_osd_uses_later_routing_frames_before_expensive_verification(tmp_path, monkeypatch):
    from PIL import Image

    import cowmata_tailring.workspace.probe as probe
    path = tmp_path / "late-osd.mp4"
    path.write_bytes(b"fixture")
    inspector = probe.SourceInspector(tmp_path, tmp_path)
    targets = []
    def frame(_path, target, **_kwargs):
        targets.append(target)
        return Image.new("RGB", (100, 100)), target
    def route(_frame, **_kwargs):
        target = targets[-1]
        return {"success": target >= 2500, "wall_ms": 100000 + target if target >= 2500 else None,
                "roi": [0, .8, 1, 1] if target >= 2500 else None, "routing_only": True}
    inspector.ocr = SimpleNamespace(routing_read=route,
        recognize=lambda *_a, **_kw: pytest.fail("full OCR in cheap routing stage"))
    monkeypatch.setattr(probe, "native_hint", lambda *_a, **_kw: None)
    monkeypatch.setattr(probe, "extract_frame", frame)
    hint = inspector.video_hint(path)
    assert hint["start_ms"] == 100000
    assert len(hint["observations"]) == 2
    assert max(targets) > 2500
    assert "intervals" not in hint


def test_late_routing_observations_are_independently_verified_and_blank_edges_stay_tentative(tmp_path, monkeypatch):
    from PIL import Image

    import cowmata_tailring.workspace.probe as probe
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.catalog import file_stamp
    path = tmp_path / "late.mp4"
    path.write_bytes(b"source")
    inspector = probe.SourceInspector(tmp_path, tmp_path)
    frame = Image.new("RGB", (640, 360))
    frames = {t: (frame, t, {"routing_only": True, "success": t >= 2500,
              "wall_ms": 999000+t if t >= 2500 else None, "roi": [0, .8, 1, 1]})
              for t in (0, 1200, 2500, 5000)}
    inspector.opening_cache = {"path": path, "stamp": file_stamp(path), "frames": frames}
    calls = []
    def recognize(_frame, **kwargs):
        target = float(kwargs["filename"].split("@")[1][:-2])
        calls.append(target)
        assert target >= 2500, "blank opening unnecessarily repeats expensive OCR"
        return {"success": True, "wall_ms": 100000+target, "roi": [0, .8, 1, 1], "metadata": {}}
    inspector.ocr = SimpleNamespace(recognize=recognize, signature="test")
    timeline = MediaTimelineIndex("synthetic", 1, 0, 0, 100, (TimelineSegment(0, 10000, 0, 10000),), ())
    monkeypatch.setattr(probe, "probe_media", lambda *_a, **_kw: {"streams": [{"codec_type": "video", "width": 640, "height": 360}]})
    monkeypatch.setattr(probe, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(probe, "probe_media_timeline", lambda *_a, **_kw: timeline)
    monkeypatch.setattr(probe, "extract_frame", lambda _p, t, *_a, **_kw: (frame, t))
    result = inspector.video(path, "b"*64)
    assert 2500 in calls and 5000 in calls
    assert result["intervals"]
    assert all(s["wall_start"] < 200000 for s in result["intervals"])
    assert not any(s["verified"] and s["media_start"] < 2500 for s in result["intervals"])
