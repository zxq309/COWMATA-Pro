"""Shared static Qt theme; no window/controller dependency.

4.4.1: bold type throughout, a branded menu bar (COWMATA cyan → green with
white type and the wordmark in its corner) and Fluent System Icons (MIT,
microsoft/fluentui-system-icons) on the key commands.

4.4.0 design language: Apple Human Interface Guidelines rhythm (8 px grid,
13 px body, 12 px secondary, hairline separators, 8–12 px corner radii,
neutral grouped background) in the COWMATA brand colours from
www.cowmata.com: main green #8ADD66 (hover #73CD4C) and secondary cyan
#35AFC8 (hover #239BB3). Green marks the one primary action on a surface;
cyan is the interactive tint for selection, focus and switches. Text on
the light green uses a deep green ink for contrast.
"""

from pathlib import Path

PALETTE = {
    "green": "#8ADD66", "green_hover": "#7FD65A", "green_pressed": "#73CD4C", "green_ink": "#153A0C",
    "cyan": "#35AFC8", "cyan_hover": "#239BB3", "cyan_ink": "#0E5F70", "cyan_soft": "#DDF1F6",
    "background": "#F5F7FA", "card": "#FFFFFF", "fill": "#EEF1F5", "control_line": "#DDE2E9",
    "line": "#E3E7ED", "label": "#1C2530", "secondary": "#6B7785", "tertiary": "#A1AAB5",
    "warning": "#B7791F", "danger": "#D93F3F",
}

_ASSETS = Path(__file__).resolve().parents[2] / "assets/fluent"

