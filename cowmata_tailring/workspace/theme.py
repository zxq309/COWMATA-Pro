"""Shared static Qt theme; no window/controller dependency."""

STYLE = """
QMainWindow, QWidget {background:#edf3f2; color:#223c41; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:13px;}
QFrame#card, QWidget#signalCard, QFrame#sourcePanel, QFrame#eventPanel {background:white; border:1px solid #dce7e5; border-radius:10px;}
QLabel {background:transparent; border:0;}
QLabel#brand {font-family:'Segoe UI'; font-size:21px; font-weight:700; color:#0e766f; letter-spacing:2px;}
QLabel#sectionTitle {font-weight:600; color:#284c50;}
QPushButton, QToolButton {background:#ffffff; border:1px solid #d6e3e1; border-radius:7px; padding:5px 11px; min-height:20px;}
QPushButton:hover, QToolButton:hover {background:#e2f2ef; border-color:#9bcfc5;}
QPushButton:pressed, QToolButton:pressed {background:#cbe8e0;}
QPushButton:checked, QToolButton:checked {background:#d8ede7; color:#00695e; border-color:#8bbfb5; font-weight:600;}
QPushButton#primary {background:#087e72; border-color:#087e72; color:white; font-weight:600;}
QPushButton#primary:hover {background:#096b63;}
QPushButton:disabled {color:#92a5a3; background:#f0f4f3;}
QComboBox, QLineEdit, QDoubleSpinBox {background:white; border:1px solid #d6e3e1; border-radius:6px; padding:4px 7px; min-height:20px; selection-background-color:#128579;}
QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus {border-color:#0c9888;}
QComboBox::drop-down {border:0; width:22px;}
QListWidget, QTableWidget {background:white; border:0; outline:0; alternate-background-color:#f5f8f7; selection-background-color:#daeee7; selection-color:#164b46;}
QListWidget::item {padding:7px 4px; border-radius:5px;}
QHeaderView::section {background:#f0f6f4; border:0; padding:7px; color:#50706f;}
QCheckBox {spacing:6px; background:transparent;}
QCheckBox::indicator {width:14px; height:14px; border:1px solid #95bcb2; border-radius:3px; background:white;}
QCheckBox::indicator:checked {background:#087e72; border:2px solid #5fc3a9;}
QSplitter::handle {background:#e1eae7; width:3px; height:3px;}
QSlider::groove:horizontal {height:4px; background:#d5e4df; border-radius:2px;}
QSlider::sub-page:horizontal {background:#21a791; border-radius:2px;}
QSlider::handle:horizontal {width:12px; margin:-4px 0; background:#078372; border-radius:6px;}
QScrollBar:vertical {background:#edf3f1; width:10px; margin:0;}
QScrollBar::handle:vertical {background:#bbcec7; border-radius:5px; min-height:24px;}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0;}
QStatusBar {background:#e4eeeb; border-top:1px solid #d3e3dc; font-size:11px;}
QMenu {background:white; border:1px solid #cbded7; padding:5px;}
QMenu::item {padding:7px 22px; border-radius:4px;}
QMenu::item:selected {background:#e2f2eb;}
QToolTip {color:#23463f; background:#fffffb; border:1px solid #bcd6cc; padding:8px;}
"""
