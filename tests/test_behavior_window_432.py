"""4.3.2: 行为识别 → 训练与识别 opened with ModuleNotFoundError (ui.algorithms.paths)."""
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.modern_window import MainWindow


def test_training_menu_opens_behavior_window(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path / "models"))
    monkeypatch.setenv("COWMATA_DATA_HOME", str(tmp_path / "data"))
    import cowmata_security.qt_ui as qt_ui

    monkeypatch.setattr(qt_ui, "guard_action", lambda parent, capability: True)
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        errors = []
        monkeypatch.setattr(w, "tell", lambda text, *a, **k: errors.append(text), raising=False)
        menus = {a.text().split("(")[0]: a.menu() for a in w.menuBar().actions()}
        train = next(a for a in menus["行为识别"].actions() if a.text() == "训练与识别…")
        train.trigger()
        app.processEvents()
        window = w._behavior_390
        assert window is not None and window.isVisible()
        window.close()
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