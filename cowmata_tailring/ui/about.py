"""Product identity without changing existing source or dependency licenses."""
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from cowmata_tailring import __build__, __version__
from cowmata_tailring.ui.i18n import t

COMPANY = "杨凌园上园智能科技有限公司"
WEBSITE = "https://www.cowmata.com/"
RELEASES = "https://github.com/zxq309/cattle-tail-ring-annotator/releases"
ABOUT_HTML = (
    "<p>软件版本：<b>{version}</b><br>构建标识：{build}</p>"
    "<p>奶牛行为真值标注工作台：九轴与多视角录像同步、候选事件审核、"
    "可追溯标签导出与历史回看。模型候选不等于人工确认的真值。</p>"
    "<p>所属公司：{company}<br><a href='{website}'>公司官网</a> · "
    "<a href='mailto:service@cowmata.com'>service@cowmata.com</a></p>"
    "<p>公司名称、COWMATA 品牌与原有公司标识的权利说明见 NOTICE。"
    "源代码保留 MIT 许可证及原署名；第三方组件遵循各自许可证。</p>"
    "<p><a href='{releases}'>查看 GitHub 发布版本与更新说明</a><br>"
    "启动先联网检查更新；有新版时自动更新，完成后才能标注。已打开工程可离线继续使用；无更新器的旧版需手动升级一次。</p>"
)


def create_about(parent=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle(t("关于"))
    dialog.resize(640, 530)
    layout = QVBoxLayout(dialog)
    title = QLabel("COWMATA Annotator")
    title.setStyleSheet("font-size:24px; font-weight:600; color:#436d25; padding:8px")
    layout.addWidget(title)
    body = QTextBrowser()
    body.setOpenExternalLinks(True)
    body.setHtml(t(ABOUT_HTML).format(version=escape(__version__), build=escape(__build__),
                                    company=escape(t(COMPANY)), website=WEBSITE, releases=RELEASES))
    layout.addWidget(body, 1)
    root = Path(__file__).resolve().parents[2]
    license_text = QTextBrowser()
    license_text.setMaximumHeight(130)
    license_text.setPlainText("\n\n".join((root / name).read_text(encoding="utf-8")
                                         for name in ("LICENSE", "NOTICE") if (root / name).is_file()))
    license_text.setAccessibleName(t("许可证与版权声明"))
    layout.addWidget(license_text)
    tutorial_path = root / "docs" / "quick-start-illustrated.pdf"
    tutorial = QPushButton(t("新手图文教程…"))
    tutorial.setEnabled(tutorial_path.is_file())
    tutorial.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(tutorial_path))))
    layout.addWidget(tutorial)
    from cowmata_tailring.app.update_ui import tr
    update = QPushButton(tr("版本与更新…", "Version and updates…"))
    update.setEnabled(parent is not None and hasattr(parent, "updater"))
    if update.isEnabled():
        update.clicked.connect(lambda: (dialog.close(), parent.updater.open_dialog()))
    layout.addWidget(update)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    return dialog


def show_about(parent=None):
    create_about(parent).exec()
