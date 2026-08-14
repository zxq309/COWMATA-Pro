from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    os.chdir(app_dir)
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setOrganizationName("BovineMotionWorkbench")
    QCoreApplication.setApplicationName("牛尾环九轴桌面标注工具")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(sys.argv)
    application.setStyle("Fusion")

    from final_desktop_window import MainWindow

    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
