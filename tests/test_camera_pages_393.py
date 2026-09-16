import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem

from cowmata_tailring.workspace.camera_pages import CameraPages, camera_pages


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])

def test_fixed_twenty_slots_never_compact_missing_views():
    groups = camera_pages(['视角09', '视角20'])
    assert [g[0] for g in groups] == ['01–08', '09–16', '17–20']
    assert [g[1] for g in groups] == [[], ['视角09'], ['视角20']]

def test_legacy_names_remain_selectable_in_bounded_pages():
    names = [f'CAM{i:02}' for i in range(21)]
    pages = camera_pages(names)
    assert [c for _, group in pages for c in group] == names
    assert all(len(group) <= 8 for _, group in pages)

def test_rapid_page_requests_only_emit_latest(app):
    widget = CameraPages()
    widget.set_inventory([f'视角{i:02}' for i in range(1, 21)])
    requests = []
    widget.selectionRequested.connect(requests.append)
    widget.request_page(0)
    widget.request_page(1)
    widget.request_page(2)
    assert requests == []
    widget._apply_pending()
    assert requests == [[f'视角{i:02}' for i in range(17, 21)]]
    widget.close()

def test_inventory_change_cancels_queued_selection(app):
    widget = CameraPages()
    widget.set_inventory(['视角01', '视角20'])
    requests = []
    widget.selectionRequested.connect(requests.append)
    widget.request_page(2)
    widget.set_inventory(['Different project'])
    widget._apply_pending()
    assert requests == []
    widget.close()

def test_page_change_preserves_annotation_time_rate_and_pool(app):
    from cowmata_tailring.workspace.modern_window import MainWindow
    window = MainWindow()
    window.board.timer.stop()
    window.save_timer.stop()
    window.source_timer.stop()
    window.cameras.blockSignals(True)
    names = [f'视角{i:02}' for i in range(1,21)]
    for name in names:
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole,name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        window.cameras.addItem(item)
    window.cameras.blockSignals(False)
    window.board.reference_ms = 1789321780123
    window.board.rate = 2
    draft = object()
    window.active_event = draft
    maps = {'视角20': {'offset_ms': 23}}
    window.settings['camera_maps'] = maps
    for group in (names[:8],names[8:16],names[16:],names[:8]):
        window.select_camera_page(group)
        assert window.board.selected == group
        assert window.board.reference_ms == 1789321780123
        assert window.board.rate == 2
        assert window.active_event is draft
        assert window.settings['camera_maps'] is maps
        assert len(window.board.pool) <= 9
    window.active_event = None
    window.dirty = False
    window.close()


def test_main_video_chooser_only_offers_playable_mp4(app, monkeypatch):
    from cowmata_tailring.workspace import window as controller
    captured = []
    monkeypatch.setattr(controller.QFileDialog, 'getOpenFileName',
        lambda *args: (captured.append(args) or ('','')))
    controller.MainWindow.choose_video_record(object())
    assert captured[0][-1] == 'MP4 视频 (*.mp4)'
