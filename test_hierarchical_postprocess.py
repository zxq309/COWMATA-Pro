from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


RUNTIME = Path(__file__).resolve().parent / "model_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from imu_behavior.postprocess import (  # noqa: E402
    postprocess_hierarchical_predictions,
    postprocess_predictions,
    run_posture_state_machine,
)
from imu_behavior.schema import EVENT_CLASSES  # noqa: E402


class HierarchicalPostprocessTests(unittest.TestCase):
    def test_legacy_feeding_is_mapped_to_standing_and_never_emitted(self) -> None:
        times = np.arange(8, dtype=np.int64) * 500
        body = np.tile([0.90, 0.04, 0.04, 0.02], (len(times), 1))
        events = np.zeros((len(times), len(EVENT_CLASSES)), dtype=np.float32)

        rows = postprocess_predictions(
            times, body, events, [0.5] * len(EVENT_CLASSES), 2.0
        )

        self.assertEqual([row["code"] for row in rows], ["STANDING"])
        self.assertNotIn("FEEDING", {row["code"] for row in rows})

    def test_known_initial_state_stays_upright_until_lying_is_confirmed(self) -> None:
        times = np.arange(8, dtype=np.int64) * 500
        posture = np.tile([0.05, 0.95], (len(times), 1))
        zeros = np.zeros(len(times), dtype=np.float32)

        states = run_posture_state_machine(times, posture, zeros, zeros)

        self.assertTrue(np.all(states[:4] == 0))
        self.assertTrue(np.all(states[4:] == 1))

    def test_walking_overlays_upright_as_mutually_exclusive_export(self) -> None:
        times = np.arange(8, dtype=np.int64) * 500
        body = np.tile([0.05, 0.05, 0.10, 0.80], (len(times), 1))
        events = np.zeros((len(times), len(EVENT_CLASSES)), dtype=np.float32)

        rows = postprocess_predictions(
            times, body, events, [0.5] * len(EVENT_CLASSES), 2.0
        )

        self.assertEqual([row["code"] for row in rows], ["WALKING"])
        self.assertEqual(rows[0]["source_head"], "locomotion")

    def test_research_events_are_candidates_but_not_preselected(self) -> None:
        times = np.arange(8, dtype=np.int64) * 500
        posture = np.tile([0.95, 0.05], (len(times), 1))
        walking = np.zeros(len(times), dtype=np.float32)
        event_codes = ("DEFECATION", "URINATION")
        events = np.zeros((len(times), len(event_codes)), dtype=np.float32)
        events[:, 0] = 0.90

        rows = postprocess_hierarchical_predictions(
            times,
            posture,
            walking,
            events,
            event_codes,
            {"DEFECATION": 0.5, "URINATION": 0.5},
            2.0,
        )
        defecation = next(row for row in rows if row["code"] == "DEFECATION")

        self.assertEqual(defecation["model_support"], "research")
        self.assertFalse(defecation["review_recommended"])


if __name__ == "__main__":
    unittest.main()
