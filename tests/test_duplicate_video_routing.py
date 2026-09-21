from types import SimpleNamespace

import pytest

from cowmata_tailring.workspace.clocks import wall_ms
from cowmata_tailring.workspace.demand import next_video_task
from cowmata_tailring.workspace.worker import IndexWorker


def duplicate_rows(with_next=True):
    names = ['2026-09-13_17-55-09.mp4', '2026-09-13_17-55-09__001.mp4']
    if with_next:
        names.append('2026-09-13_18-59-57.mp4')
    return [dict(path='录像/2026-09-13/视角11/'+name, kind='video',
                 state='pending', stamp='stable', asset_id=None, metadata={}) for name in names]


def test_equal_timestamp_recordings_remain_candidates_for_middle_of_clip():
    rows = duplicate_rows(True)
    attempted = set()
    chosen = []
    for _ in range(2):
        task = next_video_task(rows, {}, wall_ms('2026-09-13 18:21:29'),
                               wall_ms('2026-09-13 18:22:29'), attempted=attempted, explore=False)
        assert task is not None, 'Equal-start archive variants must not leave covered video pending forever'
        assert task[0] == 'full'
        chosen.append(task[1]['path'].rsplit('/', 1)[-1])
        attempted.add(task[1]['path'])
    assert set(chosen) == {'2026-09-13_17-55-09.mp4', '2026-09-13_17-55-09__001.mp4'}


def test_equal_timestamp_recordings_without_next_anchor_stay_unresolved():
    rows = duplicate_rows(False)
    assert next_video_task(rows, {}, wall_ms('2026-09-13 18:21:29'),
                           wall_ms('2026-09-13 18:22:29'), explore=False) is None


def test_index_worker_schedules_same_start_recording_at_active_playhead():
    rows = duplicate_rows()
    cat = SimpleNamespace(rows=lambda: rows, video_hints=lambda: {})
    worker = IndexWorker(cat)
    worker.window = (wall_ms('2026-09-13 18:21:14'), wall_ms('2026-09-13 18:57:05'), {})
    worker.playhead = wall_ms('2026-09-13 18:21:29')
    task = worker.next_task(rows)
    assert task is not None
    assert task[0] == 'full'
    assert '17-55-09' in task[1]['path']


def test_duplicate_files_before_recording_start_do_not_invent_coverage():
    assert next_video_task(duplicate_rows(), {}, wall_ms('2026-09-13 15:10:17'),
                           wall_ms('2026-09-13 15:11:17'), explore=False) is None
