# ruff: noqa: F811  (pytest fixtures shared from test_annotation_pipeline_v330)
"""4.4.4: the four problems reported against 4.4.3.

1. 标签开始 → 标签结束 → 完成: no evidence screenshot, no separate confirmation.
2. Playback continues across recording files/gaps instead of stopping.
3. The recording that really contains the IMU start is used (recorder PTS resets).
4. Frames after a recorder PTS reset decode ("无法解码所选位置真实画面").
"""
import json
import re
import subprocess
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageStat
from PySide6.QtWidgets import QApplication, QToolButton
from test_annotation_pipeline_v330 import case  # noqa: F401
from test_annotation_pipeline_v330 import window as workflow_window  # noqa: F401

from cowmata_tailring.media.dahua_stream import _encode_mpeg_timestamp
from cowmata_tailring.media.native_ps import native_hint, native_runs, native_wall_at, native_wall_end, read_native_index
from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
from cowmata_tailring.workspace.adaptive_playback import AdaptiveVideoBoard
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.clocks import (
    Anchor,
    ClockMap,
    VideoInterval,
    VideoTimeline,
    manual_video_metadata,
    recorder_confirms,
    wall_ms,
)
from cowmata_tailring.workspace.demand import coverage_index, coverage_state, pending_near
from cowmata_tailring.workspace.playback import VideoBoard
from cowmata_tailring.workspace.video_filename import named_intervals
from cowmata_tailring.workspace.worker import IndexWorker

PACK = bytes.fromhex("000001ba440004000401001003f8")
RESET_GAP_MS = 12_500


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def shenmo_pes(pts90, wall_raw_ms, payload):
    """One Shenmo video PES: PTS plus the recorder's per-frame epoch clock."""
    optional = _encode_mpeg_timestamp(pts90, 2) + b"\x01\x89\x80" + int(wall_raw_ms).to_bytes(8, "little")
    body = bytes((0x80, 0x81, len(optional))) + optional + payload
    return b"\0\0\1\xe0" + len(body).to_bytes(2, "big") + body


def local_raw(text):
    """Recorder epoch (UTC) for a local UTC+8 calendar time."""
    return wall_ms(text) - 8 * 3600_000


