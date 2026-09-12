def test_dataset_menu_separate_and_pregnancy_has_three_stages():
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    menus = {a.text().split("(")[0]: a.menu() for a in window.menuBar().actions()}
    try:
        names = list(menus)
        assert names.index("数据整理") + 1 == names.index("数据集构建")
        assert names.index("数据集构建") + 1 == names.index("行为识别")
        assert not any(
            "旧标签" in a.text() or "算法数据集" in a.text() for a in menus["数据整理"].actions()
        )
        pregnancy = next(a.menu() for a in menus["健康与繁殖"].actions() if a.text() == "怀孕")
        assert [a.text() for a in pregnancy.actions()] == ["孕早期", "孕中期", "孕晚期"]
        window.open_organization(1)
        assert not hasattr(window._organization_window, "legacy_dataset_button")
        window._organization_window.close()
    finally:
        window.close()
        app.processEvents()
