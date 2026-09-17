"""Closed intervals are visible without first entering the boundary editor."""
import copy

import numpy as np
from test_action_review_v331 import observed as annotation_window  # noqa: F401
from test_annotation_pipeline_v330 import case  # noqa: F401
from test_annotation_pipeline_v330 import window as workflow_window  # noqa: F401

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.signal_panel import ReviewWaveform


def test_all_closed_drafts_appear_without_becoming_confirmed_events(request):
    window, _ = request.getfixturevalue("annotation_window")
    first = window.work.add_draft(0, window.work.clock.map(100), window.work.clock.map(400), [])
    second = window.work.add_draft(1, window.work.clock.map(600), window.work.clock.map(800), [])
    original = copy.deepcopy(window.work.to_dict())
    window.refresh_events()
    projections = window.plot.wave._events
    assert [(p['t0'], p['t1']) for p in projections] == [(100, 400), (600, 800)]
    assert all(p['confirmation'] == 'video_draft' for p in projections)
    assert len({p['id'] for p in projections}) == 2
    window.select_plot_event(projections[1]['id'])
    assert window.selected_entry() == ('draft', second['id'])
    window.edit_plot_event(projections[0]['id'], 150, 350)
    assert first['reference_start'] == window.work.clock.map(150)
    assert first['reference_end'] == window.work.clock.map(350)
    assert second == original['drafts'][1]
    assert not window.work.project.events
    second_id = projections[1]['id']
    window.work.drafts.remove(first)
    window.refresh_events()
    assert window.plot.wave._events[0]['id'] == second_id, 'deletion must not remap a selected draft ID'


def test_unselected_interval_fills_only_its_time_range(request):
    request.getfixturevalue("annotation_window")
    wave = ReviewWaveform()
    wave.resize(900, 230)
    ts = np.arange(0, 10001, 20)
    wave.set_data([PlotSeries('ax', 'AX', 'g', '#159c8d', ts, np.zeros(len(ts)))], 10000)
    wave.set_view(0, 10000)
    wave.show()
    try:
        before = wave.grab().toImage()
        labels = [{'name': '区间', 'color': '#c05650'}]
        wave.set_events(labels, [{'id': 2, 'li': 0, 't0': 2000, 't1': 6000}])
        after = wave.grab().toImage()
        y = int(wave._plot_rect().top() + 20)
        inside = round(wave._x_for_time(4000))
        outside = round(wave._x_for_time(7000))
        assert before.pixelColor(inside, y) != after.pixelColor(inside, y)
        assert before.pixelColor(outside, y) == after.pixelColor(outside, y)
        wave.set_events(labels, [{'id': 3, 'li': 0, 't0': 2000, 't1': None}])
        point = wave.grab().toImage()
        assert point.pixelColor(inside, y) == before.pixelColor(inside, y), 'point events must not fabricate a duration'
        # Shade respects the visible window when zoomed into the middle of an
        # interval and never paints across the left-side signal names.
        wave.set_events(labels, [{'id': 2, 'li': 0, 't0': 2000, 't1': 6000}])
        wave.set_view(3000, 5000)
        zoom = wave.grab().toImage()
        assert zoom.pixelColor(inside, y) != before.pixelColor(inside, y)
        assert zoom.pixelColor(5, y) == before.pixelColor(5, y)
        cached = wave._static_cache.cacheKey()
        wave.set_playhead(4000)
        wave.grab()
        assert wave._static_cache.cacheKey() == cached, 'playback must reuse interval shading rather than repaint every label'
    finally:
        wave.close()
