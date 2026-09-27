"""4.3.2: 行为识别 → 训练与识别 opened with ModuleNotFoundError (ui.algorithms.paths)."""
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.modern_window import MainWindow


def test_training_menu_opens_behavior_window():
    """4.3.8 Annotator: behaviour training moved to the calving-prediction algorithm package."""
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        menus = {a.text().split("(")[0] for a in w.menuBar().actions()}
        assert not menus & {"行为识别", "数据集构建", "健康与繁殖"}
    finally:
        w.close()
        app.processEvents()


def test_dataset_default_finds_configured_dataset(tmp_path, monkeypatch):
    from cowmata_tailring.ui.algorithms.workbench_ui import dataset_default

    (tmp_path / "COWMATA_Behavior_Dataset").mkdir()
    monkeypatch.setenv("COWMATA_DATASET_HOME", str(tmp_path))
    assert dataset_default("COWMATA_Behavior_Dataset") == str(tmp_path / "COWMATA_Behavior_Dataset")
    monkeypatch.setenv("COWMATA_DATASET_HOME", str(tmp_path / "missing"))
    assert dataset_default("definitely-not-present-431") == ""