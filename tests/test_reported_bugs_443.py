# ruff: noqa: F811  (pytest fixtures shared from test_annotation_pipeline_v330)
"""4.4.3: every reported 4.4.1 problem has a working path, and every block names its fix."""
from types import SimpleNamespace

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QTableWidget
from test_annotation_pipeline_v330 import case  # noqa: F401
from test_annotation_pipeline_v330 import window as workflow_window  # noqa: F401

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, wall_ms, wall_text
from cowmata_tailring.workspace.dialogs import SourceTimeDialog
from cowmata_tailring.workspace.review_guidance import (
    confirmation_text,
    needs_time_check,
    verified_share,
)


def aligned(window, cow="COW-443"):
    window.work.project.cow_id = cow
    window.work.clock = ClockMap([Anchor(0, 10000), Anchor(window.motion.duration_ms, 11000)])
    return next(r for r in window.rows if r["kind"] == "video")


def live(window, row, *, ready=True, verified=True, reference=10150):
    return [{"camera": "A", "asset_id": row["asset_id"], "frame_ready": ready, "verified_interval": verified,
             "reference_ms": reference, "media_ms": reference - 10000, "frame_source": "ffmpeg_pts",
             "camera_mapping_revision": "uncalibrated", "video_revision": window.video_revision(row)}]


def select(window, kind, identifier):
    window.refresh_events(preferred=(kind, identifier))
    for i in range(window.events.rowCount()):
        if window.events.item(i, 0).data(Qt.ItemDataRole.UserRole) == (kind, identifier):
            window.events.selectRow(i)
            return
    raise AssertionError("entry not listed")


def test_legacy_label_is_confirmed_in_one_step_and_one_undo_restores_it(workflow_window, monkeypatch):
    window = workflow_window
    row = aligned(window)
    event = window.work.project.add_event(0, 100, 200, note="旧工程")
    event.extras.update(confirmation="legacy_unreviewed", origin="legacy-file")
    monkeypatch.setattr(window, "evidence", lambda: live(window, row))
    window.board.reference_ms = 10150
    select(window, "event", event.id)
    window.confirm_selected()
    [confirmed] = window.work.project.events
    assert confirmed.id == event.id and confirmed.extras["confirmation"] == "confirmed"
    assert abs(confirmed.t0 - 100) < 1e-6 and abs(confirmed.t1 - 200) < 1e-6
    assert confirmed.note == "旧工程" and confirmed.extras["origin"] == "legacy-file"
    assert confirmed.extras["video_evidence"][0]["verified_interval"]
    assert window.events.item(window.events.currentRow(), 4).text() == "已确认"
    assert window.work.undo_once()
    [restored] = window.work.project.events
    assert restored.extras["confirmation"] == "legacy_unreviewed" and "draft_id" not in restored.extras
    assert window.work.drafts == []


def test_blocked_legacy_confirmation_names_video_and_leaves_label_untouched(workflow_window, monkeypatch):
    window = workflow_window
    row = aligned(window)
    event = window.work.project.add_event(0, 100, 200)
    event.extras["confirmation"] = "legacy_unreviewed"
    monkeypatch.setattr(window, "evidence", lambda: live(window, row, verified=False))
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    window.board.reference_ms = 10150
    select(window, "event", event.id)
    window.confirm_selected()
    assert window.work.project.events[0].extras["confirmation"] == "legacy_unreviewed"
    assert "draft_id" not in window.work.project.events[0].extras and window.work.drafts == []
    assert "view01.mp4" in messages[-1] and "核验" in messages[-1] and "开头和结尾" in messages[-1]
    assert window.event_status.text() == messages[-1]


def test_confirm_away_from_the_action_jumps_there_first(workflow_window, monkeypatch):
    window = workflow_window
    row = aligned(window)
    draft = window.work.add_draft(0, 10100, 10200, live(window, row, ready=False))
    monkeypatch.setattr(window, "evidence", lambda: live(window, row, reference=20000))
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    window.board.reference_ms = 20000
    select(window, "draft", draft["id"])
    window.confirm_selected()
    assert window.work.project.events == []
    assert window.board.reference_ms == 10100
    assert "已跳到这条动作的开头" in messages[-1]
    assert not any(e["reference_ms"] == 20000 for e in draft["video_evidence"])


