import threading
import time

from test_fixes_v351 import organizer  # noqa: F401


def test_many_lanes_respect_global_worker_limit():
    from cowmata_tailring.workspace.video_intake import parallel_items

    active = peak = 0
    lock = threading.Lock()

    def work(item):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return item

    result = list(parallel_items(list(range(40)), work, workers=3, lane_key=lambda x: x))
    assert sorted(result) == list(range(40))
    assert 1 < peak <= 3


def test_multiple_roots_keep_distinct_view_sources_without_blocking(organizer, tmp_path):  # noqa: F811
    from PySide6.QtWidgets import QApplication

    roots = []
    for index in range(3):
        root = tmp_path / f"batch{index}"
        for view in ("视角01", "视角02"):
            folder = root / view
            folder.mkdir(parents=True)
            (folder / "a.mp4").write_bytes(b"test")
        roots.append(root)
    start = time.monotonic()
    organizer.add_video_roots(roots)
    assert time.monotonic() - start < 0.2
    deadline = time.monotonic() + 5
    while organizer._discovering and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not organizer._discovering
    assert organizer.sources.rowCount() == 6
    organizer.check_views(True)
    specs = organizer.source_specs()
    assert len(specs) == 6 and len({s["path"] for s in specs}) == 6
    organizer.add_video_roots([roots[0]])
    assert organizer.sources.rowCount() == 6


def test_visible_recording_uses_bounded_preview_without_claiming_full_decode(tmp_path):
    from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
    from cowmata_tailring.media.subprocess_tools import run_cancellable
    from cowmata_tailring.workspace.intake_health import assess_video

    path = tmp_path / "video.mp4"
    result = run_cancellable(
        [
            str(find_ffmpeg()[0]),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=25:duration=8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    assert result.returncode == 0
    report = assess_video(path, tmp_path / "cache")
    assert not report["delete_reason"]
    assert report["health_scope"] == "opening_visible"
    assert report["decoded_frames"] <= 32


def test_400_view_selection_and_specs_avoid_quadratic_path_work(organizer, tmp_path):  # noqa: F811
    # Reproduce 20 batches of 20 views without loading media.
    organizer.sources.blockSignals(True)
    for batch in range(20):
        for view in range(20):
            organizer.add_source(
                "video", tmp_path / f"b{batch}" / f"v{view}", checked=False, notify=False
            )
    organizer.sources.blockSignals(False)
    started = time.monotonic()
    organizer.check_views(True)
    specs = organizer.source_specs()
    assert len(specs) == 400
    assert time.monotonic() - started < 0.5
    assert all(not s["exclude"] for s in specs)


def test_nested_source_exclusion_is_preserved(organizer, tmp_path):  # noqa: F811
    organizer.add_source("video", tmp_path / "root", notify=False)
    organizer.add_source("video", tmp_path / "root" / "view", checked=False, notify=False)
    specs = organizer.source_specs()
    assert specs[0]["exclude"] == [str(tmp_path / "root" / "view")]


def test_many_view_progress_uses_source_identity_and_stays_bounded(organizer, tmp_path):  # noqa: F811
    organizer.sources.blockSignals(True)
    rows = []
    for batch in range(20):
        for view in range(20):
            folder = tmp_path / f'b{batch}' / f'v{view}'
            organizer.add_source('video', folder, checked=False, notify=False)
            for num in range(5):
                rows.append(dict(source=str(folder / f'{num}.mp4'), status='done'))
    organizer.sources.blockSignals(False)
    start = time.monotonic()
    organizer.refresh_view_progress(rows)
    assert time.monotonic() - start < .5
    assert '5 / 5' in organizer.sources.item(0, 3).text()
    assert '5 / 5' in organizer.sources.item(399, 3).text()


def test_resume_rejects_changed_destination_before_writing(tmp_path):
    import json

    import pytest

    from cowmata_tailring.workspace.video_intake import organize

    job = tmp_path / 'job'
    job.mkdir()
    original = dict(streaming=True, resource_root=str(tmp_path / 'old'),
                    target=str(tmp_path / 'old' / '产犊'), category='calving',
                    sources=[], rows=[])
    saved = json.dumps(original)
    (job / 'plan.json').write_text(saved)
    with pytest.raises(ValueError, match='输出目录'):
        organize(tmp_path / 'new', [], job=job, category='calving')
    assert (job / 'plan.json').read_text() == saved
    assert not (tmp_path / 'new').exists()


def test_resume_allows_added_roots_but_rejects_changed_view(tmp_path):
    import pytest

    from cowmata_tailring.workspace.organization_live import validate_resume

    root = str(tmp_path / 'farm')
    source = dict(path=str(tmp_path / 'input'), camera='视角01')
    plan = dict(resource_root=root, target=str(tmp_path / 'farm' / '产犊'),
                category='calving', sources=[source], rows=[])
    request = dict(target=root, category='calving', sources=[source,
                   dict(path=str(tmp_path / 'input2'), camera='视角20')])
    validate_resume(plan, request)
    request['sources'][0] = dict(source, camera='视角02')
    with pytest.raises(ValueError, match='视角'):
        validate_resume(plan, request)


def test_resume_rejects_changed_category_and_foreign_targets(tmp_path):
    import pytest

    from cowmata_tailring.workspace.organization_live import validate_resume

    root = str(tmp_path / 'farm')
    plan = dict(resource_root=root, target=str(tmp_path / 'farm' / '产犊'),
                category='calving', sources=[], rows=[])
    with pytest.raises(ValueError, match='类别'):
        validate_resume(plan, dict(target=root, category='estrus', sources=[]))
    plan['rows'] = [dict(target=str(tmp_path / 'other' / 'a.mp4'))]
    with pytest.raises(ValueError, match='目录以外'):
        validate_resume(plan, dict(target=root, category='calving', sources=[]))


def test_pause_restore_reuses_unchanged_view_controls(organizer, tmp_path):  # noqa: F811
    import json

    for view in range(20):
        organizer.add_source('video', tmp_path / f'view{view}', camera=f'视角{view+1:02d}', notify=False)
    original = organizer.sources.cellWidget(0, 2)
    plan = dict(mode='import', streaming=True, target=str(tmp_path / 'farm' / '产犊'),
                resource_root=str(tmp_path / 'farm'), category='calving',
                sources=organizer.source_specs(), rows=[])
    job = tmp_path / 'job'
    job.mkdir()
    (job / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
    organizer.load_task(job)
    assert organizer.sources.cellWidget(0, 2) is original
    assert organizer.sources.rowCount() == 20
