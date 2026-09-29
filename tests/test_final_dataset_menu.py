def test_dataset_menu_separate_and_pregnancy_has_three_stages():
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    menus = {a.text().split("(")[0]: a.menu() for a in window.menuBar().actions()}
    try:
        names = list(menus)
        assert "数据整理" not in names
        assert any(a.text() == "数据归类" for a in menus["下载"].actions())
        assert not {"数据集构建", "行为识别", "健康与繁殖"} & set(names)  # 4.3.8 Annotator
        assert not any(
            "旧标签" in a.text() or "算法数据集" in a.text() for a in menus["下载"].actions()
        )
        window.open_organization(1)
        assert not hasattr(window._organization_window, "legacy_dataset_button")
        window._organization_window.close()
    finally:
        window.close()
        app.processEvents()