def test_evidence_problem_explains_each_cause(workflow_window, monkeypatch):
    window = workflow_window
    row = aligned(window)
    window.board.main_camera = "A"
    message, target = window.evidence_problem([])
    assert "没有录像画面" in message and target is None
    message, target = window.evidence_problem(live(window, row, ready=False))
    assert "画面还在加载" in message and target is None
    message, target = window.evidence_problem(live(window, row, ready=True, verified=False))
    assert "view01.mp4" in message and target["path"] == row["path"]
    window.settings["camera_maps"] = {"A": {"revision": "changed"}}
    message, target = window.evidence_problem(live(window, row))
    assert "校准" in message and target is None


def test_update_evidence_on_saved_label_points_to_direct_confirmation(workflow_window, monkeypatch):
    window = workflow_window
    aligned(window)
    event = window.work.project.add_event(0, 100, 200)
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    select(window, "event", event.id)
    window.update_evidence()
    assert "确认真值" in messages[-1] and "旧标注需先" not in messages[-1]


def test_label_states_read_as_plain_chinese():
    assert [confirmation_text(code) for code in ("confirmed", "needs_review", "legacy_unreviewed", None)] == [
        "已确认", "需复核", "旧标注·待确认", "旧标注·待确认"]


def video_row(path, state, verified, duration=600000):
    return {"path": path, "kind": "video", "asset_id": path.ljust(64, "0")[:64], "state": state, "stamp": "s",
            "error": "", "metadata": {"camera": "视角03", "duration_ms": duration, "start_display": "2026-09-04 21_00_00",
                                      "intervals": [] if state == "pending" else [{
                                          "wall_start": 1000, "wall_end": 1000 + duration, "media_start": 0,
                                          "media_end": duration, "verified": verified}]}}


def test_time_check_rules_cover_review_unverified_and_manual_partial():
    assert needs_time_check(video_row("a.mp4", "review", False))
    assert needs_time_check(video_row("b.mp4", "ready", False))
    assert not needs_time_check(video_row("c.mp4", "ready", True))
    assert not needs_time_check(video_row("d.mp4", "pending", False))
    assert not needs_time_check({"kind": "imu", "state": "ready", "metadata": {}})
    partial = video_row("e.mp4", "ready", False)
    partial["metadata"]["manual_readings"] = [{"media_ms": 0, "wall_ms": 1000}, {"media_ms": 300000, "wall_ms": 301000}]
    assert needs_time_check(partial) and abs(verified_share(partial) - .5) < 1e-6
    partial["metadata"]["manual_readings"].append({"media_ms": 599000, "wall_ms": 600000})
    assert verified_share(partial) > .99


