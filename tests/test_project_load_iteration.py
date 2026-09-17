import base64
import struct
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QMessageBox

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import VideoTimeline, intervals_from_rows
from cowmata_tailring.workspace.demand import camera_name
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.playback import VideoBoard, VideoTile
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork
from cowmata_tailring.workspace.worker import IndexWorker

REAL_CLOSE_PROMPT = MainWindow.confirm_close


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert condition()


def test_structured_views_are_stable_across_resolution_and_separate_cards():
    def row(camera, resolution):
        return {"path": f"数据工程/{camera}/batch/mb00000.mp4", "asset_id": "a"*64,
                "metadata": {"camera": f"数据工程 · {resolution} · bottom_right"}}
    assert camera_name(row("视角02_神眸", "1920×1080")) == "视角02_神眸"
    assert camera_name(row("视角03_神眸", "1920×1080")) == "视角03_神眸"
    assert camera_name(row("视角02_神眸", "2560×1440")) == "视角02_神眸"
    assert camera_name(row("视角02_神眸", "2560×1440"), {"a"*64: "人工视角"}) == "人工视角"


def test_failed_new_load_cleans_only_owned_outputs_and_retains_sources(tmp_path):
    source = tmp_path / "one.mp4"
    source.write_bytes(b"original recording")
    cat = Catalog(tmp_path, load_session=True)
    cat.scan(fast=True)
    (cat.meta / "previews").mkdir()
    (cat.meta / "previews" / ("a"*64 + ".jpg")).write_bytes(b"generated preview")
    cat.close()
    assert not cat.meta.exists()
    assert source.read_bytes() == b"original recording"


def test_completed_or_human_work_is_not_removed(tmp_path):
    cat = Catalog(tmp_path, load_session=True)
    atomic_json(cat.work_path("b"*64), {"drafts": ["human work"]})
    cat.close()
    assert cat.work_path("b"*64).exists()
    reopened = Catalog(tmp_path, load_session=True)
    reopened.finish_load()
    reopened.close()
    assert (reopened.meta / "index.sqlite").exists()
    assert not (reopened.meta / ".load-pending.json").exists()


def test_crashed_load_marker_is_recovered_without_removing_unknown_files(tmp_path):
    cat = Catalog(tmp_path, load_session=True)
    meta = cat.meta
    (meta / "notes.txt").write_text("user notes")
    # Emulate process death after closing OS resources, without cleanup.
    cat.db.close()
    cat.lock.close()
    retry = Catalog(tmp_path, load_session=True)
    retry.close()
    assert (meta / "notes.txt").read_text() == "user notes"
    assert not (meta / "index.sqlite").exists()


def test_scan_cancel_does_not_mark_existing_sources_missing(tmp_path):
    cat = Catalog(tmp_path)
    (tmp_path / "one.mp4").write_bytes(b"source")
    cat.scan(fast=True)
    before = cat.rows()
    with pytest.raises(InterruptedError):
        cat.scan(cancelled=lambda: True)
    assert cat.rows() == before
    cat.close()


def test_stale_saved_views_fall_back_to_playable_camera(tmp_path, app, monkeypatch):
    cat = Catalog(tmp_path, stability_seconds=0)
    (tmp_path / "a.mp4").write_bytes(b"fixture")
    cat.scan()
    cat.index_one("a.mp4", lambda *_: {"camera": "new-view", "intervals": [
        {"wall_start": 1000, "wall_end": 2000, "media_start": 0, "media_end": 1000}]})
    window = MainWindow()
    window.catalog = cat
    window.settings = {"selected_cameras": ["old-view"]}
    window.rows = cat.rows()
    window.board.reference_ms = 1200
    monkeypatch.setattr(window, "refresh_records", lambda: None)
    monkeypatch.setattr(window, "window_videos", lambda: window.rows)
    monkeypatch.setattr(window.board, "_request", lambda *_a, **_kw: None)
    window.refresh_lists()
    assert window.board.selected == ["new-view"]
    window.cameras.item(0).setCheckState(Qt.CheckState.Unchecked)
    window.refresh_lists()
    assert window.board.selected == []  # Explicit user deselection is respected.
    window.close()
    pump(app, lambda: window._closed)


def test_close_during_scan_is_responsive_and_cleans_pending_load(tmp_path, app, monkeypatch):
    cat = Catalog(tmp_path, load_session=True)
    window = MainWindow()
    window.catalog = cat
    worker = IndexWorker(cat)
    window.worker = worker
    entered = threading.Event()
    def scan(**kw):
        entered.set()
        while not kw['cancelled']():
            time.sleep(.01)
        raise InterruptedError("cancelled")
    monkeypatch.setattr(cat, "scan", scan)
    worker.start()
    assert entered.wait(2)
    started = time.monotonic()
    window.close()
    assert time.monotonic() - started < .2
    pump(app, lambda: window._closed)
    assert not worker.thread.is_alive()
    assert not cat.meta.exists()


