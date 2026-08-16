from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import annotation_core
from media_timeline import (
    MediaTimelineIndex,
    TimelineDiscontinuity,
    TimelineSegment,
)
from transactional_project_mixin import (
    TransactionalProjectMixin,
    _video_identity_duration_matches,
)


def _hikvision_gap_index(
    source_path: str = "hiv00022.mp4",
) -> MediaTimelineIndex:
    return MediaTimelineIndex(
        source_path=source_path,
        source_size=268_435_456,
        source_mtime_ns=1,
        first_pts_ms=76_656_587.111,
        frame_duration_ms=80.0,
        segments=(
            TimelineSegment(0.0, 968_196.0, 0.0, 968_196.0),
            TimelineSegment(
                25_005_026.889,
                25_009_367.889,
                968_196.0,
                968_196.0,
            ),
        ),
        discontinuities=(
            TimelineDiscontinuity(
                packet_index=12_113,
                previous_pts_ms=77_624_703.111,
                current_pts_ms=101_661_614.0,
                delta_ms=24_036_910.889,
                position=267_096_430,
            ),
        ),
    )


class _ProjectBase:
    def _project_model(self) -> annotation_core.Project:
        return annotation_core.Project()


class _ProjectHarness(TransactionalProjectMixin, _ProjectBase):
    def __init__(self, video_path: Path, index: MediaTimelineIndex) -> None:
        self.video_path = str(video_path)
        self.media = SimpleNamespace(
            duration_ms=lambda: int(round(index.duration_ms)),
            _timeline_index=index,
        )
        super().__init__()


class ProjectVideoIdentityTests(unittest.TestCase):
    def test_new_project_saves_both_raw_and_continuous_durations(self) -> None:
        with TemporaryDirectory() as folder:
            video_path = Path(folder) / "hiv00022.mp4"
            video_path.write_bytes(b"video-fixture")
            index = _hikvision_gap_index(str(video_path.resolve()))
            project = _ProjectHarness(video_path, index)._project_model()

        identity = project.extras["videoIdentity"]
        self.assertEqual(identity["schema"], 2)
        self.assertEqual(identity["durationMs"], 968_196)
        self.assertEqual(identity["rawDurationMs"], 25_009_368)
        self.assertEqual(identity["continuousDurationMs"], 968_196)
        self.assertTrue(identity["timelineCorrected"])

    def test_saved_public_duration_matches_corrected_hikvision_timeline(self) -> None:
        identity = {"durationMs": 968_196}

        self.assertFalse(
            _video_identity_duration_matches(identity, 25_009_368)
        )
        self.assertTrue(
            _video_identity_duration_matches(
                identity,
                25_009_368,
                _hikvision_gap_index(),
            )
        )

    def test_future_identity_can_match_raw_duration_without_timeline_scan(self) -> None:
        identity = {
            "durationMs": 968_196,
            "rawDurationMs": 25_009_368,
            "continuousDurationMs": 968_196,
        }

        self.assertTrue(
            _video_identity_duration_matches(identity, 25_009_368)
        )

    def test_future_raw_duration_rejects_same_size_wrong_chunk(self) -> None:
        identity = {
            "durationMs": 968_196,
            "rawDurationMs": 25_009_368,
            "continuousDurationMs": 968_196,
        }

        self.assertFalse(
            _video_identity_duration_matches(identity, 968_196)
        )

    def test_unrelated_duration_still_fails_with_corrected_timeline(self) -> None:
        identity = {"durationMs": 700_000}

        self.assertFalse(
            _video_identity_duration_matches(
                identity,
                25_009_368,
                _hikvision_gap_index(),
            )
        )


if __name__ == "__main__":
    unittest.main()
