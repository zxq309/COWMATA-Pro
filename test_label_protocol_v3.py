from __future__ import annotations

import csv
import io
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import annotation_core
from defaults import DEFAULT_LABELS, DEFAULT_PROTOCOL
from ultimate_v7_window import MainWindow


EXPECTED_LABELS = [
    ("1", "站立", "STANDING", "body_state"),
    ("2", "躺卧", "LYING", "body_state"),
    ("3", "行走", "WALKING", "body_state"),
    ("4", "采食", "FEEDING", "body_state"),
    ("5", "其他", "OTHER", "body_state"),
    ("Q", "抬尾", "TAIL_RAISED", "tail_action"),
    ("W", "甩尾", "TAIL_WAGGING", "tail_action"),
    ("E", "起立过程", "STANDING_UP", "posture_transition"),
    ("R", "卧倒过程", "LYING_DOWN", "posture_transition"),
    ("A", "排尿", "URINATION", "objective_event"),
    ("S", "排便", "DEFECATION", "objective_event"),
]


class LabelProtocolV3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_default_protocol_has_exact_eleven_interval_labels(self) -> None:
        main_labels = [
            item for item in DEFAULT_LABELS
            if item.get("code") != "SYNC_ANCHOR"
        ]

        self.assertEqual(DEFAULT_PROTOCOL, "v3")
        self.assertEqual(
            [
                (item["key"], item["name"], item["code"], item["layer"])
                for item in main_labels
            ],
            EXPECTED_LABELS,
        )
        self.assertTrue(all(item["type"] == "interval" for item in main_labels))
        self.assertEqual(
            [item["code"] for item in DEFAULT_LABELS if item.get("type") == "point"],
            ["SYNC_ANCHOR"],
        )

    def test_exports_do_not_include_legacy_subtype_columns(self) -> None:
        project = annotation_core.Project(
            protocol="v3",
            source={"durationMs": 10_000},
            labels=annotation_core.default_labels(),
        )
        event_header = next(csv.reader(io.StringIO(annotation_core.build_events_csv(project))))
        sample_header = next(
            csv.reader(
                io.StringIO(
                    annotation_core.build_sample_multihot_csv(project, [0, 1_000])
                )
            )
        )

        self.assertNotIn("subtype", event_header)
        self.assertNotIn("objective_event__EXCRETION__URINATION", sample_header)
        self.assertIn("posture_transition__STANDING_UP", sample_header)
        self.assertIn("posture_transition__LYING_DOWN", sample_header)
        self.assertIn("objective_event__URINATION", sample_header)
        self.assertIn("objective_event__DEFECATION", sample_header)

    def test_body_states_switch_while_transition_remains_independent(self) -> None:
        window = MainWindow()
        window._autosave = lambda: None
        window.data = SimpleNamespace(duration_ms=10_000.0, times_ms=[])
        try:
            window.playhead_ms = 1_000.0
            window._label_shortcut(0)
            window.playhead_ms = 2_000.0
            window._label_shortcut(2)
            window.playhead_ms = 2_100.0
            window._label_shortcut(7)

            self.assertEqual(window.events[0]["label_code"], "STANDING")
            self.assertIn(2, window.pending_intervals)
            self.assertIn(7, window.pending_intervals)

            window.playhead_ms = 2_500.0
            window._label_shortcut(7)

            self.assertEqual(window.events[-1]["label_code"], "STANDING_UP")
            self.assertIn(2, window.pending_intervals)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