def write_reset_file(path, units, *, start_text="2026-09-06 13:32:30", reset_at, first_pts90=180_000,
                     frame_ms=1000 / 15, gap_ms=RESET_GAP_MS, back_ms=0):
    """A recorder file whose stream PTS restarts at unit ``reset_at`` (camera reconnect)."""
    start = local_raw(start_text) + 300
    out = []
    for k, payload in enumerate(units):
        if k < reset_at:
            pts90 = first_pts90 + round(k * frame_ms * 90)
            wall = start + round(k * frame_ms)
        else:
            pts90 = round((k - reset_at) * frame_ms * 90)
            wall = start + round(k * frame_ms) + gap_ms - back_ms
        out.append(PACK + shenmo_pes(pts90, wall, payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(out))
    return path


def fake_units(count, keys):
    return [(b"\0\0\0\1\x67" if k in keys else b"\0\0\1\x02") + b"test" for k in range(count)]


# --- 3/4: the recorder clock across a PTS reset -----------------------------

def test_pts_reset_becomes_two_runs_on_the_recorder_clock(tmp_path):
    path = write_reset_file(tmp_path / "2026-09-06_13-32-30.mp4", fake_units(20, {0, 10}), reset_at=10,
                            frame_ms=70)
    native = read_native_index(path)
    runs = native_runs(native)
    assert len(runs) == 2 and native["patch_timestamps"]
    start = wall_ms("2026-09-06 13:32:30") + 300
    assert runs[0] == (0.0, 700.0, start)
    assert runs[1][:2] == (700.0, 1400.0) and runs[1][2] == start + 700 + RESET_GAP_MS
    assert native["duration_ms"] == 1400 and native["wall_start"] == start
    # Media time stays contiguous for decoders; wall time keeps the real 12.5 s gap.
    assert [k[0] for k in native["keys"]] == [0, 700]
    assert native_wall_at(native, 750) == start + 700 + RESET_GAP_MS + 50
    assert native_wall_end(native) == start + 1400 + RESET_GAP_MS
    hint = native_hint(path)
    assert hint["start_ms"] == start and hint["end_ms"] >= native_wall_end(native)


def test_single_run_index_is_unchanged(tmp_path):
    path = write_reset_file(tmp_path / "a.mp4", fake_units(10, {0}), reset_at=99, frame_ms=70)
    native = read_native_index(path)
    assert "runs" not in native and "patch_timestamps" not in native
    assert native["duration_ms"] == 700 and native_runs(native) == [(0.0, 700.0, native["wall_start"])]


def test_recorder_clock_running_backwards_at_a_reset_stays_with_review(tmp_path):
    path = write_reset_file(tmp_path / "a.mp4", fake_units(20, {0, 10}), reset_at=10, frame_ms=70,
                            gap_ms=0, back_ms=5000)
    with pytest.raises(ValueError, match="不连续"):
        read_native_index(path)


def test_named_recording_keeps_each_run_at_its_own_time():
    native = {"duration_ms": 1400, "wall_start": 5000.0, "runs": [[0, 700, 5000.0], [700, 1400, 18200.0]]}
    first, second = named_intervals(1_000_000, 1400, native)
    assert (first["wall_start"], first["wall_end"], first["media_start"], first["media_end"]) == (1_000_000, 1_000_700, 0, 700)
    assert (second["wall_start"], second["media_start"], second["media_end"]) == (1_013_200, 700, 1400)
    assert first["verified"] and second["verified"]
    assert named_intervals(10, 50) == [dict(wall_start=10, wall_end=60, media_start=0.0, media_end=50.0,
                                            verified=True, warnings=[])]


def _with_runs(duration=1400):
    native = {"duration_ms": duration, "wall_start": 0.0, "frame_ms": 70.0, "first_pts_ms": 0,
              "keys": [[0, 0]], "runs": [[0, 700, 0.0], [700, 1400, 13_200.0]]}
    timeline = MediaTimelineIndex("x.mp4", 1, 1, 0, 70.0, (TimelineSegment(0, duration, 0, duration),), (), native=native)
    return {"duration_ms": duration, "timeline": timeline.to_dict(), "needs_review": False,
            "intervals": named_intervals(0, duration, native)}


def test_one_opening_reading_that_agrees_keeps_the_recorder_verified_spans():
    metadata = _with_runs()
    assert recorder_confirms(metadata, [{"media_ms": 100, "wall_ms": 400}])
    assert recorder_confirms(metadata, [{"media_ms": 800, "wall_ms": 13_200 + 100 + 900}])
    assert not recorder_confirms(metadata, [{"media_ms": 100, "wall_ms": 100 + 5000}])
    assert not recorder_confirms({**metadata, "needs_review": True}, [{"media_ms": 100, "wall_ms": 100}])


def test_manual_readings_never_bridge_a_recorder_reset():
    value = manual_video_metadata(_with_runs(), [{"media_ms": 100, "wall_ms": 100}, {"media_ms": 600, "wall_ms": 600}])
    later = [i for i in value["intervals"] if i["media_start"] >= 700]
    assert later and all(not i["verified"] for i in later)
    assert any("仅供浏览" in w for i in later for w in i["warnings"])
    assert any(i["verified"] for i in value["intervals"] if i["media_end"] <= 700)


def test_standard_mp4_export_refuses_a_recording_with_restarts(tmp_path):
    from cowmata_tailring.media.standard_video import export_standard_video

    path = write_reset_file(tmp_path / "a.mp4", fake_units(20, {0, 10}), reset_at=10, frame_ms=70)
    with pytest.raises(ValueError, match="断流重连"):
        export_standard_video(path, tmp_path / "out.mp4")


def _insert_video(catalog, name, metadata, state="ready"):
    asset = (name.encode().hex() + "0" * 64)[:64]
    with catalog.db:
        catalog.db.execute("INSERT INTO assets VALUES(?,?,?,?)", (asset, "video", json.dumps(metadata), 1.0))
        catalog.db.execute("INSERT INTO locations VALUES(?,?,?,?,?,?,1.0,1.0,'',0)",
                           (f"录像/2026-09-06/视角02/{name}", "video", json.dumps([1, 1, 1, 1]), "quick:x", asset, state))


def test_recordings_cut_short_by_4_4_3_are_reindexed_once(tmp_path):
    root = tmp_path / "farm"
    root.mkdir()
    catalog = Catalog(root)
    try:
        assert catalog.queue_recorder_clock_upgrade() == 0
        assert not (catalog.meta / "recorder-clock-upgrade.json").exists()  # nothing to judge yet
        broken = dict(format="mpeg", timeline={"native": None, "discontinuities": [{"rawStartMs": 1}]}, intervals=[])
        _insert_video(catalog, "2026-09-06_13-09-06.mp4", broken)
        _insert_video(catalog, "2026-09-06_13-32-30.mp4", dict(format="mpeg", timeline={"native": {
            "family": "shenmo-pes2-epoch", "source_size": 1, "source_mtime_ns": 1}}, intervals=[]))
        _insert_video(catalog, "2026-09-06_14-00-00.mp4", dict(format="mov,mp4", timeline={}, needs_review=True, intervals=[]))
        assert catalog.queue_recorder_clock_upgrade() == 1
        states = {r["path"].rsplit("/", 1)[-1]: r["state"] for r in catalog.rows()}
        assert states == {"2026-09-06_13-09-06.mp4": "pending", "2026-09-06_13-32-30.mp4": "ready",
                          "2026-09-06_14-00-00.mp4": "ready"}
        assert catalog.queue_recorder_clock_upgrade() == 0
    finally:
        catalog.close()


# --- 4: real H.264 across a recorder reset decodes at the requested time ------

@pytest.fixture(scope="module")
def reset_recording(tmp_path_factory):
    from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg

    try:
        ffmpeg, _ = find_ffmpeg()
    except (OSError, RuntimeError, ValueError):
        pytest.skip("FFmpeg is not available")
    base = tmp_path_factory.mktemp("ps444")
    raw = base / "source.h264"
    encoded = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                              "-i", "testsrc2=size=160x120:rate=15", "-frames:v", "60", "-c:v", "libx264",
                              "-preset", "ultrafast", "-bf", "0", "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
                              "-x264-params", "repeat-headers=1:aud=1", "-pix_fmt", "yuv420p", "-f", "h264", str(raw)],
                             capture_output=True)
    if encoded.returncode:
        pytest.skip("libx264 is not available: " + encoded.stderr.decode(errors="replace")[-200:])
    data = raw.read_bytes()
    starts = [m.start() for m in re.finditer(rb"\x00\x00\x00\x01\x09", data)]
    units = []
    for begin, end in zip(starts, starts[1:] + [len(data)]):
        unit = data[begin:end]
        cut = unit.find(b"\x00\x00\x01", 4)  # the NAL after the access-unit delimiter
        units.append(unit[cut - 1 if unit[cut - 1] == 0 else cut:])
    assert len(units) == 60 and all(len(u) < 60000 for u in units)
    path = write_reset_file(base / "录像" / "2026-09-06" / "视角02" / "2026-09-06_13-32-30.mp4", units, reset_at=30)

    def reference(frame):
        result = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(raw), "-vf",
                                 f"select=eq(n\\,{frame})", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"],
                                capture_output=True, check=True)
        return Image.open(BytesIO(result.stdout)).convert("RGB")

    return SimpleNamespace(path=path, root=base, reference=reference)


