import csv
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import Anchor, ClockMap, VideoTimeline, intervals_from_rows
from cowmata_tailring.workspace.storage import atomic_json, read_json, unique_batch
from cowmata_tailring.workspace.window import MainWindow
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def seed(root):
    catalog = Catalog(root, stability_seconds=0)
    (root / "001.mp4").write_bytes(b"test content; never decoded")
    metadata = {"camera": "A", "intervals": [{"wall_start": 10000, "wall_end": 20000,
                  "media_start": 0, "media_end": 10000, "verified": True}]}
    catalog.scan(now=100)
    catalog.index_one("001.mp4", lambda *_: metadata, now=101)
    return catalog


def test_recheck_restores_manual_correction(tmp_path):
    catalog = seed(tmp_path)
    row = catalog.rows()[0]
    correction = {"asset_id": row["asset_id"], "readings": [{"media_ms": 0}, {"media_ms": 10000}],
                  "camera": "B", "roi": [.1, .1, .6, .3], "intervals": row["metadata"]["intervals"]}
    atomic_json(catalog.meta / "video_corrections" / (row["asset_id"] + ".json"), correction)
    catalog.recheck("001.mp4")
    catalog.index_one("001.mp4", lambda *_: {"camera": "WRONG", "needs_review": True}, now=105)
    assert catalog.rows()[0]["metadata"]["camera"] == "B"
    assert catalog.rows()[0]["metadata"]["intervals"] == correction["intervals"]
    assert catalog.rows()[0]["state"] == "ready"
    catalog.close()


def test_annotation_edit_reconfirmation_keeps_edited_boundary():
    work = SessionWork("asset")
    work.project.cow_id = "C1"
    work.clock = ClockMap([Anchor(0, 10000), Anchor(10000, 20000)])
    proof = [{"frame_ready": True, "verified_interval": True}]
    draft = work.add_draft(0, 11000, 12000, proof)
    event = work.confirm_draft(draft["id"], 10000)
    work.edit_event(event.id, 1500, 2500, 10000, note="rechecked")
    assert draft["reference_start"] == 11500
    assert not draft["video_evidence"]
    with pytest.raises(ValueError):
        work.confirm_draft(draft["id"], 10000)
    draft["video_evidence"] = proof
    second = work.confirm_draft(draft["id"], 10000)
    assert (second.t0, second.t1, second.note) == (1500, 2500, "rechecked")
    with pytest.raises(ValueError):
        work.training_project(evidence_validator=lambda _: False)


def test_open_source_replacement_hides_old_frame_and_blocks_reopen(tmp_path, app):
    catalog = seed(tmp_path)
    window = MainWindow()
    window.source_timer.stop()
    window.save_timer.stop()
    window.board.timer.stop()
    window.catalog = catalog
    window.rows = catalog.rows()
    timeline = VideoTimeline(intervals_from_rows(window.rows))
    window.board.configure(catalog, window.rows, timeline)
    tile = window.board._new_tile()
    tile.camera = "A"
    tile.asset_id = window.rows[0]["asset_id"]
    tile.interval = timeline.intervals[0]
    tile.ready = True
    tile.stack.show()
    window.board.tiles = {"A": tile}
    window.board.selected = ["A"]
    window.board.reference_ms = 12000
    (tmp_path / "001.mp4").write_bytes(b"replacement")
    window.check_active_sources()
    assert tile.stack.isHidden() and not tile.ready and tile.interval is None
    assert tile.asset_id in window.board.blocked_assets
    window.board._position("A", tile, force=True)
    assert tile.engine is None and tile.interval is None
    # A stale in-memory snapshot cannot unblock a source.
    window.board.configure(catalog, window.rows, timeline)
    assert tile.asset_id in window.board.blocked_assets
    window.dirty = False
    window.close()
    catalog.close()


def test_root_switch_is_blocked_when_save_failed(tmp_path, app, monkeypatch):
    catalog = seed(tmp_path)
    window = MainWindow()
    window.catalog = catalog
    window.dirty = True
    monkeypatch.setattr(window, "save_current", lambda *_, **kw: None)
    window.open_project(tmp_path / "other")
    assert window.catalog is catalog
    assert not (tmp_path / "other").exists()
    window.dirty = False
    window.close()
    catalog.close()


def test_cancel_owned_decoder_does_not_wait_for_full_timeout():
    stop = threading.Event()
    timer = threading.Timer(.2, stop.set)
    timer.start()
    start = time.monotonic()
    with pytest.raises(RuntimeError):
        run_cancellable([sys.executable, "-c", "import time; time.sleep(60)"], cancelled=stop.is_set)
    assert time.monotonic() - start < 4
    timer.join()


