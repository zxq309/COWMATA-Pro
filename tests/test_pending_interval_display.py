"""Pending intervals appear immediately without becoming persisted events."""

from __future__ import annotations

from cowmata_tailring.annotation.mixins.annotation import AnnotationMixin


class _Table:
    def __init__(self) -> None:
        self.rows = 0
        self.items = {}

    def blockSignals(self, _blocked: bool) -> None:  # noqa: N802
        pass

    def setRowCount(self, count: int) -> None:  # noqa: N802
        self.rows = count
        self.items = {}

    def setItem(self, row: int, column: int, item) -> None:  # noqa: N802
        self.items[row, column] = item

    def item(self, row: int, column: int):
        return self.items.get((row, column))

    def rowCount(self) -> int:  # noqa: N802
        return self.rows

    def selectRow(self, _row: int) -> None:  # noqa: N802
        pass


class _Text:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, value: str) -> None:  # noqa: N802
        self.value = value


class _Plot:
    def __init__(self) -> None:
        self.events = None

    def set_events(self, _labels, events) -> None:
        self.events = events


class _Harness(AnnotationMixin):
    def __init__(self) -> None:
        self.labels = [
            {
                "name": "站立",
                "key": "1",
                "type": "interval",
                "layer": "body_state",
            },
            {
                "name": "人工辅助产犊",
                "key": "D",
                "type": "interval",
                "layer": "calving_process",
            },
        ]
        self.events = [
            {"id": 7, "li": 0, "t0": 200.0, "t1": 300.0, "note": "done"}
        ]
        self.pending_intervals = {1: 100.0}
        self.selected_event_id = None
        self.event_table = _Table()
        self.event_count_label = _Text()
        self.plot = _Plot()
        self.data_create_time_ms = 0
        self.messages = []

    def _sync_plot_event_selection(self) -> None:
        pass

    def _refresh_enabled(self) -> None:
        pass

    def statusBar(self):  # noqa: N802
        return self

    def showMessage(self, message: str, timeout: int = 0) -> None:  # noqa: N802
        self.messages.append((message, timeout))


def test_pending_interval_has_start_but_no_end_or_duration() -> None:
    window = _Harness()

    window._refresh_events()

    assert window.event_table.rows == 2
    assert window.event_table.item(0, 0).text() == "…"
    assert window.event_table.item(0, 0).data(256) == -2
    assert window.event_table.item(0, 2).text() == "人工辅助产犊"
    assert window.event_table.item(0, 3).text() == "00:00:00.100"
    assert window.event_table.item(0, 4).text() == ""
    assert window.event_table.item(0, 5).text() == ""
    assert window.event_table.item(0, 6).text() == "进行中 · 再次按快捷键结束: D"
    assert window.event_count_label.value == "1 条 · 1 进行中"

    # The virtual row is display-only: plots, saves and exports still receive
    # only completed events from ``window.events``.
    assert window.plot.events is window.events
    assert len(window.events) == 1


def test_delete_selected_cancels_only_the_pending_interval() -> None:
    window = _Harness()
    window.selected_event_id = -2

    window.delete_selected_event()

    assert window.pending_intervals == {}
    assert len(window.events) == 1
    assert window.selected_event_id is None
    assert window.messages[-1] == ("已取消选中的待闭合区间", 3000)


class _StartHarness(AnnotationMixin):
    def __init__(self) -> None:
        self.data = object()
        self.labels = [
            {
                "name": "人工辅助产犊",
                "key": "D",
                "type": "interval",
                "layer": "calving_process",
            }
        ]
        self.pending_intervals = {}
        self.playhead_ms = 1234.0
        self.selected_label = 0
        self.refresh_count = 0

    def _set_visible_label(self, _label_index: int) -> None:
        pass

    def _refresh_playhead_for_user_action(self) -> None:
        pass

    def _refresh_events(self) -> None:
        self.refresh_count += 1

    def statusBar(self):  # noqa: N802
        return self

    def showMessage(self, *_args) -> None:  # noqa: N802
        pass


def test_starting_interval_refreshes_table_immediately() -> None:
    window = _StartHarness()

    window._label_shortcut(0)

    assert window.pending_intervals == {0: 1234.0}
    assert window.refresh_count == 1
