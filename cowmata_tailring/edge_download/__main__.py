"""Standalone preview: python -m cowmata_tailring.edge_download."""
import sys

from PySide6.QtWidgets import QApplication, QMainWindow

from . import install_menu

app = QApplication.instance() or QApplication(sys.argv)
window = QMainWindow()
window.setWindowTitle('端侧数据下载 · 独立预览')
window.resize(480, 120)
controller = install_menu(window, window.menuBar().addMenu('工具'))
window.show()
controller.open()
sys.exit(app.exec())