def test_source_manager_lists_videos_needing_time_check_first_and_highlighted(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    for timer in (window.save_timer, window.source_timer, window.board.timer):
        timer.stop()
    window.catalog = Catalog(tmp_path)
    window.rows = [{"path": "产犊/Motion/r.json", "kind": "imu", "asset_id": "i" * 64, "state": "ready",
                    "stamp": "s", "error": "", "metadata": {"device": "546C50CA07DA"}},
                   video_row("录像/v1.mp4", "ready", True), video_row("录像/v2.mp4", "review", False),
                   video_row("录像/v3.mp4", "pending", False)]
    seen = {}

    def inspect(dialog):
        scope = dialog.findChild(QComboBox)
        table = dialog.findChild(QTableWidget)
        seen["scope"] = scope.currentData()
        seen["rows"] = [table.item(i, 0).text() for i in range(table.rowCount())]
        seen["state"] = table.item(0, 2).text()
        seen["color"] = table.item(0, 0).background().color().name()
        scope.setCurrentIndex(3)
        seen["all"] = table.rowCount()
        scope.setCurrentIndex(2)
        seen["videos"] = [table.item(i, 0).text() for i in range(table.rowCount())]

    monkeypatch.setattr(QDialog, "exec", inspect)
    try:
        window.source_manager()
    finally:
        window.catalog.close()
        window.close()
        app.processEvents()
    assert seen["scope"] == "verify" and seen["rows"] == ["录像/v2.mp4"]
    assert seen["state"] == "待复核 · 需核验时间" and seen["color"] == "#fff1cc"
    assert seen["all"] == 4
    assert seen["videos"][0] == "录像/v2.mp4" and seen["videos"][-1] == "录像/v3.mp4"


def test_source_manager_defaults_to_videos_of_the_loaded_record(workflow_window, monkeypatch):
    window = workflow_window
    aligned(window)
    window.rows = [r if r["kind"] != "video" else {**r, "state": "review", "metadata": {
        **r["metadata"], "intervals": [{**r["metadata"]["intervals"][0], "verified": False}]}} for r in window.rows]
    seen = {}

    def inspect(dialog):
        table = dialog.findChild(QTableWidget)
        seen["scope"] = dialog.findChild(QComboBox).currentData()
        seen["rows"] = [table.item(i, 0).text() for i in range(table.rowCount())]
        seen["summary"] = " ".join(label.text() for label in dialog.findChildren(QLabel))

    monkeypatch.setattr(QDialog, "exec", inspect)
    window.source_manager()
    assert seen["scope"] == "record" and seen["rows"] == ["view01.mp4"]
    assert "当前记录用到 1 段录像：需核验时间 1 段" in seen["summary"]


def test_time_dialog_prefills_prediction_and_reports_verified_span(tmp_path):
    app = QApplication.instance() or QApplication([])
    start = wall_ms("2026-09-04 21:00:00")
    row = {"path": "v.mp4", "kind": "video", "asset_id": "v" * 64, "state": "review", "stamp": "s", "metadata": {
        "duration_ms": 600000, "intervals": [{"wall_start": start, "wall_end": start + 600000,
                                              "media_start": 0, "media_end": 600000, "verified": False}]}}
    catalog = SimpleNamespace(meta=tmp_path, source_path=lambda p: tmp_path / p)
    dialog = SourceTimeDialog(catalog, row)
    try:
        frame = Image.new("RGB", (16, 9))
        dialog.on_result((dialog.generation, "frame", (frame, 1500.0)))
        assert dialog.timestamp.text() == wall_text(start + 1500)
        dialog.accept_reading()
        assert "粗定位" in dialog.coverage.text()
        dialog.on_result((dialog.generation, "frame", (frame, 599000.0)))
        assert dialog.timestamp.text() == wall_text(start + 599000)
        dialog.timestamp.setText("2026-09-04 21:09:58")
        dialog.accept_reading()
        assert "第 1.5 秒到第 599.0 秒之间算已核验" in dialog.coverage.text()
        assert "100%" in dialog.coverage.text()
        dialog.timestamp.textEdited.emit("typed by operator")
        dialog.on_result((dialog.generation, "frame", (frame, 3000.0)))
        assert dialog.timestamp.text() == "2026-09-04 21:09:58"
    finally:
        dialog.done(0)
        app.processEvents()


def _ps_pair(tmp_path):
    first = tmp_path / "录像" / "2026-09-06" / "视角02" / "2026-09-06_10-09-26.mp4"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"non-Dahua MPEG-PS in an mp4 name")
    (first.parent / "2026-09-06_10-31-35.mp4").write_bytes(b"next recording")
    info = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "avg_frame_rate": "15/1"}], "format": {"format_name": "mpeg"}}
    return first, info


def _native(path, wall_start, duration=1_316_000):
    stat = path.stat()
    return dict(family="shenmo-pes2-epoch", duration_ms=duration, wall_start=wall_start, first_pts_ms=0,
                frame_ms=70.0, keys=[[0, 0]], valid_end=stat.st_size, source_size=stat.st_size,
                source_mtime_ns=stat.st_mtime_ns)


def test_non_dahua_recorder_ps_is_timed_by_its_own_clock_and_confirmable(tmp_path, monkeypatch):
    """4.4.1 bug 1 root cause: 视角02–04 PS files kept an unverified filename guess forever."""
    from cowmata_tailring.workspace import probe
    from cowmata_tailring.workspace.video_names import filename_wall

    path, info = _ps_pair(tmp_path)
    monkeypatch.setattr(probe, "probe_media", lambda *a, **k: info)
    monkeypatch.setattr(probe, "read_native_index", lambda p, **k: _native(p, filename_wall(p) + 622))
    value = probe.SourceInspector(tmp_path, tmp_path / "meta").video(path, "a" * 64)
    assert value["duration_basis"] == "timeline" and value["duration_ms"] == 1_316_000
    assert value["intervals"][0]["verified"] and not value["needs_review"]
    assert value["timeline"]["native"]["family"] == "shenmo-pes2-epoch"


