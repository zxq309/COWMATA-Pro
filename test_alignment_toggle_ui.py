from __future__ import annotations

import os
import unittest
from datetime import datetime
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui_helpers import BEIJING
from ultimate_v7_window import MainWindow


class _Media:
    def __init__(self) -> None:
        self.current_path = os.path.abspath("hiv00000.mp4")
        self.current_status = "paused"
        self.time_ms = 4_000.0
        self.playing = False

    def get_time_ms(self) -> float:
        return self.time_ms

    def set_time_ms(self, value: float) -> bool:
        self.time_ms = float(value)
        return True

    def duration_ms(self) -> float:
        return 3_600_000.0

    def is_playing(self) -> bool:
        return self.playing

    def pause(self, paused: bool = True) -> None:
        self.playing = not paused

    def play(self) -> bool:
        self.playing = True
        return True

    def set_rate(self, _value: float) -> bool:
        return True

    def get_rate(self) -> float:
        return 1.0

    def set_volume(self, _value: int) -> None:
        pass

    def mute(self, _value: bool) -> None:
        pass

    def close(self) -> None:
        pass


class AlignmentToggleUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window._autosave = lambda: None
        self.window.data = SimpleNamespace(
            create_time_ms=1_800_000_000_000,
            duration_ms=3_600_000.0,
            device="TEST",
            version=2,
            sample_count=4,
            times_ms=np.asarray([0.0, 1_000.0, 2_000.0, 3_000.0]),
        )
        self.window.data_path = os.path.abspath("motion.json")
        self.window.media.close()
        self.window.media = _Media()
        self.window.video_path = self.window.media.current_path
        self.window.video_start_wall_ms = self.window.data_create_time_ms + 10_000
        self.window.align_method = "pin"
        self.window.playhead_ms = 14_000.0
        self.window._media_primed = True
        self.window._set_timelines_linked(True)

    def tearDown(self) -> None:
        self.window.close()

    def test_button_unlocks_and_repins_current_positions(self) -> None:
        self.window.pin_btn.click()

        self.assertFalse(self.window._timelines_are_linked())
        self.assertFalse(self.window.label_list.isEnabled())
        self.assertIn("钉住 当前帧", self.window.pin_btn.text())

        self.window._pending_data_anchor_ms = 25_000.0
        self.window._set_playhead_visual(25_000.0)
        self.window._pending_video_anchor_ms = 8_000.0
        self.window.media.time_ms = 8_000.0
        self.window.pin_btn.click()

        self.assertTrue(self.window._timelines_are_linked())
        self.assertTrue(self.window.label_list.isEnabled())
        self.assertTrue(self.window.pin_btn.isChecked())
        self.assertEqual(
            self.window.video_start_wall_ms,
            self.window.data_create_time_ms + 17_000,
        )
        project = self.window._project_model()
        restored = type(project).from_dict(project.to_dict())
        self.assertTrue(restored.align["locked"])
        self.assertTrue(restored.extras["ui"]["timelinesLinked"])

    def test_unlocked_state_blocks_new_annotations_and_export(self) -> None:
        self.window.pin_btn.click()
        before = len(self.window.events)

        self.window._add_event(0, 1_000.0, 2_000.0)
        project = self.window._project_model()
        issues = self.window._release_validation_issues(project)

        self.assertEqual(len(self.window.events), before)
        self.assertFalse(project.align["locked"])
        self.assertFalse(project.extras["ui"]["timelinesLinked"])
        self.assertTrue(
            any(issue.code == "timelines_unlocked" for issue in issues)
        )

    def test_unlocked_media_time_does_not_move_data_playhead(self) -> None:
        self.window.pin_btn.click()
        self.window._set_playhead_visual(25_000.0)

        self.window._on_media_time(9_000)

        self.assertEqual(self.window.playhead_ms, 25_000.0)
        self.assertEqual(self.window._pending_video_anchor_ms, 9_000.0)

    def test_open_interval_prevents_unlocking(self) -> None:
        self.window.pending_intervals[0] = self.window.playhead_ms

        self.window.pin_btn.click()

        self.assertTrue(self.window._timelines_are_linked())
        self.assertTrue(self.window.pin_btn.isChecked())
        self.assertIn("先结束或取消", self.window.statusBar().currentMessage())

    def test_pending_video_seek_prevents_premature_pin(self) -> None:
        self.window.pin_btn.click()
        self.window._pending_video_anchor_ms = 8_000.0
        self.window._ui_pending_seek_video_ms = 8_000.0

        self.window.pin_btn.click()

        self.assertFalse(self.window._timelines_are_linked())
        self.assertFalse(self.window.pin_btn.isChecked())
        self.assertIn("定位仍在确认", self.window.statusBar().currentMessage())

    def test_corner_alignment_links_and_refreshes_data_from_current_frame(self) -> None:
        self.window.pin_btn.click()
        target_wall_ms = self.window.data_create_time_ms + 25_000
        target_clock = datetime.fromtimestamp(
            target_wall_ms / 1000.0, BEIJING
        ).strftime("%H:%M:%S.%f")[:-3]
        self.window.corner_time_edit.setText(target_clock)

        self.window.align_from_corner()

        self.assertEqual(self.window.align_method, "corner")
        self.assertTrue(self.window._timelines_are_linked())
        self.assertEqual(self.window.playhead_ms, 25_000.0)


if __name__ == "__main__":
    unittest.main()