def _timeline(path, native):
    stat = path.stat()
    duration = native["duration_ms"]
    return MediaTimelineIndex(str(path), stat.st_size, stat.st_mtime_ns, native["first_pts_ms"], native["frame_ms"],
                              (TimelineSegment(0, duration, 0, duration),), (), native=native)


@pytest.mark.parametrize("frame", [10, 31, 40, 59])
def test_frames_after_a_recorder_reset_decode_at_the_requested_time(reset_recording, frame):
    from cowmata_tailring.workspace.probe import extract_frame

    native = read_native_index(reset_recording.path)
    assert len(native_runs(native)) == 2 and native["duration_ms"] == pytest.approx(4000, abs=1)
    media = frame * 1000 / 15
    image, actual = extract_frame(reset_recording.path, media, MediaTimelineIndex.from_dict(
        _timeline(reset_recording.path, native).to_dict()), image_codec="bmp")
    assert abs(actual - media) <= 40
    difference = ImageChops.difference(image, reset_recording.reference(frame)).convert("L")
    assert ImageStat.Stat(difference).mean[0] < 3


def test_named_recording_with_resets_covers_its_whole_real_time(reset_recording):
    """4.4.3 measured such files too short, so the record start fell into a fake gap."""
    from cowmata_tailring.workspace import probe
    from cowmata_tailring.workspace.clocks import intervals_from_rows

    value = probe.SourceInspector(reset_recording.root, reset_recording.root / "meta").video(reset_recording.path, "d" * 64)
    spans = value["intervals"]
    start = wall_ms("2026-09-06 13:32:30")
    assert value["duration_ms"] == pytest.approx(4000, abs=1) and not value["needs_review"]
    assert len(spans) == 2 and all(s["verified"] for s in spans)
    assert spans[0]["wall_start"] == pytest.approx(start, abs=1)  # the confirmed filename time anchors the file
    assert spans[1]["wall_start"] - spans[0]["wall_end"] == pytest.approx(RESET_GAP_MS, abs=2)
    row = {"path": "录像/2026-09-06/视角02/2026-09-06_13-32-30.mp4", "kind": "video", "asset_id": "d" * 64,
           "state": "ready", "metadata": value}
    timeline = VideoTimeline(intervals_from_rows([row]))
    later = spans[1]["wall_start"] + 1000
    interval, media = timeline.locate("视角02", later)
    assert interval.media_start == pytest.approx(2000, abs=1) and media == pytest.approx(3000, abs=1)
    assert timeline.locate("视角02", spans[0]["wall_end"] + 5000) is None
    assert timeline.next_start("视角02", spans[0]["wall_end"] + 5000) == pytest.approx(spans[1]["wall_start"])


