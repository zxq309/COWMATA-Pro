from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BOVINE_NO_MEDIA", "1")

from PySide6.QtWidgets import QApplication, QToolBar

from complete_window import MainWindow


class FakeMedia:
    current_status = "paused"


class VideoFullscreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fullscreen_keeps_native_video_handle_and_restores_workspace(self) -> None:
        window = MainWindow()
        window.media = FakeMedia()
        window.video_path = "sample.mp4"
        window._refresh_enabled()
        window.show()
        self.app.processEvents()
        original_handle = int(window.video_surface.winId())
        original_sizes = window.top_splitter.sizes()
        try:
            window.enter_video_fullscreen()
            self.app.processEvents()
            self.assertTrue(window._video_fullscreen_active)
            self.assertTrue(window.isFullScreen())
            self.assertEqual(int(window.video_surface.winId()), original_handle)
            self.assertFalse(window.data_panel.isVisible())
            self.assertFalse(window.annotation_splitter.isVisible())
            self.assertTrue(
                all(not toolbar.isVisible() for toolbar in window.findChildren(QToolBar))
            )
            self.assertEqual(window.video_fullscreen_btn.text(), "退出全屏")

            window.exit_video_fullscreen()
            self.app.processEvents()
            self.assertFalse(window._video_fullscreen_active)
            self.assertFalse(window.isFullScreen())
            self.assertEqual(int(window.video_surface.winId()), original_handle)
            self.assertTrue(window.data_panel.isVisible())
            self.assertTrue(window.annotation_splitter.isVisible())
            restored_sizes = window.top_splitter.sizes()
            self.assertTrue(all(value > 0 for value in restored_sizes))
            self.assertAlmostEqual(
                restored_sizes[0] / sum(restored_sizes),
                original_sizes[0] / sum(original_sizes),
                delta=0.15,
            )
            self.assertEqual(window.video_fullscreen_btn.text(), "⛶ 全屏")
        finally:
            window.exit_video_fullscreen()
            window.close()

    def test_escape_shortcut_exits_and_double_click_toggles(self) -> None:
        window = MainWindow()
        window.media = FakeMedia()
        window.video_path = "sample.mp4"
        window.show()
        self.app.processEvents()
        try:
            window.video_surface.doubleClicked.emit()
            self.app.processEvents()
            self.assertTrue(window._video_fullscreen_active)
            window._video_fullscreen_escape_shortcut.activated.emit()
            self.app.processEvents()
            self.assertFalse(window._video_fullscreen_active)
        finally:
            window.exit_video_fullscreen()
            window.close()


if __name__ == "__main__":
    unittest.main()
