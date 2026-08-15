from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


RUNTIME = Path(__file__).resolve().parent / "model_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from imu_behavior.causal_package import (  # noqa: E402
    frame_times_ms,
    package_prediction_intervals,
)


EVENT_CODES = (
    "STANDING_UP",
    "LYING_DOWN",
    "URINATION",
    "DEFECATION",
    "TAIL_RAISED",
    "TAIL_WAGGING",
)


class PackageCausalAdapterTests(unittest.TestCase):
    def test_frame_times_match_reference_catalog_mapping(self) -> None:
        segments = [
            {
                "start_index": 0,
                "stop_index": 4,
                "start_ms": 100,
                "stop_ms": 180,
            },
            {
                "start_index": 4,
                "stop_index": 8,
                "start_ms": 400,
                "stop_ms": 480,
            },
        ]
        actual = frame_times_ms(np.asarray([0, 2, 4, 6]), segments)
        np.testing.assert_array_equal(actual, np.asarray([100, 140, 400, 440]))

    def test_body_mapping_and_event_candidates_follow_package_contract(self) -> None:
        times = np.arange(0, 5_000, 500, dtype=np.int64)
        posture = np.tile(np.asarray([[0.95, 0.05]]), (len(times), 1))
        posture[0] = [0.05, 0.95]  # Isolated half-second flash is removed.
        walking = np.full(len(times), 0.10)
        events = np.zeros((len(times), len(EVENT_CODES)), dtype=np.float64)
        events[2:5, EVENT_CODES.index("URINATION")] = [0.90, 0.95, 0.85]
        thresholds = {"WALKING": 0.80, **{code: 0.50 for code in EVENT_CODES}}

        rows = package_prediction_intervals(
            times,
            posture,
            walking,
            events,
            EVENT_CODES,
            thresholds,
            output_hz=2.0,
        )

        body = [row for row in rows if row["layer"] == "body_state"]
        self.assertEqual([row["code"] for row in body], ["STANDING"])
        urination = next(row for row in rows if row["code"] == "URINATION")
        self.assertEqual(urination["start_ms"], 1_000)
        self.assertEqual(urination["end_ms"], 2_500)
        self.assertEqual(urination["duration_s"], 1.5)
        self.assertEqual(urination["candidate_type"], "event_candidate")
        self.assertNotIn("FEEDING", {row["code"] for row in rows})


if __name__ == "__main__":
    unittest.main()
