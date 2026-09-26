"""4.3.1: simpler menus — every command reachable, at most two levels deep, no duplicates."""
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.modern_window import MainWindow


def leaves(menu, depth=1, out=None):
    out = [] if out is None else out
    for action in menu.actions():
        if action.menu() is not None:
            leaves(action.menu(), depth + 1, out)
        elif not action.isSeparator():
            out.append((action, depth))
    return out


def test_menus_are_shallow_grouped_and_complete():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        menus = {a.text().split("(")[0]: a.menu() for a in w.menuBar().actions()}
        assert list(menus) == ["文件", "数据准备", "标注与复核", "数据集构建", "行为识别", "健康与繁殖", "帮助"]
        found = [leaf for menu in menus.values() for leaf in leaves(menu)]
        texts = [a.text() for a, _ in found]
        # No command is listed twice and nothing sits deeper than menu > submenu > item.
        assert len(texts) == len(set(texts)) + texts.count("返回标注布局") - 1
        assert max(depth for _, depth in found) <= 2
        assert "算法管理" not in texts  # same window as 行为识别 → 训练与识别
        top = {name: [a.text() for a in m.actions() if not a.isSeparator()] for name, m in menus.items()}
        assert top["数据准备"] == ["端侧数据下载…", "数据归类…", "导出标准 MP4 副本…", "录像索引与核验"]
        assert top["标注与复核"] == ["自动生成候选…", "用所选九轴区间建立候选", "完成本份九轴…", "下一份未完成九轴",
                                "撤销", "重做", "标签编辑", "时间同步", "多人协作", "显示与播放"]
        assert top["行为识别"] == ["训练与识别…", "逐项算法检查", "打开模型库"]
        assert "打开协作原始数据包…" in [a.text() for a in w._collaboration_menu.actions()]
        view = next(a.menu() for a in menus["标注与复核"].actions() if a.text() == "显示与播放")
        assert [a.text() for a in view.actions()][:2] == ["放大视频（保留波形条）", "波形分屏到独立窗口 / 合并"]
        # Shortcuts survive the move and none is duplicated.
        keys = [a.shortcut().toString() for a, _ in found if not a.shortcut().isEmpty()]
        assert len(keys) == len(set(keys))
        assert {"Ctrl+O", "Ctrl+S", "Ctrl+Z", "Ctrl+Y", "F5", "F11", "Ctrl+E", "Ctrl+Shift+E", "Ctrl+Return"} <= set(keys)
        assert all(a in {x for x, _ in found} for a in w.algorithm_actions.values())
    finally:
        w.close()
        app.processEvents()