# --- 2: playback continues across gaps and unindexed files --------------------

def span(asset, start, end, media_start=0.0, camera="A"):
    return VideoInterval(asset * 64, f"{asset}.mp4", camera, start, end, media_start, media_start + end - start, True)


def board_at(board_class, intervals, reference, cameras=("A",)):
    board = board_class()
    board.timer.stop()
    board.select(list(cameras))
    board.timeline = VideoTimeline(intervals)
    board.reference_ms = reference
    board.playing = True
    for camera, tile in board.tiles.items():
        board._position(camera, tile)
    return board


def test_playback_crosses_a_recorder_reset_gap(app):
    board = board_at(VideoBoard, [span("a", 0, 10_000), span("a", 22_500, 40_000, 10_000)], 10_050)
    notices = []
    board.notice.connect(notices.append)
    try:
        assert board._gap_camera() == "A"
        board._handle_coverage_gap("A")
        assert board.playing and board.reference_ms == 22_500
        assert board.tiles["A"].interval.media_start == 10_000
        assert "12 秒" in notices[-1] and "自动接到下一段" in notices[-1]
    finally:
        board.close()


def test_playback_waits_for_the_next_file_index_and_resumes_by_itself(app, tmp_path):
    board = board_at(VideoBoard, [span("a", 0, 10_000)], 10_200)
    board.coverage_probe = lambda camera, reference: "pending"
    requested, notices = [], []
    board.coverageNeeded.connect(lambda camera, reference: requested.append((camera, reference)))
    board.notice.connect(notices.append)
    try:
        board._handle_coverage_gap("A")
        assert not board.playing and board.coverage_wait["camera"] == "A"
        assert requested == [("A", 10_200.0)] and "自动继续播放" in notices[-1]
        board.play(True)  # pressing play while waiting keeps the automatic wait
        assert board.coverage_wait and not board.playing
        catalog = SimpleNamespace(meta=tmp_path, readonly=False, source_path=lambda p: tmp_path / p)
        board.configure(catalog, [], VideoTimeline([span("a", 0, 10_000), span("b", 11_000, 30_000)]))
        for _ in range(5):
            app.processEvents()
        # The next file (b) starts 0.8 s later: the wait ends there and playback resumes.
        assert board.coverage_wait is None and board.playing and board.reference_ms == 11_000
    finally:
        board.close()


def test_moving_elsewhere_or_waiting_too_long_ends_the_wait(app):
    board = board_at(VideoBoard, [span("a", 0, 10_000)], 10_200)
    board.coverage_probe = lambda camera, reference: "pending"
    notices = []
    board.notice.connect(notices.append)
    try:
        board._handle_coverage_gap("A")
        board.seek(10_400)  # small internal moves keep waiting
        assert board.coverage_wait
        board.seek(5_000)
        assert board.coverage_wait is None
        board.seek(10_200)
        board.playing = True
        board._position("A", board.tiles["A"])
        board._handle_coverage_gap("A")
        board.coverage_wait["since"] -= board.COVERAGE_WAIT_S + 1
        board.tick()
        assert board.coverage_wait is None and "超过 2 分钟" in notices[-1]
    finally:
        board.close()


def test_a_real_long_gap_still_stops_and_names_the_next_recording(app):
    board = board_at(VideoBoard, [span("a", 0, 10_000), span("b", 3_600_000, 3_700_000)], 10_100)
    board.coverage_probe = lambda camera, reference: None
    notices = []
    board.notice.connect(notices.append)
    try:
        board._handle_coverage_gap("A")
        assert not board.playing and board.coverage_wait is None
        assert "60 分钟" in notices[-1] and "下一录像时段" in notices[-1]
    finally:
        board.close()


