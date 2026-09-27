"""4.1.7 regressions: PS frame-clock priority, timeline re-queue, resume replay."""
import json
from pathlib import Path

import pytest

from cowmata_tailring.workspace import probe as probe_module
from cowmata_tailring.workspace.dahua_tasks import _legacy_request_matches


def _media_info(duration=None, fmt="mpeg"):
    return {
        "streams": [dict(codec_type="video", codec_name="hevc", width=2560, height=1440,
                         duration=duration, avg_frame_rate="15/1")],
        "format": {"format_name": fmt, "duration": duration or "85802.0"},
    }


def test_ps_with_adjacent_name_prefers_frame_clock_timeline(tmp_path, monkeypatch):
    """A Dahua PS recording must not keep the adjacent-filename guess when its
    own frame clock can be measured; mid-file frames then stay decodable."""
    from cowmata_tailring.media.dahua_duration import DahuaProgramScan
    from cowmata_tailring.workspace.probe import SourceInspector

    first = tmp_path / "2026-09-02_00-22-52.mp4"
    following = tmp_path / "2026-09-02_00-59-11.mp4"
    first.write_bytes(b"legacy PS in mp4 extension")
    following.write_bytes(b"next legacy PS in mp4 extension")
    info = _media_info()
    info["streams"][0].pop("duration")
    monkeypatch.setattr(probe_module, "probe_media", lambda *a, **k: info)

    def fake_timeline(path, cancelled=lambda: False):
        return probe_module.MediaTimelineIndex(
            str(Path(path).resolve()), Path(path).stat().st_size, Path(path).stat().st_mtime_ns,
            0, 66.66, (probe_module.TimelineSegment(0, 2_178_765, 0, 2_178_765),), (),
            native=dict(signature="dahua-classified-frame-clock-1", family="dahua-program-frame-clock",
                        source_size=Path(path).stat().st_size, source_mtime_ns=Path(path).stat().st_mtime_ns,
                        first_pts_ms=0, frame_ms=66.66, frame_count=32686, duration_ms=2_178_765,
                        valid_end=Path(path).stat().st_size, keys=[[0, 0]], timestamp_data="", patch_timestamps=True),
        )

    monkeypatch.setattr(
        "cowmata_tailring.media.classified_dahua.classified_dahua_timeline", fake_timeline)
    inspector = SourceInspector(tmp_path, tmp_path / "meta")
    value = inspector.video(first, "a" * 64)
    assert value["duration_ms"] == 2_178_765
    assert value["duration_basis"] == "timeline"
    assert value["timeline"]["native"]["frame_count"] == 32686


def test_ps_adjacent_guess_survives_when_frame_clock_fails(tmp_path, monkeypatch):
    """A broken scan keeps the previous bounded browse guess instead of failing."""
    from cowmata_tailring.workspace.probe import SourceInspector

    first = tmp_path / "2026-09-02_00-22-52.mp4"
    following = tmp_path / "2026-09-02_00-59-11.mp4"
    first.write_bytes(b"legacy PS in mp4 extension")
    following.write_bytes(b"next legacy PS in mp4 extension")
    info = _media_info()
    info["streams"][0].pop("duration")
    monkeypatch.setattr(probe_module, "probe_media", lambda *a, **k: info)

    def broken(path, cancelled=lambda: False):
        raise ValueError("大华码流缺少有效帧计数、帧时钟或关键帧")

    monkeypatch.setattr(
        "cowmata_tailring.media.classified_dahua.classified_dahua_timeline", broken)
    inspector = SourceInspector(tmp_path, tmp_path / "meta")
    value = inspector.video(first, "b" * 64)
    assert value["duration_basis"] == "adjacent_filename"
    assert value["needs_review"]


