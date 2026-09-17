import base64
import copy
import struct

import pytest

from cowmata_tailring.annotation.data import parse_motion_object
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.dialogs import MappingDialog
from cowmata_tailring.workspace.label_file import build_label_file, load_history, save_label_file
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def record(version=2, ticks=(151, 171, 191, 311)):
    formats = {0: "<9h", 1: "<H9h", 2: "<I9h"}
    raw = b"".join(struct.pack(formats[version], *(([v] if version else []) + [1] * 9)) for v in ticks)
    return {"version": version, "imu": base64.b64encode(raw).decode(),
            "create_time": 1785732300601, "update_time": 1785739800000, "device": "test"}


def test_confirmed_capture_semantics_and_upload_independence():
    obj = record()
    m = parse_motion_object(obj)
    assert list(m.times_ms) == [0, 20, 40, 160]
    assert m.epoch_at(0) == obj["create_time"] + 151
    assert m.epoch_at(160) == obj["create_time"] + 311
    obj["update_time"] += 99999999
    assert parse_motion_object(obj).capture_timing() == m.capture_timing()
    clock = ClockMap.from_capture(m, 480)
    assert clock.map(40) == m.epoch_at(40) + 480 * 60000
    assert clock.quality(40) == "device_clock"
    assert ClockMap.from_dict(clock.to_dict()).to_dict() == clock.to_dict()
    manual = clock.with_anchor(40, 10000, {"manual": True})
    assert len(manual.anchors) == 1 and manual.quality(40) == "single_anchor"
    assert manual.with_anchor(160, 10120, {}).quality(50) == "interpolated"


@pytest.mark.parametrize("version", [0, 1])
def test_legacy_formats_explicit_estimate_not_server_time(version):
    m = parse_motion_object(record(version, (20, 20, 20)))
    assert list(m.times_ms) == [0, 20, 40]
    assert ClockMap.from_capture(m).quality(20) == "legacy_estimate"


def test_unsigned_rollover_keeps_label_coordinates():
    m = parse_motion_object(record(ticks=(2**32 - 20, 0, 20)))
    assert list(m.times_ms) == [0, 20, 40]
    assert m.epoch_at(40) == m.create_time_ms + 2**32 + 20


@pytest.mark.parametrize("version", [0, 1, 2])
def test_snippet_history_does_not_double_add_parent_offset(tmp_path, version):
    p = tmp_path / "source.json"
    obj = record(version, (151, 171, 191, 311) if version == 2 else (20, 20, 20, 20))
    atomic_json(p, obj)
    motion = parse_motion_object(obj, source_path=p)
    work = SessionWork(digest_file(p))
    work.clock = ClockMap.from_capture(motion)
    label = build_label_file(work, motion, tmp_path, [], {}, selection=(20, 40))
    out = tmp_path / "label.json"
    save_label_file(out, label)
    history = load_history(out)
    assert list(history.motion.times_ms) == [20, 40]
    assert history.motion.epoch_at(20) == motion.epoch_at(20)
    assert history.motion.epoch_at(40) == motion.epoch_at(40)
    assert history.work.clock.to_dict() == work.clock.to_dict()


def test_existing_manual_clock_not_changed_by_new_capture_fields():
    work = SessionWork("a" * 64)
    work.clock = ClockMap([Anchor(0, 1000), Anchor(200, 1300)])
    before = copy.deepcopy(work.to_dict())
    restored = SessionWork.from_dict(before)
    assert restored.to_dict() == before


def test_mapping_table_excludes_automatic_origin():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    dialog = MappingDialog(ClockMap.from_capture(parse_motion_object(record())))
    assert dialog.table.rowCount() == 0
    dialog.close()
    assert app is not None


