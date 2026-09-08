"""Package the unmodified official COWMATA favicon at Windows icon sizes."""
import hashlib
import json
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    app = QGuiApplication.instance() or QGuiApplication([])
    root = Path(__file__).resolve().parents[1] / "assets/app-icon"
    source = root.parent / "brand/official-favicon.ico"
    # Resampling only: preserve the corporate glyph and its original colour.
    tile = QImage(str(source)).scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    assert not tile.isNull()
    assert tile.save(str(root / "cowmata.png"))
    with Image.open(root / "cowmata.png") as icon:
        icon.save(root / "cowmata.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    panel = QImage(164, 314, QImage.Format.Format_RGB32)
    panel.fill(QColor("#f3f7f0"))
    painter = QPainter(panel)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(QRectF(20, 78, 124, 124), tile)
    painter.end()
    assert panel.save(str(root / "installer.bmp"))
    header = QImage(150, 57, QImage.Format.Format_RGB32)
    header.fill(QColor("#f3f7f0"))
    painter = QPainter(header)
    renderer = QSvgRenderer(str(root.parent / "brand/official-wordmark.svg"))
    assert renderer.isValid()
    renderer.render(painter, QRectF(8, 20, 134, 17))
    painter.end()
    assert header.save(str(root / "installer-header.bmp"))
    paths = ("cowmata.png", "cowmata.ico", "installer.bmp", "installer-header.bmp")
    provenance = {
        "source": "https://www.cowmata.com/en/favicon.ico",
        "retrieved": "2026-09-09",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "license": "Corporate trademark, rights retained by COWMATA; see NOTICE",
        "adaptation": "Original official favicon, format/size conversion only. Not the former Phosphor cow.",
        "sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths},
    }
    (root / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps(provenance))
    del app


if __name__ == "__main__":
    main()
