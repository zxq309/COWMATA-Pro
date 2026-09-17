import pytest

from cowmata_tailring.workspace.clocks import (
    Anchor,
    ClockMap,
    VideoInterval,
    VideoTimeline,
    wall_ms,
    wall_text,
)


def seg(name, start, end, camera="one", verified=True):
    return VideoInterval(name, name + ".mp4", camera, start, end, 0, end - start, verified)


def test_calendar_filename_format_roundtrip_cross_midnight():
    value = wall_ms("2026-08-03 23_59_59")
    assert wall_text(value + 2000, filename=True) == "2026-08-04 00_00_01"
    assert wall_ms(wall_text(value, filename=True)) == value


def test_anchor_drift_inverse_quality_and_revision():
    mapping = ClockMap([Anchor(0, 100), Anchor(1000, 1200), Anchor(2000, 2250)])
    assert mapping.map(500) == 650
    assert mapping.map(650, inverse=True) == 500
    assert mapping.map(1500) == 1725
    assert mapping.quality(-1) == "extrapolated"
    assert mapping.quality(500) == "interpolated"
    changed = mapping.with_anchor(3000, 3300, {"human": True})
    assert changed.revision != mapping.revision
    assert len(mapping.anchors) == 3  # Existing labels can retain old mapping.
    assert ClockMap.from_dict(mapping.to_dict()).to_dict() == mapping.to_dict()
    with pytest.raises(ValueError):
        ClockMap([Anchor(0, 1000), Anchor(10, 900)])


def test_no_implicit_source_clock_or_silent_extrapolation():
    with pytest.raises(ValueError):
        ClockMap().map(100)
    assert ClockMap([Anchor(0, 100)]).quality(10000) == "single_anchor"
    mapping = ClockMap([Anchor(0, 0), Anchor(1000, 1000)], breaks=[(400, 600)])
    assert mapping.quality(500) == "unconfirmed"


def test_overlap_nested_missing_boundary_and_independent_cameras():
    timeline = VideoTimeline([seg("long", 0, 100), seg("short", 10, 20),
                              seg("next", 100, 150), seg("gapafter", 200, 300),
                              seg("camera2", 40, 200, "two")])
    assert timeline.locate("one", 30)[0].asset_id == "long"
    assert timeline.locate("one", 15, prefer="long")[0].asset_id == "long"
    assert timeline.locate("one", 100)[0].asset_id == "next"
    assert timeline.locate("one", 175) is None
    assert timeline.next_start("one", 175) == 200
    assert timeline.locate("two", 100)[1] == 60
    assert timeline.bounds() == (0, 300)


@pytest.mark.parametrize("count", range(1, 9))
def test_one_to_eight_camera_maps_keep_reference_clock(count):
    intervals = [seg(str(i), i * 100, 10000 + i * 100, str(i)) for i in range(count)]
    mappings = {str(i): ClockMap([Anchor(i * 100, 0)]) for i in range(count)}
    timeline = VideoTimeline(intervals, mappings)
    assert len(timeline.cameras) == count
    for camera in timeline.cameras:
        assert timeline.locate(camera, 500)[1] == 500
        assert timeline.reference_time(camera, timeline.camera_time(camera, 500)) == 500