def test_seek_slider_commits_only_release_and_follows_actual_position(app):
    tile = VideoTile()
    commands = []
    tile.transportRequested.connect(lambda *args: commands.append(args[1:]))
    tile.update_seek(10000, 20000, 12000)
    assert tile.seek_slider.value() == 200000
    tile.seek_slider.setSliderDown(True)
    tile.seek_slider.setSliderPosition(750000)
    tile.update_seek(30000, 40000, 32000)
    assert not commands and tile.seek_bounds == (10000, 20000)
    tile.seek_slider.setSliderDown(False)
    assert commands == [("seek_absolute", 17500)]
    tile.close()


def test_four_speed_main_clock_does_not_chase_requested_throughput(app):
    board = VideoBoard()
    board.timer.stop()
    board.select(["A"])
    row = {"state": "ready", "asset_id": "a"*64, "path": "a.mp4", "metadata": {
        "camera": "A", "intervals": [{"wall_start": 10000, "wall_end": 90000, "media_start": 0, "media_end": 80000}]}}
    board.timeline = VideoTimeline(intervals_from_rows([row]))
    tile = board.tiles["A"]
    tile.asset_id, tile.interval, tile.ready = row['asset_id'], board.timeline.intervals[0], True
    tile.engine = SimpleNamespace(get_time_ms=lambda: 3000, current_status='playing')
    board._observe = lambda *_: None
    board._prepare_next = lambda: None
    board.playing, board.rate, board.reference_ms = True, 4, 17000
    board.tick()
    assert board.reference_ms == 13000
    tile.engine = None
    board.close()


def test_record_status_colors_distinguish_active_unfinished_new_and_done(tmp_path, app, monkeypatch):
    window = MainWindow()
    window.catalog = Catalog(tmp_path)
    window.devices.addItem("test", "test")
    rows = [{"kind": "imu", "path": f"{i}.json", "asset_id": str(i)*64, "stamp": "same",
             "state": "ready", "metadata": {"device": "test"}} for i in range(1, 5)]
    window.rows = rows
    window.current_row = rows[0]
    window.work = SessionWork(rows[0]['asset_id'])
    window.settings['review_progress'] = {
        '2.json': {'stamp': 'same', 'status': 'in_progress', 'imu_ms': 83000},
        '4.json': {'stamp': 'same', 'status': 'done'}}
    monkeypatch.setattr(window, 'select_record', lambda *_: None)
    window.source_available = True
    window.refresh_records()
    items = [window.records.item(i) for i in range(4)]
    assert [i.text().split()[1] for i in items] == ['正在标注', '未完成', '未开始', '已完成']
    assert len({i.foreground().color().name() for i in items}) == 4
    assert '83.0' in items[1].toolTip()
    window.work = None
    window.close()
    pump(app, lambda: window._closed)


def test_status_record_retains_full_long_paths(app):
    window = MainWindow()
    message = "扫描：" + "数据工程/视角01/长路径/" * 30
    window.set_index_status(message)
    window.show_status_details()
    assert message in window.status_log.toPlainText()
    window.close()
    pump(app, lambda: window._closed)


def test_remux_playback_uses_its_own_offsets_and_preserves_original_metadata(tmp_path):
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.catalog import file_stamp
    from cowmata_tailring.workspace.compatibility import CompatibilityCache
    cache = CompatibilityCache(tmp_path)
    cache.root.mkdir(parents=True)
    asset = "a"*64
    path = cache.root / (asset + ".mkv")
    path.write_bytes(b"validated remux fixture")
    index = MediaTimelineIndex("old.building.mkv", 0, 0, 0, 40,
                               (TimelineSegment(0, 10000, 0, 10000),), ())
    cache.entries[asset] = {"stamp": file_stamp(path), "timeline": index.to_dict()}
    source = {"format": "mpeg", "timeline": {"native": {"keys": [[0, 100000]]}}}
    result = cache.playback_metadata(asset, source)
    assert result['format'] == 'matroska'
    assert not result['timeline'].get('native')
    assert result['timeline']['source']['path'] == str(path.resolve())
    assert source['timeline']['native']['keys'] == [[0, 100000]]


def test_switch_away_and_return_preserves_drafts_and_resume_position(tmp_path, app):
    directory = tmp_path / '九轴' / 'test' / 'batch'
    directory.mkdir(parents=True)
    for i in range(2):
        raw = b''.join(struct.pack('<I9h', j*20, *([1]*9)) for j in range(251))
        atomic_json(directory / f'{i}.json', {'version':2, 'imu':base64.b64encode(raw).decode(),
                    'create_time':1785732300601+i*10000, 'update_time':1785739800000, 'device':'test'})
    window = MainWindow()
    window.open_project(tmp_path)
    pump(app, lambda: window.work is not None and window._load_notified, timeout=8)
    first = window.work.asset_id
    window.work.add_draft(0, 100, 200, [])
    window._set_imu(1800)
    window.dirty = True
    window.records.setCurrentRow(1)
    pump(app, lambda: window.work is not None and window.work.asset_id != first, timeout=8)
    second = window.work.asset_id
    assert '未完成' in window.records.item(0).text()
    assert '正在标注' in window.records.item(1).text()
    window.work.add_draft(1, 300, 500, [])
    window.dirty = True
    window.records.setCurrentRow(0)
    pump(app, lambda: window.work is not None and window.work.asset_id == first, timeout=8)
    assert window.imu_ms == 1800 and len(window.work.drafts) == 1
    assert '未完成' in window.records.item(1).text()
    from cowmata_tailring.workspace.storage import read_json
    assert len(read_json(window.catalog.work_path(second))['drafts']) == 1
    window.close()
    pump(app, lambda: window._closed)


