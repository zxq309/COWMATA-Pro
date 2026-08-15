from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BOVINE_NO_MEDIA", "1")

import model_assist
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from model_assist import (
    ModelAssistMixin,
    PredictionReviewDialog,
    interval_iou,
    intervals_overlap,
    json_safe,
    prediction_fingerprint,
)
from video_index import VideoIndexEntry


class ModelAssistPureTests(unittest.TestCase):
    def test_fingerprint_is_stable_at_millisecond_precision(self) -> None:
        row = {"code": "walking", "start_ms": 10.4, "end_ms": 99.6}
        self.assertEqual(prediction_fingerprint(row), "WALKING:10:100")

    def test_touching_intervals_do_not_overlap(self) -> None:
        self.assertFalse(intervals_overlap(0, 1000, 1000, 2000))
        self.assertTrue(intervals_overlap(0, 1001, 1000, 2000))

    def test_interval_iou_handles_shifted_boundaries(self) -> None:
        self.assertAlmostEqual(interval_iou(0, 1000, 100, 1100), 900 / 1100)
        self.assertEqual(interval_iou(0, 1000, 1000, 2000), 0.0)

    def test_json_safe_replaces_non_finite_values(self) -> None:
        self.assertEqual(
            json_safe({"nan": float("nan"), "inf": float("inf"), "ok": 1.5}),
            {"nan": None, "inf": None, "ok": 1.5},
        )

    def test_prediction_cache_accepts_non_finite_calibration_metrics(self) -> None:
        class StatusBar:
            def showMessage(self, *_args) -> None:
                pass

        class FakeAssist:
            def __init__(self, source: Path, model: Path) -> None:
                self.data_path = str(source)
                self.model = model

            def _configured_model_path(self) -> Path:
                return self.model

            def statusBar(self) -> StatusBar:
                return StatusBar()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.json"
            model = root / "best.pt"
            source.write_text("{}", encoding="utf-8")
            model.write_bytes(b"model")
            fake = FakeAssist(source, model)
            with patch.object(model_assist, "PREDICTION_CACHE_DIR", root / "cache"):
                output = ModelAssistMixin._save_prediction_cache(
                    fake,
                    {
                        "model": str(model),
                        "algorithm": "test-v2",
                        "calibration": {"nll": float("nan")},
                        "prediction_intervals": [],
                    },
                )
            self.assertIsNotNone(output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsNone(payload["calibration"]["nll"])
            self.assertEqual(len(payload["cache_key"]), 12)


class PredictionReviewSortingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_confidence_sort_keeps_selection_and_location_bound_to_prediction(self) -> None:
        predictions = [
            {
                "code": "STANDING",
                "label": "站立",
                "layer": "body_state",
                "start_ms": 100.0,
                "end_ms": 200.0,
                "confidence_mean": 0.10,
                "confidence_max": 0.20,
                "_recommended": False,
            },
            {
                "code": "LYING",
                "label": "躺卧",
                "layer": "body_state",
                "start_ms": 300.0,
                "end_ms": 400.0,
                "confidence_mean": 0.95,
                "confidence_max": 0.99,
                "_recommended": True,
            },
            {
                "code": "WALKING",
                "label": "行走",
                "layer": "body_state",
                "start_ms": 500.0,
                "end_ms": 600.0,
                "confidence_mean": 0.50,
                "confidence_max": 0.60,
                "_recommended": True,
            },
        ]
        located: list[float] = []
        smart_located: list[dict] = []
        dialog = PredictionReviewDialog(
            predictions,
            located.append,
            smart_locate_callback=smart_located.append,
        )
        try:
            dialog.table.sortItems(7, Qt.SortOrder.DescendingOrder)
            first = dialog._prediction_for_table_row(0)
            self.assertIs(first, predictions[1])
            dialog._locate_row(0, 7)
            self.assertEqual(located, [300.0])
            dialog._smart_locate_row(0, 7)
            self.assertEqual(smart_located, [predictions[1]])

            dialog._set_all(False)
            dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
            self.assertEqual(dialog.selected_predictions(), [predictions[1]])

            dialog.table.sortItems(7, Qt.SortOrder.AscendingOrder)
            self.assertEqual(dialog.selected_predictions(), [predictions[1]])
            dialog._select_recommended()
            self.assertEqual(
                {item["code"] for item in dialog.selected_predictions()},
                {"LYING", "WALKING"},
            )
        finally:
            dialog.close()


class IndexedVideoOpenTests(unittest.TestCase):
    def test_source_switch_uses_index_mapping_and_requested_review_target(
        self,
    ) -> None:
        class StatusBar:
            def __init__(self) -> None:
                self.message = ""

            def showMessage(self, message: str, _duration: int) -> None:
                self.message = message

        class FakeWindow:
            def __init__(self) -> None:
                self.video_path = str(Path("old.mp4").resolve())
                self.video_start_wall_ms = 0
                self.align_method = "continuation"
                self._source_switch_active = False
                self._source_switch_kind = ""
                self._source_switch_snapshot = {}
                self._source_switch_mapping_committed = False
                self._timelines_linked = False
                self.playhead = 0.0
                self.status = StatusBar()

            def open_video(self, path: str) -> None:
                self.video_path = str(Path(path).resolve())
                self._source_switch_active = True
                self._source_switch_kind = "video"
                self._source_switch_snapshot = {
                    "data_ms": 1000.0,
                    "linked": False,
                }
                self._source_switch_mapping_committed = True

            def _sync_pin_button(self) -> None:
                pass

            def _set_timelines_linked(self, linked: bool) -> None:
                self._timelines_linked = linked

            def _update_alignment_status(self) -> None:
                pass

            def set_playhead(self, value: float, seek_video: bool) -> None:
                self.playhead = value
                self.seek_video = seek_video

            def statusBar(self) -> StatusBar:
                return self.status

        target_path = str(Path("matched.mp4").resolve())
        entry = VideoIndexEntry(
            target_path,
            123_456,
            60_000.0,
            1,
            1,
            playback_offset_ms=5_000.0,
        )
        window = FakeWindow()

        opened = ModelAssistMixin._open_video_index_match(
            window, entry, 45_000.0
        )

        self.assertTrue(opened)
        self.assertEqual(window.video_start_wall_ms, 118_456)
        self.assertEqual(window.align_method, "video_index")
        self.assertTrue(window._source_switch_mapping_committed)
        self.assertEqual(window._source_switch_snapshot["data_ms"], 45_000.0)
        self.assertTrue(window._source_switch_snapshot["linked"])
        self.assertEqual(window.playhead, 45_000.0)
        self.assertTrue(window.seek_video)


if __name__ == "__main__":
    unittest.main()
