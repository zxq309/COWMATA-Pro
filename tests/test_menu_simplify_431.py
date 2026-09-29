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
        # 4.4.0: the operator's day in menu order.
        assert list(menus) == ["文件", "编辑", "上传", "下载", "标注", "工具", "帮助"]
        found = [leaf for menu in menus.values() for leaf in leaves(menu)]
        texts = [a.text() for a, _ in found]
        # Nothing sits deeper than menu > submenu > item; every 4.3.9 command was re-homed.
        assert max(depth for _, depth in found) <= 2
        assert w._menu_unplaced == []
        top = {name: [a.text() for a in m.actions() if not a.isSeparator()] for name, m in menus.items()}
        assert top["文件"][:5] == ["打开工程…", "打开九轴…", "打开视频…", "打开协作数据包…", "历史回看…"]
        assert top["编辑"][:2] == ["撤销", "重做"]
        assert top["上传"][:4] == ["台账", "导入表格…", "同步修改", "刷新服务器"]
        assert top["下载"][:3] == ["端侧数据…", "更新台账并下载", "停止下载"] and "数据归类" in top["下载"]
        assert top["标注"][:4] == ["自动生成候选…", "用所选区间建立候选", "逐项检查", "自动标注模型…"]
        assert {"时间同步", "录像", "视图", "设置…"} <= set(top["工具"])
        assert top["帮助"] == ["快速开始", "新手图文教程…", "账号", "关于"]
        assert "派发原始数据包…" in [a.text() for a in w._collaboration_menu.actions()]
        view = next(a.menu() for a in menus["工具"].actions() if a.text() == "视图")
        assert [a.text() for a in view.actions()][:2] == ["放大视频", "波形分屏"]
        # Shortcuts survive the move and none is duplicated.
        keys = [a.shortcut().toString() for a, _ in found if not a.shortcut().isEmpty()]
        assert len(keys) == len(set(keys))
        assert {"Ctrl+O", "Ctrl+S", "Ctrl+Z", "Ctrl+Y", "F5", "F11", "Ctrl+E", "Ctrl+Shift+E", "Ctrl+Return",
                "Ctrl+U", "Ctrl+D", "Ctrl+Q", "Ctrl+W", "Ctrl+F"} <= set(keys)
        assert all(a in {x for x, _ in found} for a in w.algorithm_actions.values())
    finally:
        w.close()
        app.processEvents()