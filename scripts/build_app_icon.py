"""Render the vendored MIT Phosphor cow into local Windows icon resources."""
import hashlib
import json
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    app = QGuiApplication.instance() or QGuiApplication([])
    root = Path(__file__).resolve().parents[1] / "assets/app-icon"
    original = (root / "cow-duotone.svg").read_bytes()
    renderer = QSvgRenderer(original.replace(b'fill="currentColor"', b'fill="#ffffff"'))
    assert renderer.isValid()
    tile = QImage(512, 512, QImage.Format.Format_ARGB32)
    tile.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tile)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(35, 20, 480, 500)
    gradient.setColorAt(0, QColor("#18b7a6"))
    gradient.setColorAt(0.6, QColor("#087f96"))
    gradient.setColorAt(1, QColor("#155bb0"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(QRectF(16, 16, 480, 480), 112, 112)
    renderer.render(painter, QRectF(91, 81, 330, 330))
    painter.end()
    assert tile.save(str(root / "cowmata.png"))
    with Image.open(root / "cowmata.png") as icon:
        icon.save(root / "cowmata.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    panel = QImage(164, 314, QImage.Format.Format_RGB32)
    panel.fill(QColor("#082e42"))
    painter = QPainter(panel)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(QRectF(20, 78, 124, 124), tile)
    painter.end()
    assert panel.save(str(root / "installer.bmp"))
    paths = ("cow-duotone.svg", "LICENSE.txt", "cowmata.png", "cowmata.ico", "installer.bmp")
    provenance = {
        "source": "https://github.com/phosphor-icons/core",
        "revision": "2b75f3ad12b420c9504ef05df8d2564a28f8500e",
        "source_path": "assets/duotone/cow-duotone.svg",
        "license": "MIT; see LICENSE.txt",
        "adaptation": "White cow on teal-blue rounded tile; existing corporate marks unchanged.",
        "sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths},
    }
    (root / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps(provenance))
    del app


if __name__ == "__main__":
    main()