def test_next_recording_is_indexed_before_the_current_one_ends(app):
    board = board_at(VideoBoard, [span("a", 0, 100_000)], 60_000)
    board.coverage_probe = lambda camera, reference: "pending" if reference > 100_000 else None
    requested = []
    board.coverageNeeded.connect(lambda camera, reference: requested.append((camera, reference)))
    try:
        board._prefetch_next_recordings()
        board._prefetch_next_recordings()  # throttled
        assert requested == [("A", 100_001.0)]
        board.reference_ms = 20_000  # more than one minute (x rate) ahead: not yet
        requested.clear()
        board._coverage_requests.clear()
        board._prefetch_next_recordings()
        assert requested == []
    finally:
        board.close()


def test_single_view_priority_follows_the_main_view_gap(app):
    board = board_at(AdaptiveVideoBoard, [span("b", 0, 100_000, camera="B")], 50_000, cameras=("A", "B"))
    try:
        board.main_camera = "A"
        board.playback_policy = "focus"
        assert board._gap_camera() == "A"
        board.playback_policy = "full"
        assert board._gap_camera() is None
    finally:
        board.close()


def _row(path, state, intervals=None, camera="视角07"):
    return {"path": path, "kind": "video", "state": state, "asset_id": None if state == "pending" else path.ljust(64, "0")[:64],
            "metadata": {"camera": camera, "intervals": intervals or []}}


def test_coverage_state_tells_an_unindexed_next_file_from_a_real_gap():
    first = _row("录像/2026-09-07/视角07/2026-09-07_04-16-55.mp4", "ready", [
        dict(wall_start=wall_ms("2026-09-07 04:16:55"), wall_end=wall_ms("2026-09-07 04:27:10"), verified=True)])
    second = _row("录像/2026-09-07/视角07/2026-09-07_04-27-12.mp4", "pending")
    index = coverage_index({"视角07": [first, second]}, {})
    assert coverage_state(index, "视角07", wall_ms("2026-09-07 04:27:11")) == "pending"
    assert coverage_state(index, "视角07", wall_ms("2026-09-07 04:30:00")) == "pending"
    assert pending_near(index, "视角07", wall_ms("2026-09-07 04:27:11")) == [second["path"]]
    second["state"] = "invalid"
    index = coverage_index({"视角07": [first, second]}, {})
    assert coverage_state(index, "视角07", wall_ms("2026-09-07 04:30:00")) == "invalid"
    index = coverage_index({"视角07": [first]}, {})
    assert coverage_state(index, "视角07", wall_ms("2026-09-07 05:00:00")) is None
    unsure = _row("录像/2026-09-07/视角07/2026-09-07_09-00-00.mp4", "review", [
        dict(wall_start=wall_ms("2026-09-07 09:00:00"), wall_end=wall_ms("2026-09-07 09:20:00"), verified=False)])
    index = coverage_index({"视角07": [first, unsure]}, {})
    assert coverage_state(index, "视角07", wall_ms("2026-09-07 05:00:00")) == "review"


def test_playback_prefetch_never_cancels_running_indexing(workflow_window):
    window = workflow_window
    calls = []
    original = window.worker
    window.worker = SimpleNamespace(request=lambda action, value=None: calls.append((action, value)))
    try:
        pending = _row("录像/2026-09-07/视角07/2026-09-07_04-27-12.mp4", "pending")
        window._coverage_index = coverage_index({"视角07": [pending]}, {})
        window.request_video_coverage("视角07", wall_ms("2026-09-07 04:27:11"))
    finally:
        window.worker = original
    assert calls == [("prefetch", [pending["path"]])]
    worker = IndexWorker(window.catalog)
    worker.request("prefetch", [pending["path"]])
    assert not worker.job_stop.is_set() and worker.commands.get_nowait() == ("prefetch", [pending["path"]])


# --- 1: label start → label end → done ------------------------------------------

def aligned(window, cow="COW-444"):
    window.work.project.cow_id = cow
    window.work.clock = ClockMap([Anchor(0, 10000), Anchor(window.motion.duration_ms, 11000)])
    window.board.main_camera = "A"
    return next(r for r in window.rows if r["kind"] == "video")


def live(window, row, reference, *, verified=True):
    return [{"camera": "A", "asset_id": row["asset_id"], "frame_ready": True, "verified_interval": verified,
             "reference_ms": reference, "media_ms": reference - 10000, "frame_source": "native_playback_clock",
             "camera_mapping_revision": "uncalibrated", "video_revision": window.video_revision(row)}]


