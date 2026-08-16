from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BOVINE_NO_MEDIA", "1")

from PySide6.QtWidgets import QApplication, QHeaderView, QToolBar

from integrated_window import MainWindow


class CompactUiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.resize(1_920, 1_080)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()

    def test_time_scale_selector_stays_in_compact_navigation_row(self) -> None:
        self.assertGreaterEqual(
            self.window.data_nav_layout.indexOf(self.window.view_scale_combo), 0
        )
        self.assertIs(
            self.window.view_scale_combo.parentWidget(),
            self.window.data_nav_bar,
        )
        self.assertEqual(
            self.window.data_panel.layout().indexOf(self.window.view_scale_combo),
            -1,
        )

    def test_video_uses_primary_space_and_alignment_tools_are_collapsed(self) -> None:
        self.assertGreaterEqual(self.window.video_surface.width(), 760)
        self.assertGreaterEqual(self.window.video_surface.height(), 520)
        top_sizes = self.window.top_splitter.sizes()
        self.assertAlmostEqual(
            top_sizes[0] / sum(top_sizes), 0.5, delta=0.08
        )
        self.assertLessEqual(self.window.annotation_splitter.height(), 226)
        self.assertFalse(self.window.alignment_advanced.isVisible())

        self.window.alignment_toggle_btn.click()
        self.app.processEvents()
        self.assertTrue(self.window.alignment_advanced.isVisible())
        self.assertTrue(self.window.ffmpeg_btn.isVisible())

    def test_video_navigation_follows_natural_filename_order(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (
                "segment10.mp4",
                "segment2.mp4",
                "segment1.mp4",
                "notes.txt",
            ):
                (root / name).touch()

            original_media = self.window.media
            original_open_video = self.window.open_video
            opened: list[str] = []
            try:
                self.window.media = object()
                self.window.video_path = str(root / "segment2.mp4")
                self.window.open_video = lambda path=None: opened.append(str(path))
                self.window._refresh_enabled()

                self.assertTrue(self.window.previous_video_btn.isEnabled())
                self.assertTrue(self.window.next_video_btn.isEnabled())
                self.window.next_video_btn.click()
                self.assertEqual(Path(opened[-1]).name, "segment10.mp4")

                self.window.previous_video_btn.click()
                self.assertEqual(Path(opened[-1]).name, "segment1.mp4")
            finally:
                self.window.open_video = original_open_video
                self.window.media = original_media
                self.window.video_path = ""

    def test_video_navigation_disables_buttons_at_directory_boundaries(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "clip1.mp4"
            last = root / "clip2.mp4"
            first.touch()
            last.touch()
            (root / "clip3.json").touch()

            original_media = self.window.media
            try:
                self.window.media = object()
                self.window.video_path = str(first)
                self.window._refresh_enabled()
                self.assertFalse(self.window.previous_video_btn.isEnabled())
                self.assertTrue(self.window.next_video_btn.isEnabled())

                self.window.video_path = str(last)
                self.window._refresh_enabled()
                self.assertTrue(self.window.previous_video_btn.isEnabled())
                self.assertFalse(self.window.next_video_btn.isEnabled())

                last.unlink()
                self.window.video_path = str(first)
                self.window._refresh_enabled()
                self.assertFalse(self.window.previous_video_btn.isEnabled())
                self.assertFalse(self.window.next_video_btn.isEnabled())
            finally:
                self.window.media = original_media
                self.window.video_path = ""

    def test_json_navigation_follows_natural_filename_order(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (
                "session10.json",
                "session2.json",
                "session1.json",
                "session3.mp4",
            ):
                (root / name).touch()

            original_data = self.window.data
            original_data_path = self.window.data_path
            original_open_json = self.window.open_json
            opened: list[str] = []
            try:
                self.window.data = object()
                self.window.data_path = str(root / "session2.json")
                self.window.open_json = lambda path=None: opened.append(str(path))
                self.window._refresh_enabled()

                self.assertTrue(self.window.previous_json_btn.isEnabled())
                self.assertTrue(self.window.next_json_btn.isEnabled())
                self.window.next_json_btn.click()
                self.assertEqual(Path(opened[-1]).name, "session10.json")

                self.window.previous_json_btn.click()
                self.assertEqual(Path(opened[-1]).name, "session1.json")
            finally:
                self.window.open_json = original_open_json
                self.window.data = original_data
                self.window.data_path = original_data_path

    def test_json_navigation_disables_buttons_at_directory_boundaries(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "data1.json"
            last = root / "data2.json"
            first.touch()
            last.touch()
            (root / "data3.mp4").touch()

            original_data = self.window.data
            original_data_path = self.window.data_path
            try:
                self.window.data = object()
                self.window.data_path = str(first)
                self.window._refresh_enabled()
                self.assertFalse(self.window.previous_json_btn.isEnabled())
                self.assertTrue(self.window.next_json_btn.isEnabled())

                self.window.data_path = str(last)
                self.window._refresh_enabled()
                self.assertTrue(self.window.previous_json_btn.isEnabled())
                self.assertFalse(self.window.next_json_btn.isEnabled())

                last.unlink()
                self.window.data_path = str(first)
                self.window._refresh_enabled()
                self.assertFalse(self.window.previous_json_btn.isEnabled())
                self.assertFalse(self.window.next_json_btn.isEnabled())
            finally:
                self.window.data = original_data
                self.window.data_path = original_data_path

    def test_export_actions_remain_available_in_compact_menu(self) -> None:
        actions = set(self.window.export_tools_menu.actions())
        for action in (
            self.window.export_action,
            self.window.export_training_action,
            self.window.export_boris_action,
            self.window.irr_action,
        ):
            self.assertIn(action, actions)

    def test_model_actions_share_the_top_toolbar_dropdown(self) -> None:
        self.assertIs(
            self.window.model_assist_toolbar, self.window.main_toolbar
        )
        self.assertEqual(len(self.window.findChildren(QToolBar)), 1)
        self.assertIs(
            self.window.main_toolbar.widgetForAction(
                self.window.model_assist_widget_action
            ),
            self.window.model_assist_button,
        )
        actions = set(self.window.model_assist_menu.actions())
        for action in (
            self.window.model_predict_action,
            self.window.model_waveform_adjust_action,
            self.window.model_edit_prediction_action,
            self.window.model_mark_reviewed_action,
            self.window.model_select_action,
        ):
            self.assertIn(action, actions)

    def test_event_table_uses_compact_rows_and_stretch_note_column(self) -> None:
        self.assertFalse(self.window.event_table.verticalHeader().isVisible())
        self.assertEqual(
            self.window.event_table.verticalHeader().defaultSectionSize(), 24
        )
        self.assertEqual(
            self.window.event_table.horizontalHeader().sectionResizeMode(6),
            QHeaderView.ResizeMode.Stretch,
        )


if __name__ == "__main__":
    unittest.main()