STYLE = """
QMainWindow, QWidget {background:#F5F7FA; color:#1C2530; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:13px; font-weight:600;}
QDialog {background:#F5F7FA;}
QFrame#card, QWidget#signalCard, QFrame#sourcePanel, QFrame#eventPanel, QFrame#algorithmPanel, QGroupBox {background:#FFFFFF; border:1px solid #E3E7ED; border-radius:12px;}
QGroupBox {margin-top:14px; padding:18px 12px 12px; font-weight:600;}
QGroupBox::title {subcontrol-origin:margin; left:14px; padding:0 4px; color:#6B7785;}
QLabel {background:transparent; border:0;}
QLabel#brand {font-size:20px; font-weight:700; color:#1C2530;}
QLabel#heading {font-size:20px; font-weight:700; color:#1C2530;}
QLabel#sectionTitle, QLabel#section {font-size:12px; font-weight:600; color:#6B7785; padding:2px 2px;}
QLabel#muted, QLabel#secondary {color:#6B7785;}
QLabel#number {font-size:28px; font-weight:700; color:#1C2530;}
QPushButton, QToolButton {background:#FFFFFF; border:1px solid #DDE2E9; border-radius:8px; padding:5px 14px; min-height:20px; color:#1C2530;}
QPushButton:hover, QToolButton:hover {background:#F2F5F8; border-color:#CDD4DD;}
QPushButton:pressed, QToolButton:pressed {background:#E6EAF0;}
QPushButton:checked, QToolButton:checked {background:#DDF1F6; border-color:#9BD3E0; color:#0E5F70; font-weight:600;}
QPushButton:focus {border-color:#35AFC8;}
QPushButton#primary {background:#8ADD66; border:1px solid #7ACD57; color:#153A0C; font-weight:600;}
QPushButton#primary:hover {background:#7FD65A;}
QPushButton#primary:pressed {background:#73CD4C;}
QPushButton#primary[mainAction="true"] {font-size:14px; min-height:26px; padding:5px 18px;}
QPushButton#importOption {background:#FFFFFF; border:1px solid #E3E7ED; border-radius:12px; text-align:left; padding:14px; font-size:14px; min-height:56px;}
QPushButton#importOption:hover, QPushButton#importOption:focus {background:#F2FBEE; border:1px solid #8ADD66;}
QPushButton#destructive {color:#D93F3F;}
QPushButton:disabled, QToolButton:disabled, QPushButton#primary:disabled {background:#F0F2F5; border-color:#E6E9EE; color:#A1AAB5;}
QToolButton::menu-indicator {image:none; width:0;}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QDateTimeEdit, QTimeEdit, QPlainTextEdit, QTextEdit {background:#FFFFFF; border:1px solid #DDE2E9; border-radius:8px; padding:4px 8px; min-height:20px; selection-background-color:#35AFC8; selection-color:#FFFFFF;}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QDateTimeEdit:focus, QTimeEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {border:1px solid #35AFC8;}
QLineEdit:read-only {background:#F8FAFC;}
QComboBox::drop-down {border:0; width:22px;}
QComboBox::down-arrow {image:url(ARROW_ICON); width:12px; height:12px;}
QComboBox QAbstractItemView {background:#FFFFFF; border:1px solid #E3E7ED; outline:0; padding:4px; selection-background-color:#35AFC8; selection-color:#FFFFFF;}
QListWidget, QListView, QTableWidget, QTableView, QTreeWidget, QTreeView {background:#FFFFFF; border:1px solid #E3E7ED; border-radius:10px; outline:0; alternate-background-color:#F8FAFC; selection-background-color:#DDF1F6; selection-color:#0E5F70; gridline-color:#EEF1F4;}
QListWidget::item {padding:6px 6px; border-radius:6px;}
QTreeWidget::item {padding:4px 2px;}
QTreeWidget::item:selected, QListWidget::item:selected {background:#DDF1F6; color:#0E5F70;}
QHeaderView {background:transparent;}
QHeaderView::section {background:#F8FAFC; border:0; border-bottom:1px solid #E3E7ED; padding:6px 8px; color:#6B7785; font-weight:600; font-size:12px;}
QTabWidget::pane {border:1px solid #E3E7ED; border-radius:10px; background:#FFFFFF; top:-1px;}
QTabBar::tab {background:#EEF1F5; color:#4A5563; padding:6px 18px; margin:2px 1px; border:1px solid transparent; border-radius:7px;}
QTabBar::tab:selected {background:#FFFFFF; color:#1C2530; font-weight:600; border:1px solid #DDE2E9;}
QTabBar::tab:hover:!selected {background:#E6EAF0;}
QCheckBox, QRadioButton {spacing:7px; background:transparent;}
QCheckBox::indicator, QListView::indicator {width:17px; height:17px; border:1px solid #B9C2CD; border-radius:5px; background:#FFFFFF;}
QCheckBox::indicator:checked, QListView::indicator:checked {background:#35AFC8; border:1px solid #239BB3; image:url(CHECK_ICON);}
QCheckBox::indicator:disabled, QListView::indicator:disabled {background:#E9EDF2; border-color:#D3D9E1;}
QCheckBox:focus {outline:0;}
QSplitter::handle {background:transparent; width:6px; height:6px;}
QSlider::groove:horizontal {height:4px; background:#E3E7ED; border-radius:2px;}
QSlider::sub-page:horizontal {background:#35AFC8; border-radius:2px;}
QSlider::handle:horizontal {width:16px; height:16px; margin:-6px 0; background:#FFFFFF; border:1px solid #C9D1DA; border-radius:8px;}
QProgressBar {background:#E9EDF2; border:0; border-radius:4px; text-align:center; color:#1C2530;}
QProgressBar::chunk {background:#35AFC8; border-radius:4px;}
QScrollBar:vertical {background:transparent; width:10px; margin:2px;}
QScrollBar:horizontal {background:transparent; height:10px; margin:2px;}
QScrollBar::handle:vertical {background:#C9D1DA; border-radius:4px; min-height:28px;}
QScrollBar::handle:horizontal {background:#C9D1DA; border-radius:4px; min-width:28px;}
QScrollBar::handle:hover {background:#AEB8C4;}
QScrollBar::add-line, QScrollBar::sub-line {height:0; width:0;}
QScrollBar::add-page, QScrollBar::sub-page {background:transparent;}
QStatusBar {background:#F5F7FA; border-top:1px solid #E3E7ED; color:#6B7785; font-size:12px;}
QStatusBar::item {border:0;}
QMenuBar {background:qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #239BB3, stop:0.55 #35AFC8, stop:1 #6FCB5A); border:0; padding:3px 8px; color:#FFFFFF; font-size:14px; font-weight:700;}
QMenuBar::item {background:transparent; color:#FFFFFF; padding:6px 14px; margin:1px 2px; border-radius:8px;}
QMenuBar::item:selected {background:rgba(255,255,255,0.22);}
QMenuBar::item:pressed {background:#FFFFFF; color:#0E5F70;}
QLabel#menuLogo {background:transparent; padding:0 10px 0 4px;}
QMenu {background:#FFFFFF; border:1px solid #DDE2E9; border-radius:10px; padding:5px;}
QMenu::item {padding:7px 30px 7px 10px; border-radius:6px; color:#1C2530; background:transparent; font-weight:600;}
QMenu::icon {padding-left:6px;}
QMenu::item:selected {background:#35AFC8; color:#FFFFFF;}
QMenu::item:disabled {color:#A1AAB5; background:transparent;}
QMenu::item:checked {font-weight:600;}
QMenu::separator {height:1px; background:#EDF0F4; margin:4px 8px;}
QMenu::indicator {width:14px; height:14px; left:4px;}
QToolTip {background:#1C2530; color:#FFFFFF; border:0; border-radius:6px; padding:6px 9px;}
QCalendarWidget QToolButton {color:#1C2530; background:transparent; border:0; padding:4px;}
QCalendarWidget QAbstractItemView {background:#FFFFFF; selection-background-color:#35AFC8; selection-color:#FFFFFF;}
"""

