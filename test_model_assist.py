from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BOVINE_NO_MEDIA", "1")

import model_assist
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from integrated_window import MainWindow as IntegratedWindow
from interactive_plot import InteractiveSignalPlotWidget
from model_assist import (
    ModelAssistMixin,
    PredictionEditDialog,
    PredictionReviewDialog,
    adjust_prediction_row,
    interval_iou,
    intervals_overlap,
    json_safe,
    prediction_fingerprint,
)
from widgets import PlotSeries


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

    def test_human_adjustment_preserves_original_prediction_and_history(self) -> None:
        original = {
            "code": "URINATION",
            "label": "排尿",
            "layer": "objective_event",
            "start_ms": 10_000.0,
            "end_ms": 14_000.0,
            "confidence_mean": 0.91,
        }
        adjusted = adjust_prediction_row(
            original,
            code="DEFECATION",
            label="排便",
            layer="objective_event",
            start_ms=10_500.0,
            end_ms=13_500.0,
            edited_at="2026-08-15T12:00:00+08:00",
        )

        self.assertEqual(adjusted["code"], "DEFECATION")
        self.assertEqual(adjusted["duration_s"], 3.0)
        self.assertTrue(adjusted["human_adjusted"])
        self.assertEqual(adjusted["prediction_original_code"], "URINATION")
        self.assertEqual(adjusted["prediction_original_start_ms"], 10_000.0)
        self.assertEqual(adjusted["prediction_original_end_ms"], 14_000.0)
        self.assertEqual(len(adjusted["prediction_adjustment_history"]), 1)
        self.assertEqual(
            adjusted["prediction_adjustment_history"][0]["updated"]["start_ms"],
            10_500.0,
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
        dialog = PredictionReviewDialog(predictions, located.append)
        try:
            dialog.table.sortItems(7, Qt.SortOrder.DescendingOrder)
            first = dialog._prediction_for_table_row(0)
            self.assertIs(first, predictions[1])
            dialog._locate_row(0, 7)
            self.assertEqual(located, [300.0])

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

    def test_edit_dialog_returns_label_and_millisecond_boundaries(self) -> None:
        prediction = {
            "code": "URINATION",
            "label": "排尿",
            "start_ms": 1_000.0,
            "end_ms": 4_000.0,
        }
        options = [
            ("URINATION", "排尿", "objective_event"),
            ("DEFECATION", "排便", "objective_event"),
        ]
        dialog = PredictionEditDialog(prediction, options, duration_ms=10_000.0)
        try:
            dialog.label_combo.setCurrentIndex(1)
            dialog.start_spin.setValue(1.234)
            dialog.end_spin.setValue(3.456)
            self.assertEqual(
                dialog.values(),
                ("DEFECATION", "排便", "objective_event", 1_234.0, 3_456.0),
            )
        finally:
            dialog.close()

    def test_review_dialog_edits_selected_suggestion_before_import(self) -> None:
        prediction = {
            "code": "URINATION",
            "label": "排尿",
            "layer": "objective_event",
            "start_ms": 1_000.0,
            "end_ms": 4_000.0,
            "confidence_mean": 0.90,
            "confidence_max": 0.95,
            "_recommended": True,
        }
        dialog = PredictionReviewDialog([prediction], lambda _value: None, duration_ms=10_000.0)
        try:
            dialog.table.selectRow(0)

            class AcceptedEdit:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def exec(self):
                    return QDialog.DialogCode.Accepted

                def values(self):
                    return (
                        "DEFECATION",
                        "排便",
                        "objective_event",
                        1_500.0,
                        3_500.0,
                    )

            with patch.object(model_assist, "PredictionEditDialog", AcceptedEdit):
                dialog._edit_selected()

            self.assertEqual(prediction["code"], "DEFECATION")
            self.assertEqual(
                (prediction["start_ms"], prediction["end_ms"]),
                (1_500.0, 3_500.0),
            )
            self.assertEqual(prediction["prediction_original_code"], "URINATION")
            self.assertTrue(prediction["human_adjusted"])
            self.assertEqual(dialog.selected_predictions(), [prediction])
        finally:
            dialog.close()


class WaveformPredictionAdjustmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _plot() -> tuple[InteractiveSignalPlotWidget, dict]:
        plot = InteractiveSignalPlotWidget()
        plot.resize(1_000, 600)
        times = np.arange(0.0, 120_001.0, 20.0)
        plot.set_data(
            [
                PlotSeries(
                    "acc_x",
                    "加速度 X",
                    "m/s²",
                    "#e86c62",
                    times,
                    np.sin(times / 1_000.0),
                )
            ],
            120_000.0,
        )
        event = {"id": 7, "li": 0, "t0": 50_000.0, "t1": 60_000.0}
        plot.set_events(
            [{"name": "排尿", "color": "#2676d9"}],
            [event],
        )
        return plot, event

    def test_selected_interval_focuses_and_exposes_waveform_boundaries(self) -> None:
        plot, event = self._plot()
        try:
            self.assertTrue(plot.focus_event(7))
            self.assertEqual(plot._selected_event_id, 7)
            self.assertEqual(plot.view_range, (40_000.0, 70_000.0))
            y = plot._plot_rect().center().y()
            left = plot._selected_boundary_hit(
                plot._x_for_time(float(event["t0"])), y
            )
            right = plot._selected_boundary_hit(
                plot._x_for_time(float(event["t1"])), y
            )
            self.assertEqual(left, (event, "left"))
            self.assertEqual(right, (event, "right"))
        finally:
            plot.close()

    def test_waveform_boundary_drag_changes_selected_interval(self) -> None:
        plot, event = self._plot()
        changed: list[tuple[int, float, object]] = []
        plot.eventChanged.connect(
            lambda event_id, start, end: changed.append((event_id, start, end))
        )
        plot.focus_event(7)
        plot.show()
        QApplication.processEvents()
        try:
            y = int(plot._plot_rect().center().y())
            start = QPoint(int(plot._x_for_time(50_000.0)), y)
            target = QPoint(int(plot._x_for_time(52_000.0)), y)
            QTest.mousePress(plot, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(plot, target)
            QTest.mouseRelease(plot, Qt.MouseButton.LeftButton, pos=target)
            QApplication.processEvents()
            self.assertAlmostEqual(float(event["t0"]), 52_000.0, delta=100.0)
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0][0], 7)
        finally:
            plot.close()

    def test_waveform_boundary_cannot_collapse_interval(self) -> None:
        plot, event = self._plot()
        plot.focus_event(7)
        plot.show()
        QApplication.processEvents()
        try:
            y = int(plot._plot_rect().center().y())
            start = QPoint(int(plot._x_for_time(50_000.0)), y)
            beyond_end = QPoint(int(plot._x_for_time(65_000.0)), y)
            QTest.mousePress(plot, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(plot, beyond_end)
            QTest.mouseRelease(plot, Qt.MouseButton.LeftButton, pos=beyond_end)
            QApplication.processEvents()
            self.assertLess(float(event["t0"]), float(event["t1"]))
        finally:
            plot.close()


class ManualIntervalWaveformDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_selecting_manual_interval_displays_waveform_boundaries(self) -> None:
        window = IntegratedWindow()
        try:
            urination_index = next(
                index
                for index, label in enumerate(window.labels)
                if label["code"] == "URINATION"
            )
            times = np.arange(0.0, 20_001.0, 20.0)
            window.data = SimpleNamespace(duration_ms=20_000.0, times_ms=times)
            window.plot.set_data(
                [
                    PlotSeries(
                        "acc_x",
                        "加速度 X",
                        "m/s²",
                        "#e86c62",
                        times,
                        np.sin(times / 1_000.0),
                    )
                ],
                20_000.0,
            )
            manual = {
                "id": 31,
                "li": urination_index,
                "label_code": "URINATION",
                "layer": "objective_event",
                "t0": 5_000.0,
                "t1": 9_000.0,
                "note": "人工标注",
            }
            window.events = [manual]
            located: list[float] = []
            window.set_playhead = lambda value, *_args, **_kwargs: located.append(
                float(value)
            )
            window._refresh_events()

            window.event_table.selectRow(0)
            QApplication.processEvents()

            self.assertEqual(window.selected_event_id, 31)
            self.assertEqual(window.plot._selected_event_id, 31)
            self.assertIs(window.plot._selected_event(), manual)
            self.assertTrue(window.model_waveform_adjust_action.isEnabled())
            self.assertEqual(
                window.model_waveform_adjust_action.text(), "调整选中区间"
            )
            y = window.plot._plot_rect().center().y()
            self.assertEqual(
                window.plot._selected_boundary_hit(
                    window.plot._x_for_time(5_000.0), y
                ),
                (manual, "left"),
            )
            self.assertEqual(
                window.plot._selected_boundary_hit(
                    window.plot._x_for_time(9_000.0), y
                ),
                (manual, "right"),
            )
            self.assertEqual(located, [5_000.0])
        finally:
            window.close()


class ImportedPredictionEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_imported_prediction_can_change_label_and_boundaries(self) -> None:
        window = IntegratedWindow()
        try:
            urination_index = next(
                index
                for index, label in enumerate(window.labels)
                if label["code"] == "URINATION"
            )
            window.data = SimpleNamespace(
                duration_ms=20_000.0,
                times_ms=np.arange(0.0, 20_001.0, 20.0),
            )
            window.events = [
                {
                    "id": 1,
                    "li": urination_index,
                    "label_code": "URINATION",
                    "layer": "objective_event",
                    "t0": 5_000.0,
                    "t1": 9_000.0,
                    "note": "[模型建议·已人工复核]",
                    "prediction_source": "imu_model",
                    "prediction_review_status": "reviewed",
                    "prediction_reviewed_at": "2026-08-15T12:00:00+08:00",
                }
            ]
            window.selected_event_id = 1
            window._autosave = lambda: None
            located: list[float] = []
            window.set_playhead = lambda value, *_args, **_kwargs: located.append(
                float(value)
            )

            class AcceptedEdit:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                def exec(self):
                    return QDialog.DialogCode.Accepted

                def values(self):
                    return (
                        "DEFECATION",
                        "排便",
                        "objective_event",
                        5_500.0,
                        8_500.0,
                    )

            with patch.object(model_assist, "PredictionEditDialog", AcceptedEdit):
                window.edit_selected_prediction()

            event = window.events[0]
            self.assertEqual(event["label_code"], "DEFECATION")
            self.assertEqual((event["t0"], event["t1"]), (5_500.0, 8_500.0))
            self.assertEqual(event["prediction_original_code"], "URINATION")
            self.assertTrue(event["prediction_human_adjusted"])
            self.assertEqual(len(event["prediction_adjustment_history"]), 1)
            self.assertIn("人工调整预测标签/边界", event["note"])
            self.assertEqual(event["prediction_review_status"], "pending")
            self.assertIn("模型建议·待人工复核", event["note"])
            self.assertNotIn("prediction_reviewed_at", event)
            self.assertEqual(located, [5_500.0])
        finally:
            window.close()

    def test_dragging_prediction_boundary_records_adjustment_history(self) -> None:
        window = IntegratedWindow()
        try:
            urination_index = next(
                index
                for index, label in enumerate(window.labels)
                if label["code"] == "URINATION"
            )
            original = {
                "id": 2,
                "li": urination_index,
                "label_code": "URINATION",
                "layer": "objective_event",
                "t0": 5_000.0,
                "t1": 9_000.0,
                "note": "[模型建议·待人工复核]",
                "prediction_source": "imu_model",
                "prediction_review_status": "pending",
            }
            window.data = SimpleNamespace(
                duration_ms=20_000.0,
                times_ms=np.arange(0.0, 20_001.0, 20.0),
            )
            window.events = [{**original, "t0": 5_500.0, "t1": 8_500.0}]
            window.selected_event_id = 2
            window._drag_history_snapshot = {
                "labels": window.labels,
                "events": [original],
                "next_event_id": window.next_event_id,
                "selected_label": window.selected_label,
                "selected_event_id": 2,
                "pending_intervals": {},
            }
            window._autosave = lambda: None

            window._event_changed_on_plot(2, 5_500.0, 8_500.0)

            event = window.events[0]
            self.assertTrue(event["prediction_human_adjusted"])
            self.assertEqual(event["prediction_original_start_ms"], 5_000.0)
            self.assertEqual(event["prediction_original_end_ms"], 9_000.0)
            self.assertEqual(len(event["prediction_adjustment_history"]), 1)
            self.assertIn("人工调整预测边界", event["note"])
        finally:
            window.close()

    def test_conflicting_body_boundary_drag_is_reverted(self) -> None:
        window = IntegratedWindow()
        try:
            standing_index = next(
                index
                for index, label in enumerate(window.labels)
                if label["code"] == "STANDING"
            )
            original = {
                "id": 10,
                "li": standing_index,
                "label_code": "STANDING",
                "layer": "body_state",
                "t0": 5_000.0,
                "t1": 9_000.0,
                "note": "[模型建议·待人工复核]",
                "prediction_source": "imu_model",
                "prediction_review_status": "pending",
            }
            window.data = SimpleNamespace(
                duration_ms=20_000.0,
                times_ms=np.arange(0.0, 20_001.0, 20.0),
            )
            window.events = [
                {**original, "t1": 11_000.0},
                {
                    "id": 11,
                    "li": standing_index,
                    "label_code": "STANDING",
                    "layer": "body_state",
                    "t0": 10_000.0,
                    "t1": 15_000.0,
                },
            ]
            window.selected_event_id = 10
            window._drag_history_snapshot = {
                "labels": window.labels,
                "events": [
                    original,
                    window.events[1].copy(),
                ],
                "next_event_id": window.next_event_id,
                "selected_label": window.selected_label,
                "selected_event_id": 10,
                "pending_intervals": {},
            }
            window._autosave = lambda: None

            with patch.object(model_assist.QMessageBox, "warning") as warning:
                window._event_changed_on_plot(10, 5_000.0, 11_000.0)

            self.assertEqual(window.events[0]["t1"], 9_000.0)
            self.assertNotIn("prediction_adjustment_history", window.events[0])
            warning.assert_called_once()
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
