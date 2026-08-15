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
    ("1", "站立", "STANDING", "body_state", "interval"),
    ("2", "躺卧", "LYING", "body_state", "interval"),
    ("3", "行走", "WALKING", "body_state", "interval"),
    ("4", "努责首次出现", "STRAINING_ONSET", "calving_process", "point"),
    ("5", "努责区间", "STRAINING_BOUT", "calving_process", "interval"),
    (
        "6", "胎膜囊（水囊）首次可见", "AMNIOTIC_SAC_FIRST_VISIBLE",
        "calving_process", "point",
    ),
    (
        "7", "胎儿首个部位首次可见", "FETAL_PART_FIRST_VISIBLE",
        "calving_process", "point",
    ),
    ("8", "犊牛完全娩出", "CALF_FULLY_EXPELLED", "calving_process", "point"),
    (
        "9", "胎膜完全排出", "FETAL_MEMBRANES_FULLY_EXPELLED",
        "calving_process", "point",
    ),
    ("Q", "抬尾", "TAIL_RAISED", "tail_action", "interval"),
    ("W", "甩尾", "TAIL_WAGGING", "tail_action", "interval"),
    ("E", "起立过程", "STANDING_UP", "posture_transition", "interval"),
    ("R", "卧倒过程", "LYING_DOWN", "posture_transition", "interval"),
    ("A", "排尿", "URINATION", "objective_event", "interval"),
    ("S", "排便", "DEFECATION", "objective_event", "interval"),
]


class LabelProtocolV4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_default_protocol_has_exact_fifteen_labels(self) -> None:
        main_labels = [
            item for item in DEFAULT_LABELS
            if item.get("code") != "SYNC_ANCHOR"
        ]

        self.assertEqual(DEFAULT_PROTOCOL, "v4")
        self.assertEqual(
            [
                (
                    item["key"], item["name"], item["code"], item["layer"],
                    item["type"],
                )
                for item in main_labels
            ],
            EXPECTED_LABELS,
        )
        self.assertEqual(len({item["key"] for item in DEFAULT_LABELS}), 16)
        self.assertEqual(
            [item["code"] for item in DEFAULT_LABELS if item.get("type") == "point"],
            [
                "STRAINING_ONSET",
                "AMNIOTIC_SAC_FIRST_VISIBLE",
                "FETAL_PART_FIRST_VISIBLE",
                "CALF_FULLY_EXPELLED",
                "FETAL_MEMBRANES_FULLY_EXPELLED",
                "SYNC_ANCHOR",
            ],
        )
        self.assertNotIn("FEEDING", {item["code"] for item in DEFAULT_LABELS})
        self.assertNotIn("OTHER", {item["code"] for item in DEFAULT_LABELS})

    def test_exports_do_not_include_legacy_subtype_columns(self) -> None:
        project = annotation_core.Project(
            protocol="v4",
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
        self.assertIn("calving_process__STRAINING_ONSET", sample_header)
        self.assertIn("calving_process__STRAINING_BOUT", sample_header)
        self.assertIn("calving_process__CALF_FULLY_EXPELLED", sample_header)
        self.assertNotIn("body_state__FEEDING", sample_header)
        self.assertNotIn("body_state__OTHER", sample_header)

    def test_straining_point_and_bout_are_independent_of_body_state(self) -> None:
        window = MainWindow()
        window._autosave = lambda: None
        window.data = SimpleNamespace(duration_ms=10_000.0, times_ms=[])
        try:
            by_code = {
                label["code"]: index for index, label in enumerate(window.labels)
            }
            window.playhead_ms = 1_000.0
            window._label_shortcut(by_code["STANDING"])
            window.playhead_ms = 2_000.0
            window._label_shortcut(by_code["WALKING"])
            window.playhead_ms = 2_100.0
            window._label_shortcut(by_code["STRAINING_BOUT"])
            window.playhead_ms = 2_200.0
            window._label_shortcut(by_code["STRAINING_ONSET"])

            self.assertEqual(window.events[0]["label_code"], "STANDING")
            self.assertEqual(window.events[-1]["label_code"], "STRAINING_ONSET")
            self.assertIsNone(window.events[-1]["t1"])
            self.assertIn(by_code["WALKING"], window.pending_intervals)
            self.assertIn(by_code["STRAINING_BOUT"], window.pending_intervals)

            window.playhead_ms = 2_500.0
            window._label_shortcut(by_code["STRAINING_BOUT"])

            bout = next(
                event
                for event in window.events
                if event["label_code"] == "STRAINING_BOUT"
            )
            self.assertEqual((bout["t0"], bout["t1"]), (2_100.0, 2_500.0))
            self.assertIn(by_code["WALKING"], window.pending_intervals)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
