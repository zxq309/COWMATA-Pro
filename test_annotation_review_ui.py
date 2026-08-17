from __future__ import annotations

import os
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BOVINE_NO_MEDIA", "1")

from PySide6.QtWidgets import QApplication

from annotation_review import AnnotationReviewDialog
from annotation_review_core import new_workspace
from data_core import _synthetic_object
from integrated_window import MainWindow
from test_annotation_review import EVENT_COLUMNS, write_events_csv


def one_session_workspace() -> dict:
    workspace = new_workspace()
    workspace["sessions"] = [
        {
            "key": "session-key",
            "session_id": "session-001",
            "csv_path": "session-001.events.csv",
            "json_path": "session-001.json",
            "json_match_status": "resolved",
            "device": "DEV",
            "cow_id": "cow-1",
            "labels": [
                {
                    "name": "站立",
                    "code": "STANDING",
                    "layer": "body_state",
                    "type": "interval",
                }
            ],
            "events": [
                {
                    "id": 1,
                    "li": 0,
                    "label_code": "STANDING",
                    "t0": 1000.0,
                    "t1": 2000.0,
                    "review_uid": "session-key:1",
                    "review_status": "pending",
                    "review_modified": False,
                }
            ],
        }
    ]
    return workspace


class AnnotationReviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_non_modal_dialog_and_selection_controls(self) -> None:
        workspace = one_session_workspace()
        calls: list[tuple[str, str]] = []
        dialog = AnnotationReviewDialog(
            lambda: workspace,
            lambda uid: calls.append(("locate", uid)),
            lambda uid: calls.append(("edit", uid)),
            lambda uid: calls.append(("approve", uid)),
            lambda uid: calls.append(("next", uid)),
            lambda: calls.append(("root", "")),
            lambda: calls.append(("export", "")),
        )
        try:
            self.assertFalse(dialog.isModal())
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertFalse(dialog.locate_btn.isEnabled())
            self.assertFalse(dialog.edit_btn.isEnabled())
            self.assertFalse(dialog.approve_btn.isEnabled())
            self.assertTrue(dialog.next_btn.isEnabled())

            dialog.table.selectRow(0)
            dialog._refresh_controls()
            self.assertTrue(dialog.locate_btn.isEnabled())
            self.assertTrue(dialog.edit_btn.isEnabled())
            self.assertTrue(dialog.approve_btn.isEnabled())
            dialog.locate_btn.click()
            dialog.edit_btn.click()
            dialog.approve_btn.click()
            self.assertEqual(
                calls,
                [
                    ("locate", "session-key:1"),
                    ("edit", "session-key:1"),
                    ("approve", "session-key:1"),
                ],
            )
        finally:
            dialog.close()

    def test_status_filter_keeps_missing_json_visible(self) -> None:
        workspace = one_session_workspace()
        workspace["sessions"][0]["json_match_status"] = "missing"
        dialog = AnnotationReviewDialog(
            lambda: workspace,
            lambda _uid: None,
            lambda _uid: None,
            lambda _uid: None,
            lambda _uid: None,
            lambda: None,
            lambda: None,
        )
        try:
            self.assertEqual(dialog.table.item(0, 0).text(), "缺少 JSON")
            dialog.status_filter.setCurrentIndex(
                dialog.status_filter.findData("missing")
            )
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), "缺少 JSON")
        finally:
            dialog.close()


class AnnotationReviewWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_toolbar_menu_and_explicit_csv_import(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "session-001.events.csv"
            write_events_csv(csv_path)
            window = MainWindow()
            window._review_autosave_path = root / "autosave.review.json"
            window.show()
            self.app.processEvents()
            try:
                window.import_review_csvs([str(csv_path)])
                self.app.processEvents()
                self.assertIs(
                    window.main_toolbar.widgetForAction(
                        window.annotation_review_widget_action
                    ),
                    window.annotation_review_button,
                )
                self.assertIsNotNone(window._review_dialog)
                self.assertFalse(window._review_dialog.isModal())
                self.assertEqual(window._review_dialog.table.rowCount(), 2)
                self.assertIsNotNone(window._review_dock)
                self.assertFalse(window._review_dock.isFloating())
                self.assertTrue(window._review_dock.isVisible())
                self.assertTrue(window.plot.isVisible())
                self.assertGreater(window.plot.height(), 240)
                actions = set(window.annotation_review_menu.actions())
                self.assertIn(window.review_import_action, actions)
                self.assertIn(window.review_export_action, actions)
                self.assertTrue(window.review_open_action.isEnabled())
                self.assertTrue(window.review_save_action.isEnabled())
                window._review_dialog.close_btn.click()
                self.app.processEvents()
                self.assertFalse(window._review_dock.isVisible())
                window.show_annotation_review()
                self.app.processEvents()
                self.assertTrue(window._review_dock.isVisible())
            finally:
                window.close()

    def test_session_switch_syncs_edits_and_autosaves(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            json_root = root / "九轴数据"
            json_root.mkdir()

            csv_paths: list[Path] = []
            for session_id in ("session-a", "session-b"):
                csv_path = root / "标注" / f"{session_id}.events.csv"
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                row = {column: "" for column in EVENT_COLUMNS}
                row.update(
                    {
                        "index": "1",
                        "session_id": session_id,
                        "label": "站立",
                        "code": "STANDING",
                        "layer": "body_state",
                        "type": "interval",
                        "t_start_rel_ms": "0",
                        "t_end_rel_ms": "20",
                        "reviewed_start_ms": "0",
                        "reviewed_end_ms": "20",
                    }
                )
                with csv_path.open(
                    "w", encoding="utf-8-sig", newline=""
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=EVENT_COLUMNS)
                    writer.writeheader()
                    writer.writerow(row)
                (json_root / f"{session_id}.json").write_text(
                    json.dumps(_synthetic_object(0)), encoding="utf-8"
                )
                csv_paths.append(csv_path)

            window = MainWindow()
            window._review_autosave_path = root / "autosave.review.json"
            try:
                window.import_review_csvs([str(path) for path in csv_paths])
                first, second = window._review_workspace["sessions"]
                first_uid = first["events"][0]["review_uid"]
                second_uid = second["events"][0]["review_uid"]

                window.locate_review_event(first_uid)
                window.events[0]["t0"] = 5.0
                window.events[0]["reviewed_range"]["start"] = 5.0
                window.locate_review_event(second_uid)

                self.assertEqual(first["events"][0]["t0"], 5.0)
                self.assertEqual(first["events"][0]["review_status"], "modified")
                window.locate_review_event(first_uid)
                self.assertEqual(window.events[0]["t0"], 5.0)
                self.assertTrue(window._review_autosave_path.is_file())
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
