from PySide6.QtWidgets import QApplication, QTableView

from cowmata_tailring.workspace.classification_viewer import ReportModel
from cowmata_tailring.workspace.dataset_build_ui import PairModel


def test_live_dataset_updates_keep_selected_record():
    app = QApplication.instance() or QApplication([])
    model = PairModel()
    view = QTableView()
    view.setModel(model)
    rows = [dict(id=str(i), status="pending", source=f"{i}.json") for i in range(100)]
    model.replace(rows)
    view.selectRow(60)
    model.replace([{**r, "status": "done" if i == 0 else r["status"]} for i, r in enumerate(rows)])
    assert view.currentIndex().row() == 60
    assert model.data(model.index(0, 0)) == "已完成"
    view.close()
    app.processEvents()


def test_live_classification_updates_keep_selected_record():
    app = QApplication.instance() or QApplication([])
    model = ReportModel()
    view = QTableView()
    view.setModel(model)
    rows = [dict(source=f"c:/source/{i}.mp4", kind="video", status="pending") for i in range(100)]
    model.replace(rows)
    view.selectRow(60)
    model.replace([{**r, "status": "done" if i == 0 else r["status"]} for i, r in enumerate(rows)])
    assert view.currentIndex().row() == 60
    view.close()
    app.processEvents()
