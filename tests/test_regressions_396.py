# ruff: noqa: F811 -- pytest fixture injection
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from test_fixes_v351 import organizer  # noqa: F401


def wait_scan(window):
    deadline = time.monotonic() + 5
    while window._discovering and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert not window._discovering


def test_close_idle_resets_all_sources_and_visible_results(organizer, tmp_path):
    organizer.add_source("video", tmp_path / "old", notify=False)
    organizer.video_root.setText(str(tmp_path / "old"))
    organizer.target.setText(str(tmp_path / "farm"))
    organizer.show()
    organizer.close()
    QApplication.processEvents()
    organizer.show()
    assert organizer.sources.rowCount() == 0
    assert organizer.video_root.text() == ""
    assert organizer.target.text() == ""
    assert organizer.model.rowCount() == 0


def test_close_active_keeps_task_running_and_sources(organizer, tmp_path):
    organizer.add_source("video", tmp_path / "active", notify=False)
    organizer._active_task = True
    organizer.show()
    organizer.close()
    QApplication.processEvents()
    assert organizer.running
    assert organizer.sources.rowCount() == 1
    assert not organizer._shutdown_timer.isActive()
    organizer._active_task = False


def test_discovery_retains_empty_known_views_after_prior_moves(tmp_path):
    from cowmata_tailring.workspace.video_discovery import discover

    root = tmp_path / "batch"
    for name in ("乐橙", "右1", "右2"):
        (root / name).mkdir(parents=True)
    (root / "乐橙" / "left.mp4").write_bytes(b"video")
    updates = []
    discover(root, lambda: False, updates.append)
    assert updates[-1]["counts"] == {
        str(root / "乐橙"): 1,
        str(root / "右1"): 0,
        str(root / "右2"): 0,
    }


def test_choose_replaces_sources_and_cancel_keeps_selection(organizer, tmp_path, monkeypatch):
    # Picker is an OS boundary. Real directory discovery and state transition run.
    from cowmata_tailring.workspace import native_folders

    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "视角01").mkdir(parents=True)
        (root / "视角01" / "a.mp4").write_bytes(b"a")
    organizer.set_video_root(first)
    wait_scan(organizer)
    monkeypatch.setattr(native_folders, "choose_folders", lambda *a, **k: [str(second)])
    organizer.choose_video_root()
    wait_scan(organizer)
    assert organizer._video_roots == [second]
    assert str(first) not in organizer.sources.item(0, 1).text()
    monkeypatch.setattr(native_folders, "choose_folders", lambda *a, **k: [])
    organizer.choose_video_root()
    assert organizer._video_roots == [second]


@pytest.mark.parametrize("group,prefix", [("a", "a"), ("g", "g"), ("m", "m")])
def test_each_motion_group_uses_three_separate_lanes(group, prefix):
    from cowmata_tailring.workspace.signal_panel import ReviewWaveform

    _app = QApplication.instance() or QApplication([])
    w = ReviewWaveform()
    w._series = [SimpleNamespace(key=prefix + x, name=x, values=np.array([1.0])) for x in "xyz"]
    w.set_group(group)
    assert [len(s) for _, s in w.visible_groups()] == [1, 1, 1]
    w.close()


def test_restore_another_job_updates_displayed_root(organizer, tmp_path):
    import json

    organizer.video_root.setText(str(tmp_path / "unrelated"))
    organizer._video_roots = [tmp_path / "unrelated"]
    organizer.add_source("video", tmp_path / "unrelated" / "视角01")
    job = tmp_path / "job"
    job.mkdir()
    saved = tmp_path / "old-batch" / "视角07"
    plan = dict(
        mode="import",
        target=str(tmp_path / "farm"),
        sources=[dict(kind="video", path=str(saved), camera="视角07")],
        rows=[],
    )
    (job / "plan.json").write_text(json.dumps(plan), encoding="utf8")
    organizer.load_task(job)
    assert str(tmp_path / "unrelated") not in organizer.video_root.text()
    assert str(saved) in organizer.video_root.toolTip()


def test_current_job_restore_does_not_remove_other_unselected_views(organizer, tmp_path):
    import json

    for n in range(3):
        organizer.add_source("video", tmp_path / f"view{n}", checked=n == 0, notify=False)
    job = tmp_path / "job"
    job.mkdir()
    organizer.job = job
    plan = dict(
        mode="import",
        target=str(tmp_path / "farm"),
        sources=[dict(kind="video", path=str(tmp_path / "view0"), camera="视角01")],
        rows=[],
    )
    (job / "plan.json").write_text(json.dumps(plan), encoding="utf8")
    organizer.load_task(job)
    assert organizer.sources.rowCount() == 3