def test_dahua_timeline_upgrade_requeues_broken_ps_rows(tmp_path):
    """Indexed PS rows without seek keys are requeued once; manual clocks stay."""
    from cowmata_tailring.workspace.catalog import Catalog

    root = tmp_path / "farm"
    video = root / "录像" / "2026-09-02" / "视角01" / "2026-09-02_00-22-52.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"ps bytes")
    catalog = Catalog(root)
    broken = dict(camera="视角01", format="mpeg", duration_ms=2_179_000,
                  timeline={"native": None, "segments": []}, intervals=[], needs_review=True)
    fine = dict(camera="视角01", format="mov,mp4", duration_ms=46_601,
                timeline={"native": None}, intervals=[], needs_review=False)
    manual = dict(camera="视角01", format="mpeg", duration_ms=2_179_000,
                  timeline={"native": None}, intervals=[],
                  manual_readings=[{"media_ms": 0, "wall_ms": 1}, {"media_ms": 1000, "wall_ms": 1001}])
    with catalog.db:
        for key, meta in (("broken", broken), ("fine", fine), ("manual", manual)):
            asset = "f" * 63 + key[0]
            catalog.db.execute("INSERT INTO assets VALUES(?,?,?,?)", (asset, "video", json.dumps(meta), 1.0))
            catalog.db.execute(
                "INSERT INTO locations VALUES(?,?,?,?,?,'review',1.0,1.0,'',0)",
                (f"录像/2026-09-02/视角01/{key}.mp4", "video",
                 json.dumps([video.stat().st_size, 1, 1, 1]), "quick:x", asset))
    queued = catalog.queue_dahua_timeline_upgrade()
    assert queued == 1
    rows = {r["path"]: r for r in catalog.rows()}
    assert rows["录像/2026-09-02/视角01/broken.mp4"]["state"] == "pending"
    assert rows["录像/2026-09-02/视角01/fine.mp4"]["state"] == "review"
    assert rows["录像/2026-09-02/视角01/manual.mp4"]["state"] == "review"
    catalog.close()


def test_legacy_resume_request_without_deadline_still_matches():
    saved = {
        "target": "F:/扬大_高邮牧场", "category": "calving", "scenario": "mixed",
        "mapping": {"channel:1": "视角01"}, "start": "", "end": "",
        "split_midnight": True, "json_sources": [], "storage_profile": "native",
    }
    resumed = dict(saved, deadline_seconds=28800)
    assert _legacy_request_matches(resumed, saved)
    changed = dict(saved, mapping={"channel:1": "视角02"}, deadline_seconds=28800)
    assert not _legacy_request_matches(changed, saved)
    assert not _legacy_request_matches(resumed, None)


def test_resume_replays_saved_request_when_form_agrees():
    """DahuaPanel.organize replays the stored request so the signature holds."""
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.dahua_ui import DahuaPanel

    app = QApplication.instance() or QApplication([])
    panel = DahuaPanel()
    saved = {
        "target": "F:/牧场", "category": "calving", "scenario": "mixed",
        "mapping": {}, "start": "", "end": "", "split_midnight": True,
        "json_sources": [], "storage_profile": "native",
    }
    panel.apply_restored_options(saved)
    assert panel._resumed_request == saved
    captured = {}

    def fake_start(action, request, job=None):
        captured["action"] = action
        captured["options"] = request["options"]

    panel.start = fake_start
    panel.index = {"groups": {}}
    panel.job = Path("Z:/nowhere")
    panel.target.setText("F:/牧场")

    class Field:
        def __init__(self, data="", text=None):
            self.data = data
            self._text = text if text is not None else str(data)

        def currentData(self):
            return self.data

        def text(self):
            return self._text

        def isChecked(self):
            return bool(self.data)

    panel.category = Field("calving")
    panel.scenario = Field("mixed")
    panel.storage_profile = Field("native")
    panel.selected_mapping = lambda: {}
    panel.start_time = Field("", "")
    panel.end_time = Field("", "")
    panel.midnight = Field(True)
    panel.import_json = Field(False)
    panel.json_sources = []
    panel.organize()
    assert captured["options"] == dict(saved)
    assert panel._resumed_request is None
    panel.deleteLater()
