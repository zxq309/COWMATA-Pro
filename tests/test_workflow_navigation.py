"""Workflow navigation keeps recovery visible and reuses the existing business commands."""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from cowmata_tailring.workspace.modern_window import MainWindow


@pytest.fixture()
def window():
    app = QApplication.instance() or QApplication([])
    view = MainWindow()
    yield view
    view.close()
    app.processEvents()


class DownloadHost:
    def __init__(self):
        self.calls = []
        self.dialog = SimpleNamespace(
            store=SimpleNamespace(value={"forward_tracks": ["AI大模型算法"]}),
            open_decision_app=lambda: self.calls.append("report"),
            open_configuration=lambda: self.calls.append("settings"),
            retry_failed=lambda: self.calls.append("retry"),
        )

    def ensure(self):
        return self.dialog

    def open(self):
        self.calls.append("show")

    def run(self, callback, show=True):
        if show:
            self.open()
        callback(self.dialog)


def test_missing_risk_report_has_visible_feedback_and_recovery(window, monkeypatch):
    from cowmata_tailring.edge_download import decider

    host = DownloadHost()
    window._downloader = host
    monkeypatch.setattr(decider, "decision_app", lambda **kwargs: None)
    window.open_risk_overview()
    assert host.calls == ["show", "report"]
    assert not window.banner.isHidden()
    assert "风险总览尚未生成" in window.banner.text()
    assert "下载设置" in window.banner.text() and "运行记录" in window.banner.text()


def test_existing_risk_report_opens_without_showing_the_downloader(window, monkeypatch, tmp_path):
    from cowmata_tailring.edge_download import decider

    host = DownloadHost()
    window._downloader = host
    calls = []

    def existing(**kwargs):
        calls.append(kwargs["tracks"])
        return tmp_path / "report.html"

    monkeypatch.setattr(decider, "decision_app", existing)
    window.open_risk_overview()
    assert calls == [["AI大模型算法"]]
    assert host.calls == ["report"]


def test_retry_menu_reuses_downloader_recovery_and_shows_its_status(window):
    host = DownloadHost()
    window._downloader = host
    download = next(a.menu() for a in window.menuBar().actions() if a.text() == "下载")
    action = next(a for a in download.actions() if a.text() == "重试失败批次")
    action.trigger()
    assert host.calls == ["show", "retry"]


def test_workflow_is_nonmodal_reusable_and_routes_each_step(window, monkeypatch):
    host = DownloadHost()
    window._downloader = host
    calls = []
    window._ledger_host = SimpleNamespace(open=lambda: calls.append("ledger"), close=lambda: True)
    monkeypatch.setattr(window, "choose_project", lambda: calls.append("project"))
    monkeypatch.setattr(window, "open_risk_overview", lambda: calls.append("risk"))
    window.workflow_button.click()
    dialog = window.workflow_dialog
    assert not dialog.isModal() and dialog.isVisible()
    assert len(window.workflow_actions) == 5
    for key in ("ledger", "download", "annotate", "inference", "report"):
        window.workflow_actions[key].click()
    assert calls == ["ledger", "project", "risk"]
    assert host.calls == ["show", "settings"]
    assert any("实时推理依据台账与下载数据运行" in label.text() for label in dialog.findChildren(QLabel))
    dialog.hide()
    window.quick_help()
    assert window.workflow_dialog is dialog and dialog.isVisible()