def test_recorder_clock_far_from_the_name_still_needs_manual_check(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import probe
    from cowmata_tailring.workspace.video_names import filename_wall

    path, info = _ps_pair(tmp_path)
    monkeypatch.setattr(probe, "probe_media", lambda *a, **k: info)
    monkeypatch.setattr(probe, "read_native_index", lambda p, **k: _native(p, filename_wall(p) + 95_000))
    value = probe.SourceInspector(tmp_path, tmp_path / "meta").video(path, "b" * 64)
    assert value["needs_review"] and not value["intervals"][0]["verified"]
    assert "相差 95 秒" in value["warnings"][-1]


def test_packet_clock_is_used_when_the_recorder_index_is_unreadable(tmp_path, monkeypatch):
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace import probe

    path, info = _ps_pair(tmp_path)
    monkeypatch.setattr(probe, "probe_media", lambda *a, **k: info)

    def unreadable(*_a, **_k):
        raise ValueError("原生录像 PTS 跳变")
    monkeypatch.setattr(probe, "read_native_index", unreadable)
    stat = path.stat()
    packets = MediaTimelineIndex(str(path), stat.st_size, stat.st_mtime_ns, 0, 66.67,
                                 (TimelineSegment(0, 1_140_000, 0, 1_140_000),), ())
    monkeypatch.setattr(probe, "probe_media_timeline", lambda *a, **k: packets)
    value = probe.SourceInspector(tmp_path, tmp_path / "meta").video(path, "c" * 64)
    assert value["duration_basis"] == "timeline" and value["duration_ms"] == 1_140_000
    assert value["intervals"][0]["verified"]


def test_recorder_timed_rows_are_not_requeued_on_every_open(tmp_path):
    import json

    root = tmp_path / "farm"
    video = root / "录像" / "2026-09-06" / "视角02" / "2026-09-06_10-09-26.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"ps bytes")
    catalog = Catalog(root)
    rows = {"timed": dict(format="mpeg", duration_basis="timeline", timeline={"native": None}, intervals=[]),
            "guess": dict(format="mpeg", duration_basis="adjacent_filename", timeline={"native": None}, intervals=[])}
    with catalog.db:
        for key, meta in rows.items():
            asset = "e" * 63 + key[0]
            catalog.db.execute("INSERT INTO assets VALUES(?,?,?,?)", (asset, "video", json.dumps(meta), 1.0))
            catalog.db.execute("INSERT INTO locations VALUES(?,?,?,?,?,'ready',1.0,1.0,'',0)",
                               (f"录像/2026-09-06/视角02/{key}.mp4", "video",
                                json.dumps([video.stat().st_size, 1, 1, 1]), "quick:x", asset))
    try:
        assert catalog.queue_dahua_timeline_upgrade() == 1
        states = {r["path"].rsplit("/", 1)[-1]: r["state"] for r in catalog.rows()}
        assert states == {"timed.mp4": "ready", "guess.mp4": "pending"}
    finally:
        catalog.close()

def test_qt_standard_buttons_read_chinese_in_the_chinese_ui():
    from PySide6.QtWidgets import QDialogButtonBox

    from cowmata_tailring.ui import i18n

    app = QApplication.instance() or QApplication([])
    previous = i18n.get_language()
    i18n.set_language("zh")
    translator = i18n.install_qt_translations(app)
    try:
        assert translator is not None
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                               | QDialogButtonBox.StandardButton.Close)
        texts = {box.button(b).text().replace("&", "").split("(")[0] for b in (
            QDialogButtonBox.StandardButton.Save, QDialogButtonBox.StandardButton.Cancel, QDialogButtonBox.StandardButton.Close)}
        assert texts == {"保存", "取消", "关闭"}
    finally:
        if translator is not None:
            app.removeTranslator(translator)
        i18n.set_language(previous)
    i18n.set_language("en")
    try:
        assert i18n.install_qt_translations(app) is None
    finally:
        i18n.set_language(previous)

def test_saving_a_time_check_updates_the_index_and_reports_coverage(workflow_window, monkeypatch):
    window = workflow_window
    original = next(r for r in window.rows if r["kind"] == "video")
    interval = {**original["metadata"]["intervals"][0], "verified": False}
    row = {**original, "state": "review", "metadata": {**original["metadata"], "duration_ms": 1000, "intervals": [interval]}}

    def accept(dialog):
        dialog.readings = [{"media_ms": 0.0, "wall_ms": 10000.0}, {"media_ms": 990.0, "wall_ms": 10990.0}]
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SourceTimeDialog, "exec", accept)
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    assert window.edit_source(row)
    assert "时间已核验并保存" in messages[-1]
    saved = next(r for r in window.catalog.rows() if r["asset_id"] == row["asset_id"])
    assert len(saved["metadata"]["manual_readings"]) == 2 and verified_share(saved) > .95