@pytest.mark.parametrize("name", ["CON", "NUL.txt", "COM1", "LPT9", "trailing."])
def test_reject_reserved_batch_names(tmp_path, name):
    with pytest.raises(ValueError):
        unique_batch(tmp_path, name)


def test_atomic_backup_contains_prior_valid_version(tmp_path):
    target = tmp_path / "human.json"
    atomic_json(target, {"event": 1})
    atomic_json(target, {"event": 2})
    target.write_text("partial write")
    assert read_json(target) == {"event": 1}


def test_corrupt_index_quarantined_without_losing_human_work(tmp_path):
    catalog = seed(tmp_path)
    asset_id = catalog.rows()[0]["asset_id"]
    human = catalog.work_path(asset_id)
    atomic_json(human, {"events": ["irreplaceable"]})
    index = catalog.meta / "index.sqlite"
    catalog.close()
    index.write_bytes(b"corrupt derived index")
    restored = Catalog(tmp_path)
    try:
        assert restored.recovered_index.read_bytes() == b"corrupt derived index"
        assert read_json(human) == {"events": ["irreplaceable"]}
        assert restored.rows() == []
        assert (tmp_path / "001.mp4").read_bytes() == b"test content; never decoded"
    finally:
        restored.close()


@pytest.mark.parametrize("message, recover", [("file is not a database", True),
                                             ("database disk image is malformed", True),
                                             ("disk I/O error", False), ("database is locked", False)])
def test_legacy_sqlite_error_without_result_code(tmp_path, monkeypatch, message, recover):
    import sqlite3

    from cowmata_tailring.workspace import catalog as module
    catalog = seed(tmp_path)
    index = catalog.meta / "index.sqlite"
    catalog.close()
    before = index.read_bytes()
    connect = sqlite3.connect

    class FailedConnection:
        def execute(self, *_):
            raise sqlite3.DatabaseError(message)

        def close(self):
            pass

    calls = []

    def legacy_connect(*args, **kwargs):
        calls.append(True)
        return FailedConnection() if len(calls) == 1 else connect(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", legacy_connect)
    if recover:
        restored = Catalog(tmp_path)
        try:
            assert restored.recovered_index.read_bytes() == before
        finally:
            restored.close()
    else:
        with pytest.raises(RuntimeError, match=message):
            Catalog(tmp_path)
        assert index.read_bytes() == before
        assert not list(index.parent.glob("index.corrupt.*"))


def test_window_confirm_export_revision_gate_and_reopen(tmp_path, app, monkeypatch):
    catalog = seed(tmp_path)
    window = MainWindow()
    window.board.timer.stop()
    window.source_timer.stop()
    window.save_timer.stop()
    window.catalog = catalog
    window.rows = catalog.rows()
    row = window.rows[0]
    window.work = SessionWork("a" * 64)
    window.work.project.cow_id = "SYNTHETIC-TEST"
    window.work.clock = ClockMap([Anchor(0, 10000), Anchor(10000, 20000)])
    times = np.array([0, 100, 300, 1000, 2050, 2200, 2500, 3100, 10000], dtype=float)
    window.motion = SimpleNamespace(duration_ms=10000, times_ms=times, quality_report=lambda: {"sample_count": len(times)})
    window.source_available = True
    proof = [{"camera": "A", "asset_id": row["asset_id"], "frame_ready": True,
              "verified_interval": True, "reference_ms": 12000,
              "camera_mapping_revision": "uncalibrated", "video_revision": window.video_revision(row)}]
    draft = window.work.add_draft(0, 12000, 13000, proof)
    window.refresh_events()
    window.events.selectRow(0)
    window.confirm_selected()
    assert window.work.project.events[0].extras["confirmation"] == "confirmed"
    assert window.work.project.events[0].t0 == 2000
    assert read_json(catalog.work_path(window.work.asset_id))["drafts"][0]["id"] == draft["id"]
    output = tmp_path / "exports"
    output.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: str(output))
    window.export_training()
    exported = output / "COWMATA_aaaaaaaa"
    assert (exported / "参考时间与视频证据.csv").is_file()
    assert (exported / "BORIS.csv").is_file()
    with (exported / "逐样本标签.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(times)
    assert any(r["reviewed_any"] == "0" for r in rows)
    restored = SessionWork.from_dict(read_json(catalog.work_path(window.work.asset_id)))
    assert restored.project.events[0].t0 == 2000
    window.settings["camera_maps"] = {"A": {"revision": "changed"}}
    assert not window.validate_evidence(proof)
    window.export_training()
    second = output / "COWMATA_aaaaaaaa_02"
    assert not second.exists()  # Validate first; no partial extra directory.
    assert window.work.project.events[0].t0 == 2000
    window.dirty = False
    window.close()
    catalog.close()
