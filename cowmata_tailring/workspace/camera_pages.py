"""Stable camera pages; browsing inventory never creates media decoders."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

VIEWS = tuple(f'视角{i:02}' for i in range(1, 21))


def camera_pages(names):
    names = list(dict.fromkeys(names))
    groups = []
    if any(name in VIEWS for name in names):
        for start, end in ((1,8),(9,16),(17,20)):
            groups.append((f'{start:02}–{end:02}',
                [v for v in VIEWS[start-1:end] if v in names]))
    others = [name for name in names if name not in VIEWS]
    for start in range(0,len(others),8):
        groups.append((f'其他 {start//8+1}',others[start:start+8]))
    return groups


class CameraPages(QWidget):
    selectionRequested = Signal(list)
    customRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups, self.buttons, self.names = [], [], []
        self._pending = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self._apply_pending)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0,0,0,0)
        self.row.addWidget(QLabel('视角分组'))
        self.status = QLabel('每次最多 8 路')
        self.row.addWidget(self.status,1)
        custom = QPushButton('自选视角…')
        custom.setToolTip('在素材列表自由组合，最多同时显示 8 路')
        custom.clicked.connect(self.customRequested)
        self.row.addWidget(custom)
        self.hide()

    def set_inventory(self, names):
        if list(names) == self.names:
            return
        self.timer.stop()
        self._pending = None
        self.names = list(names)
        self.groups = camera_pages(names)
        for button in self.buttons:
            self.row.removeWidget(button)
            button.deleteLater()
        self.buttons.clear()
        for index,(label,group) in enumerate(self.groups):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setEnabled(bool(group))
            button.setToolTip(('本组有素材：'+'、'.join(group)) if group else '该组未发现录像；保留视角编号')
            button.clicked.connect(lambda checked=False,i=index:self.request_page(i))
            self.row.insertWidget(index+1,button)
            self.buttons.append(button)
        self.setVisible(len(names)>8 or any(v in names for v in VIEWS[8:]))
        self.set_selected([])

    def set_selected(self, names):
        for button,(_,group) in zip(self.buttons,self.groups):
            button.setChecked(bool(names) and set(names)==set(group))
        self.status.setText(f'已选 {len(names)} 路 / 素材 {len(self.names)} 路 · 最多 8 路')

    def request_page(self, index):
        if 0 <= index < len(self.groups) and self.groups[index][1]:
            self._pending = index
            self.status.setText('正在切换 · 保持当前时间与标注')
            self.timer.start()  # A newer click replaces the pending selection.

    def _apply_pending(self):
        self.timer.stop()
        index,self._pending = self._pending,None
        if index is not None and index < len(self.groups):
            names = list(self.groups[index][1])
            self.selectionRequested.emit(names)
            self.set_selected(names)

    def closeEvent(self,event):
        self.timer.stop()
        self._pending=None
        super().closeEvent(event)
