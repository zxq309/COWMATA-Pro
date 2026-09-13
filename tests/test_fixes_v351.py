"""Client regressions: editable intake, blocked state starts and recoverable ends."""

# ruff: noqa: F811
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget
from test_annotation_pipeline_v330 import case, window  # noqa: F401
from test_updates import startup_gate  # noqa: F401

from cowmata_tailring.workspace import organization_ui
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture
def organizer(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(organization_ui, "QSettings", lambda: settings)
    dialog = organization_ui.OrganizationWindow(owner)
    dialog.tabs.setCurrentIndex(1)
    dialog.scenario.setCurrentIndex(1)
    dialog.show()
    app.processEvents()
    yield dialog
    dialog.close()
    owner.close()


def test_attach_inputs_offer_category_and_single_farm_picker(organizer, tmp_path, monkeypatch):
    assert organizer.target.isEnabled() and organizer.category.isEnabled()
    organizer.category.setCurrentIndex(organizer.category.findData("pregnancy"))
    assert organizer.pregnancy_stage.isEnabled() and organizer.pregnancy_stage.isVisible()
    farm = tmp_path / "自选牧场"
    farm.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: str(farm))
    organizer.target_browse.click()
    assert organizer.target.text() == str(farm)
    assert organizer.farm.text() == str(farm)


def test_compact_window_keeps_intake_controls_visible_without_scrolling(organizer):
    organizer.resize(950, 650)
    QApplication.processEvents()
    for widget in (
        organizer.target_browse,
        organizer.sources,
        organizer.execute_top,
        organizer.cancel_button,
        organizer.export_button,
    ):
        assert widget.isVisible()
        assert organizer.rect().contains(widget.geometry())
    assert not organizer.tabs.isVisible()


@pytest.mark.parametrize("existing", ["STANDING", "LYING", "WALKING"])
@pytest.mark.parametrize("new", ["STANDING", "LYING", "WALKING"])
def test_state_start_inside_any_existing_state_is_rejected(window, monkeypatch, existing, new):
    def index(code):
        return next(i for i, label in enumerate(window.work.project.labels) if label.code == code)

    window.work.add_draft(index(existing), 10000, 10500, [])
    window.board.main_camera = "A"
    monkeypatch.setattr(
        window, "evidence", lambda: [{"camera": "A", "frame_ready": True, "reference_ms": 10200}]
    )
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)
    window.mark(index(new))
    assert window.active_event is None
    assert len(window.work.drafts) == 1 and messages


def test_rejected_end_can_be_repositioned_and_finished(window, monkeypatch):
    index = next(
        i for i, label in enumerate(window.work.project.labels) if label.code == "STANDING"
    )
    window.work.add_draft(index, 10400, 10600, [])
    position = [10100]
    window.board.main_camera = "A"
    monkeypatch.setattr(
        window,
        "evidence",
        lambda: [{"camera": "A", "frame_ready": True, "reference_ms": position[0]}],
    )
    monkeypatch.setattr(window, "tell", lambda *_: None)
    window.mark(index)
    position[0] = 10500
    window.mark(index)
    assert window.active_event is not None and "end" not in window.active_event
    position[0] = 10300
    window.mark(index)
    assert window.active_event is None
    assert window.work.drafts[-1]["reference_end"] == 10300


def test_failed_update_offers_immediate_local_start(startup_gate):
    from PySide6.QtWidgets import QDialog

    startup_gate.updater._failed("[SSL: UNEXPECTED_EOF_WHILE_READING]")
    assert startup_gate.continue_button.isEnabled()
    QTest.mouseClick(startup_gate.continue_button, Qt.MouseButton.LeftButton)
    assert startup_gate.result() == QDialog.DialogCode.Accepted
    assert startup_gate.updater.pending_job is None


def test_state_start_guard_also_checks_confirmed_events():
    from cowmata_tailring.workspace.clocks import Anchor, ClockMap

    work = SessionWork("sample")
    work.clock = ClockMap([Anchor(0, 10000), Anchor(1000, 11000)])
    index = next(i for i, label in enumerate(work.project.labels) if label.code == "STANDING")
    work.project.add_event(index, 100, 500)
    with pytest.raises(ValueError):
        work.assert_state_interval(index, 10200, None)
