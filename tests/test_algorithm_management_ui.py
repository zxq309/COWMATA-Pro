import threading
import time

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from cowmata_tailring.algorithms.workbench_ui import AlgorithmWorkbench, CalvingEvidenceWindow


def pump_until(predicate):
    app = QApplication.instance()
    for _ in range(300):
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def test_management_loads_bundled_metrics_and_can_switch_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path))
    owner = QWidget()
    window = AlgorithmWorkbench(owner)
    assert window.metrics.rowCount() >= 3
    assert "events-20260915-01" in window.active.text()
    window.use_version()
    assert (tmp_path/"active.json").is_file()
    assert window.metrics.item(0, 7).text() == "—"
    window.close()
    window.deleteLater()
    owner.deleteLater()


def test_closing_management_cancels_owned_worker_before_window_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path))
    started, stopped = threading.Event(), threading.Event()
    def slow(request, *, cancelled, progress_path):
        started.set()
        while not cancelled():
            time.sleep(.005)
        stopped.set()
        raise InterruptedError("cancelled")
    monkeypatch.setattr("cowmata_tailring.algorithms.workbench_ui.run_job", slow)
    owner = QWidget()
    window = AlgorithmWorkbench(owner)
    window.show()
    window.launch(dict(action="train"))
    assert started.wait(1)
    assert not window.close()
    pump_until(lambda: not window.running)
    assert stopped.is_set() and not window.isVisible()
    window.deleteLater()
    owner.deleteLater()


def test_calving_window_keeps_results_bound_to_report_cow(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path))
    owner = QWidget()
    window = CalvingEvidenceWindow(owner)
    window.output = tmp_path
    window.accept_result(dict(model_version="test", rows=[], issues=[], evaluation=[
        dict(metric="预警指标", value=None, scope="不输出预警")]))
    assert window.evaluation.item(0, 1).text() == "—"
    assert window.evidence.rowCount() == 0
    window.close()
    window.deleteLater()
    owner.deleteLater()