def label_once(window, monkeypatch, start=10100, end=10500, **kw):
    row = next(r for r in window.rows if r["kind"] == "video")
    position = [start]
    monkeypatch.setattr(window, "evidence", lambda: live(window, row, position[0], **kw))
    window.mark(0)
    position[0] = end
    window.mark(0)


def test_ending_a_label_confirms_it_without_screenshot_or_second_click(workflow_window, monkeypatch):
    window = workflow_window
    aligned(window)
    monkeypatch.setattr(window, "capture_evidence", lambda **_: pytest.fail("no screenshot step in 4.4.4"))
    label_once(window, monkeypatch)
    [draft] = window.work.drafts
    [event] = window.work.project.events
    assert draft["origin"] == "marked" and draft["confirmation"] == "confirmed"
    assert event.extras["confirmation"] == "confirmed" and event.extras["draft_id"] == draft["id"]
    clock = window.work.clock
    assert abs(event.t0 - clock.map(10100, inverse=True)) < 1e-6
    assert abs(event.t1 - clock.map(10500, inverse=True)) < 1e-6
    assert event.extras["video_evidence"][0]["verified_interval"]
    assert "已完成" in window.event_status.text() and "无需截图" in window.event_status.text()
    assert window.events.rowCount() == 1  # the label, not a leftover draft
    assert window.work.undo_once()  # one undo removes the whole label
    assert window.work.project.events == [] and window.work.drafts == []


def test_label_that_cannot_be_confirmed_yet_says_why_and_stays_a_draft(workflow_window, monkeypatch):
    window = workflow_window
    aligned(window)
    window.work.clock = ClockMap([Anchor(0, 10000)], basis="device_clock")  # automatic link only
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    label_once(window, monkeypatch)
    [draft] = window.work.drafts
    assert draft["confirmation"] == "video_draft" and window.work.project.events == []
    assert "待确认草稿" in messages[-1] and "对齐" in messages[-1]
    assert window.event_status.text() == messages[-1]


def test_unverified_recording_time_is_named_when_a_label_ends(workflow_window, monkeypatch):
    window = workflow_window
    aligned(window)
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    label_once(window, monkeypatch, verified=False)
    assert window.work.project.events == [] and "view01.mp4" in messages[-1] and "核验" in messages[-1]


def test_pending_labels_complete_when_the_record_is_opened_again(workflow_window, monkeypatch):
    window = workflow_window
    row = aligned(window)
    draft = window.work.add_draft(0, 10100, 10500, live(window, row, 10100), origin="marked")
    old = window.work.add_draft(1, 10600, 10700, live(window, row, 10600))  # 4.4.3 draft: left for review
    assert window.auto_confirm_marked_drafts() == 1
    assert draft["confirmation"] == "confirmed" and old["confirmation"] == "video_draft"


def test_manual_confirmation_no_longer_opens_a_screenshot_dialog(workflow_window, monkeypatch):
    window = workflow_window
    row = aligned(window)
    draft = window.work.add_draft(0, 10100, 10200, live(window, row, 10100))
    monkeypatch.setattr(window, "evidence", lambda: live(window, row, 10150))
    monkeypatch.setattr(window, "isVisible", lambda: True)
    monkeypatch.setattr(window, "capture_evidence", lambda **_: pytest.fail("screenshot dialog opened"))
    window.board.reference_ms = 10150
    window.refresh_events(preferred=("draft", draft["id"]))
    window.events.selectRow(0)
    window.confirm_selected()
    assert window.work.project.events[0].extras["confirmation"] == "confirmed"


def test_screenshot_and_add_evidence_steps_left_the_labelling_menus(app):
    from cowmata_tailring.workspace.modern_window import MainWindow

    window = MainWindow()
    try:
        operations = next(b for b in window.findChildren(QToolButton) if b.text() == "操作")
        texts = [a.text() for a in operations.menu().actions()]
        assert "确认真值" in texts and "补充证据" not in texts and "证据截图…" not in texts
        assert "证据截图（可选）…" in [a.text() for a in window._legacy_menu.actions()]
        assert window._menu_unplaced == []
    finally:
        window.close()
        app.processEvents()


def test_release_identifies_as_4_4_4_or_later():
    from cowmata_tailring import __build__, __version__

    assert tuple(int(part) for part in __version__.split(".")) >= (4, 4, 4)
    assert __build__.startswith("annotator-" + __version__.replace(".", "") + "-")