@pytest.mark.parametrize("choice", ["save", "discard", "cancel"])
def test_close_choice_preserves_or_writes_only_requested_work(tmp_path, app, monkeypatch, choice):
    from cowmata_tailring.workspace.storage import read_json
    window = MainWindow()
    window.catalog = Catalog(tmp_path)
    window.work = SessionWork("a"*64)
    window.work.add_draft(0, 100, 200, [], note="saved")
    window.save_current()
    path = window.catalog.work_path(window.work.asset_id)
    window.work.drafts[0]["note"] = "unsaved correction"
    window.dirty = True
    calls = []
    monkeypatch.setattr(window, "confirm_close", lambda: calls.append(choice) or choice)
    window.close()
    if choice == "cancel":
        assert not window._closing_requested and not window._closed
        assert window.save_timer.isActive() and window.dirty
        assert read_json(path)["drafts"][0]["note"] == "saved"
        monkeypatch.setattr(window, "confirm_close", lambda: "save")
        window.close()
    pump(app, lambda: window._closed)
    assert calls == [choice]
    assert read_json(path)["drafts"][0]["note"] == ("saved" if choice == "discard" else "unsaved correction")


def test_close_prompt_defaults_to_save_and_escape_returns_to_work(tmp_path, app, monkeypatch):
    window = MainWindow()
    window.catalog = Catalog(tmp_path)
    def inspect(dialog):
        assert dialog.defaultButton().text() == "保存并退出"
        assert dialog.escapeButton().text() == "返回继续标注"
        assert "此前自动保存" in dialog.informativeText()
        dialog.escapeButton().click()
    monkeypatch.setattr(QMessageBox, "exec", inspect)
    assert REAL_CLOSE_PROMPT(window) == "cancel"
    window.close()
    pump(app, lambda: window._closed)


def test_saved_completed_label_can_be_edited_deleted_and_undone_after_reopen(tmp_path, app, monkeypatch):
    from cowmata_tailring.annotation.core import Event
    from cowmata_tailring.workspace.storage import read_json
    first = MainWindow()
    first.catalog = Catalog(tmp_path)
    first.work = SessionWork("b"*64)
    first.work.project.events = [Event(id=1, li=0, t0=100, t1=400, note="incorrect", extras={"confirmation": "confirmed"})]
    first.work.progress["status"] = "done"
    first.save_current()
    path = first.catalog.work_path(first.work.asset_id)
    first.close()
    pump(app, lambda: first._closed)
    window = MainWindow()
    window.catalog = Catalog(tmp_path)
    window.work = SessionWork.from_dict(read_json(path))
    window.motion = SimpleNamespace(duration_ms=10000)
    window.source_available = True
    window.refresh_events()
    window.events.selectRow(0)
    def edit(dialog):
        dialog.findChild(QComboBox).setCurrentIndex(1)
        fields = dialog.findChildren(QLineEdit)
        fields[0].setText("1.25")
        fields[1].setText("2.5")
        fields[2].setText("corrected after reopening")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", edit)
    window.edit_selected()
    event = window.work.project.events[0]
    assert (event.li, event.t0, event.t1, event.note) == (1, 1250, 2500, "corrected after reopening")
    assert event.extras["confirmation"] == "needs_review"
    assert read_json(path)["progress"]["status"] == "in_progress"
    assert read_json(path)["project"]["events"][0]["note"] == event.note
    window.events.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    window.delete_selected()
    assert not read_json(path)["project"]["events"]
    assert window.work.undo_once()
    assert window.work.project.events[0].note == event.note
    window.close()
    pump(app, lambda: window._closed)


def test_cleanup_file_lock_retains_marker_for_retry(tmp_path, monkeypatch):
    from pathlib import Path
    cat = Catalog(tmp_path, load_session=True)
    original = Path.unlink
    def blocked(path, *args, **kwargs):
        if path.name == "index.sqlite":
            raise PermissionError("test locked file")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", blocked)
    cat.close()
    assert (cat.meta / ".load-pending.json").exists()
    monkeypatch.setattr(Path, "unlink", original)
    Catalog(tmp_path, load_session=True).close()
    assert not cat.meta.exists()