STYLE = STYLE.replace("CHECK_ICON", (_ASSETS / "check_visible.svg").as_posix())
STYLE = STYLE.replace("ARROW_ICON", (_ASSETS / "chevron_down.svg").as_posix())

# Segmented control used for sheet/page switches (iOS UISegmentedControl).
SEGMENTED = (
    "QTabBar::tab {background:#EEF1F5; color:#4A5563; padding:6px 20px; margin:0 1px; border:1px solid transparent; border-radius:7px;}"
    "QTabBar::tab:selected {background:#FFFFFF; color:#1C2530; font-weight:600; border:1px solid #DDE2E9;}"
    "QTabBar::tab:hover:!selected {background:#E6EAF0;}"
)


def fluent_icon(name, color="#1C2530", size=20):
    """A Fluent System Icon (assets/fluent/<name>.svg) recoloured for the current surface."""
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    path = _ASSETS / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    data = path.read_text(encoding="utf-8").replace("#212121", color)
    icon = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(data.encode("utf-8"))).render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        icon.addPixmap(pixmap)
    return icon


def wordmark_pixmap(height=16, color="#FFFFFF"):
    """The official COWMATA wordmark (www.cowmata.com) in one colour, for the menu bar."""
    import re

    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    path = _ASSETS.parent / "brand" / "official-wordmark.svg"
    data = re.sub(r'fill="#[0-9A-Fa-f]{6}"', f'fill="{color}"', path.read_text(encoding="utf-8"))
    renderer = QSvgRenderer(QByteArray(data.encode("utf-8")))
    size = renderer.defaultSize()
    width = max(1, round(size.width() * height / max(1, size.height())))
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, width * 2, height * 2))
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


# Key commands and their icons (menu text -> Fluent icon name).
MENU_ICONS = {
    "打开工程…": "folder_open", "保存": "save", "导出": "share", "关闭工程": "dismiss",
    "撤销": "arrow_undo", "重做": "arrow_redo", "修改标签…": "tag", "批量修改…": "edit", "查找记录": "search",
    "台账": "table", "导入表格…": "arrow_upload", "同步修改": "arrow_sync", "上传设置…": "settings",
    "端侧数据…": "arrow_download", "数据归类": "folder_arrow_right", "下载设置…": "settings", "风险等级总览": "data_trending",
    "自动生成候选…": "sparkle", "完成本份…": "checkmark_circle", "协作": "people_team",
    "时间同步": "clock", "录像": "video", "视图": "window_new", "设置…": "settings",
    "快速开始": "book_open", "新手图文教程…": "book_open", "提示信息说明…": "info", "账号": "person", "关于": "info",
}


def decorate_menus(menubar):
    """Icons on the key commands and the wordmark in the menu bar's left corner."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    def visit(menu):
        for action in menu.actions():
            name = MENU_ICONS.get(action.text())
            if name and action.icon().isNull():
                action.setIcon(fluent_icon(name, "#35AFC8"))
                action.setIconVisibleInMenu(True)
            if action.menu() is not None:
                visit(action.menu())

    for top in menubar.actions():
        if top.menu() is not None:
            visit(top.menu())
    logo = QLabel(menubar)
    logo.setObjectName("menuLogo")
    logo.setPixmap(wordmark_pixmap(15))
    logo.setToolTip("COWMATA · www.cowmata.com")
    menubar.setCornerWidget(logo, Qt.Corner.TopLeftCorner)
    menubar._cowmata_logo = logo  # PySide does not keep the corner widget alive by itself
    logo.show()
    return logo