def test_index_migrates_only_imu_metadata_preserves_work_and_video(tmp_path):
    atomic_json(tmp_path / "imu.json", record())
    (tmp_path / "v.mp4").write_bytes(b"fixture")
    c = Catalog(tmp_path, stability_seconds=0)
    c.scan(now=1)
    c.index_one("imu.json", lambda *_: {"time_semantics": "unknown"}, now=2)
    c.index_one("v.mp4", lambda *_: {"camera": "A"}, now=2)
    asset = c.rows(kind="imu")[0]["asset_id"]
    atomic_json(c.work_path(asset), {"sentinel": "unchanged"})
    human = c.work_path(asset).read_bytes()
    c.close()
    c = Catalog(tmp_path, stability_seconds=0)
    assert c.rows(kind="imu")[0]["state"] == "pending"
    assert c.rows(kind="video")[0]["state"] == "ready"
    c.index_one("imu.json", SourceInspector(c.root, c.meta), now=3)
    assert c.rows(kind="imu")[0]["metadata"]["capture_timing"]["revision"] == 1
    assert c.work_path(asset).read_bytes() == human
    c.close()


@pytest.mark.parametrize("version", [0, 1, 2])
def test_real_history_widget_accepts_device_and_legacy_clock_status(tmp_path, version):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.history_window import HistoryWindow
    app = QApplication.instance() or QApplication([])
    p = tmp_path / "imu.json"
    obj = record(version, (151, 171, 191, 311) if version == 2 else (20, 20, 20, 20))
    atomic_json(p, obj)
    motion = parse_motion_object(obj, source_path=p)
    work = SessionWork(digest_file(p))
    label = build_label_file(work, motion, tmp_path, [], {}, selection=(20, 40))
    out = tmp_path / "history.json"
    save_label_file(out, label)
    window = HistoryWindow(out)
    window.future.result(timeout=10)
    window.poll_load()
    window.update_imu(20)  # Direct call, so a Qt-swallowed callback cannot hide errors.
    assert ("设备时钟" if version == 2 else "旧协议") in window.position.text()
    window.video_time(window.data.work.clock.map(40))
    window.dispose()
    assert app is not None


@pytest.mark.parametrize("manual", [False, True])
def test_workspace_load_links_capture_time_but_preserves_manual_mapping(tmp_path, manual, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.catalog import file_stamp
    from cowmata_tailring.workspace.window import MainWindow
    app = QApplication.instance() or QApplication([])
    p = tmp_path / "imu.json"
    obj = record()
    atomic_json(p, obj)
    before = p.read_bytes()
    motion = parse_motion_object(obj, source_path=p)
    window = MainWindow()
    window.board.timer.stop()
    window.source_timer.stop()
    window.save_timer.stop()
    window.catalog = Catalog(tmp_path, stability_seconds=0)
    window.catalog.scan(now=1)
    window.catalog.index_one(p.name, SourceInspector(tmp_path, window.catalog.meta), now=2)
    row = window.catalog.rows(kind="imu")[0]
    if manual:
        work = SessionWork(row["asset_id"])
        work.clock = ClockMap([Anchor(0, 5000), Anchor(160, 5170)])
        stored = work.clock.to_dict()
        atomic_json(window.catalog.work_path(row["asset_id"]), work.to_dict())
    seeks = []
    monkeypatch.setattr(window.board, "seek", seeks.append)
    window._motion_loaded((window.load_generation, row, motion, file_stamp(p), False))
    assert window.link.isChecked() and window.linked
    assert seeks and seeks[-1] == (5000 if manual else motion.epoch_at(0) + 480 * 60000)
    if manual:
        assert window.work.clock.to_dict() == stored
    else:
        assert window.work.clock.quality(0) == "device_clock"
    window.close()
    window.catalog.close()
    assert p.read_bytes() == before
    assert app is not None


def test_snippet_after_counter_wrap_keeps_unwrapped_acquisition_epoch(tmp_path):
    p = tmp_path / "wrapped.json"
    obj = record(ticks=(2**32 - 20, 0, 20, 40))
    atomic_json(p, obj)
    motion = parse_motion_object(obj, source_path=p)
    work = SessionWork(digest_file(p))
    out = tmp_path / "snippet.json"
    save_label_file(out, build_label_file(work, motion, tmp_path, [], {}, selection=(20, 40)))
    restored = load_history(out)
    assert restored.motion.epoch_at(20) == motion.create_time_ms + 2**32
    assert restored.motion.epoch_at(40) == motion.create_time_ms + 2**32 + 20
