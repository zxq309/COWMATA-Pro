from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ultimate_v7_window import MainWindow


class EventTableBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.data = SimpleNamespace(
            create_time_ms=1_785_141_053_033,
            duration_ms=3_599_927.0,
        )
        self.window.events = [
            {
                "id": 58,
                "li": 2,
                "t0": 2_132_758.0,
                "t1": 2_133_225.0,
                "note": "",
            },
            {
                "id": 59,
                "li": 2,
                "t0": 2_133_752.0,
                "t1": 2_144_779.0,
                "note": "",
            },
        ]
        self.window.selected_event_id = 58
        self.seek_calls: list[tuple[float, bool]] = []

        def record_seek(value: float, seek_video: bool = True) -> None:
            self.seek_calls.append((float(value), bool(seek_video)))

        self.window.set_playhead = record_seek

    def tearDown(self) -> None:
        self.window.close()

    def test_refresh_selects_new_event_without_seeking(self) -> None:
        self.window._refresh_events()

        self.assertEqual(self.seek_calls, [])
        self.assertEqual(
            self.window.event_table.item(0, 3).text(),
            "2026-07-27 17:06:25.791",
        )
        self.assertEqual(
            self.window.event_table.item(0, 4).text(),
            "2026-07-27 17:06:26.258",
        )
        self.assertEqual(
            self.window.event_table.item(0, 5).text(),
            "00:00:00.467",
        )
        self.assertEqual(
            self.window.event_table.horizontalHeaderItem(3).text(),
            "开始记录时间",
        )
        self.assertEqual(
            self.window.event_table.horizontalHeaderItem(4).text(),
            "结束记录时间",
        )

    def test_user_selection_still_navigates_to_event_start(self) -> None:
        self.window._refresh_events()

        self.window.event_table.selectRow(1)

        self.assertEqual(self.seek_calls, [(2_133_752.0, True)])

    def test_finishing_annotation_keeps_the_end_position(self) -> None:
        self.window.events = []
        self.window.next_event_id = 58
        self.window.playhead_ms = 2_133_225.0
        self.window._autosave = lambda: None

        self.window._add_event(2, 2_132_758.0, 2_133_225.0)

        self.assertEqual(self.seek_calls, [(2_133_225.0, False)])
        self.assertEqual(self.window.events[0]["t0"], 2_132_758.0)
        self.assertEqual(self.window.events[0]["t1"], 2_133_225.0)


if __name__ == "__main__":
    unittest.main()
