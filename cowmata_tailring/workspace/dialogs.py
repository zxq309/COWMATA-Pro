from __future__ import annotations

import threading

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.media.timeline import MediaTimelineIndex

from .clocks import Anchor, ClockMap, wall_ms, wall_text
from .ocr import TimestampOCR
from .probe import extract_frame


class RegionCanvas(QWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.roi = None
        self.begin = None
        self.end = None
        self.setMinimumSize(600, 320)
        self.setMouseTracking(True)

    def image_rect(self):
        if self.image.isNull():
            return QRectF(self.rect())
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height())

    def normal(self, point):
        rect = self.image_rect()
        return QPointF(max(0, min(1, (point.x() - rect.x()) / rect.width())),
                       max(0, min(1, (point.y() - rect.y()) / rect.height())))

    def mousePressEvent(self, event):
        self.begin = self.normal(event.position())
        self.end = self.begin

    def mouseMoveEvent(self, event):
        if self.begin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.end = self.normal(event.position())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.begin is not None:
            self.end = self.normal(event.position())
            rect = QRectF(self.begin, self.end).normalized()
            if rect.width() > .005 and rect.height() > .005:
                self.roi = [rect.left(), rect.top(), rect.right(), rect.bottom()]
                self.changed.emit(self.roi)
            self.begin = self.end = None
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111722"))
        rect = self.image_rect()
        if not self.image.isNull():
            painter.drawImage(rect, self.image)
        roi = self.roi
        if self.begin is not None and self.end is not None:
            selection = QRectF(self.begin, self.end).normalized()
            roi = [selection.left(), selection.top(), selection.right(), selection.bottom()]
        if roi:
            painter.setPen(QPen(QColor("#ffc64c"), 2))
            painter.drawRect(QRectF(rect.x() + roi[0] * rect.width(), rect.y() + roi[1] * rect.height(),
                                    (roi[2] - roi[0]) * rect.width(), (roi[3] - roi[1]) * rect.height()))


class SourceTimeDialog(QDialog):
    resultReady = Signal(object)
    taskFailed = Signal(str)

    def __init__(self, catalog, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle("录像时间核验 · 四角识别 / 框选 / 人工读数")
        self.resize(1000, 750)
        self.catalog, self.row = catalog, row
        self.frame = None
        self.actual_ms = 0.0
        self.report = None
        self.generation = 0
        self.alive = True
        self.timeline = MediaTimelineIndex.from_dict(row["metadata"]["timeline"]) if row["metadata"].get("timeline") else None
        self.metadata = row["metadata"]
        layout = QVBoxLayout(self)
        self.tip = QLabel("拖动框选时间戳。至少核对两个相隔较远的读数；单点只供粗定位。原始录像不会修改。")
        self.tip.setWordWrap(True)
        layout.addWidget(self.tip)
        self.canvas = RegionCanvas()
        self.canvas.roi = self.metadata.get("roi")
        preview = catalog.meta / self.metadata.get("preview", "not-present")
        if preview.is_file():
            self.canvas.image = QImage(str(preview))
        layout.addWidget(self.canvas, 1)
        form = QFormLayout()
        self.position = QDoubleSpinBox()
        self.position.setDecimals(3)
        self.position.setRange(0, max(0, self.metadata.get("duration_ms", 0) / 1000 - .1))
        self.position.setSuffix(" 秒")
        form.addRow("文件内位置", self.position)
        self.timestamp = QLineEdit()
        self.timestamp.setPlaceholderText("2026-08-03 12:44:58（日期必须人工确认）")
        form.addRow("画面读数", self.timestamp)
        from .demand import camera_name
        self.camera = QLineEdit(camera_name(row,getattr(parent,'settings',{}).get('camera_overrides')))
        form.addRow("逻辑视角名称", self.camera)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        for title, handler in (("读取这个位置的画面", self.load_frame), ("识别框选区域 / 四角", self.recognize),
                               ("确认当前读数", self.accept_reading)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["播放位置（秒）", "人工确认的画面时间", "来源"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(140)
        layout.addWidget(self.table)
        self.readings = list(self.metadata.get("manual_readings", []))
        self.refresh_readings()
        end = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        end.accepted.connect(self.validate_accept)
        end.rejected.connect(self.reject)
        layout.addWidget(end)
        self.resultReady.connect(self.on_result)
        self.taskFailed.connect(self.tip.setText)

    def background(self, function, kind):
        self.generation += 1
        generation = self.generation
        self.tip.setText("后台读取中，请稍候；可以取消，不会修改原视频。")

        def run():
            try:
                value = function()
                if self.alive:
                    self.resultReady.emit((generation, kind, value))
            except Exception as exc:
                if self.alive:
                    self.taskFailed.emit(str(exc))

        threading.Thread(target=run, daemon=True).start()

    def load_frame(self):
        position = self.position.value() * 1000
        self.background(lambda: extract_frame(self.catalog.source_path(self.row["path"]), position, self.timeline, cancelled=lambda: not self.alive), "frame")

    def recognize(self):
        if self.frame is None:
            self.tip.setText("请先读取这个位置的画面，再框选识别")
            return
        frame, roi = self.frame.copy(), self.canvas.roi
        self.background(lambda: TimestampOCR().recognize(frame, filename=self.row["path"], roi=roi), "ocr")

    def on_result(self, result):
        generation, kind, value = result
        if generation != self.generation:
            return
        if kind == "frame":
            self.frame, self.actual_ms = value
            data = self.frame.tobytes("raw", "RGB")
            self.canvas.image = QImage(data, self.frame.width, self.frame.height, self.frame.width * 3, QImage.Format.Format_RGB888).copy()
            self.canvas.update()
            self.tip.setText(f"已解码实际位置 {self.actual_ms / 1000:.3f} 秒。框选时间戳后可识别，也可直接输入看到的读数。")
        else:
            self.report = value
            if value.get("timestamp"):
                self.timestamp.setText(value["timestamp"])
            self.tip.setText("请核对识别文字再点确认。" + "；".join(value["warnings"]))

    def accept_reading(self):
        try:
            if self.frame is None:
                raise ValueError("请先读取画面，不能把请求秒数当成实际帧位置")
            stamp = wall_ms(self.timestamp.text())
            self.readings = [r for r in self.readings if abs(r["media_ms"] - self.actual_ms) > .01]
            self.readings.append({"media_ms": self.actual_ms, "wall_ms": stamp, "source": "manual",
                                  "ocr": self.report, "roi": self.canvas.roi})
            self.readings.sort(key=lambda r: r["media_ms"])
            self.refresh_readings()
        except ValueError as exc:
            self.tip.setText(str(exc))

    def refresh_readings(self):
        self.table.setRowCount(len(self.readings))
        for i, reading in enumerate(self.readings):
            for j, text in enumerate((f"{reading['media_ms'] / 1000:.3f}", wall_text(reading["wall_ms"]), "人工确认")):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, j, item)

    def validate_accept(self):
        try:
            from .clocks import manual_video_metadata
            manual_video_metadata(self.metadata,self.readings)
            if not self.camera.text().strip():
                raise ValueError("请填写逻辑视角名称")
            self.accept()
        except ValueError as exc:
            self.tip.setText(str(exc))

    def done(self, result):
        self.alive = False
        super().done(result)


class MappingDialog(QDialog):
    def __init__(self, mapping: ClockMap, parent=None, *, camera=False):
        super().__init__(parent)
        self.setWindowTitle("相机时钟 → 参考时钟校准" if camera else "九轴 → 视频同步锚点与未确认区间")
        self.resize(850, 500)
        self.camera = camera
        self.value = mapping
        layout = QVBoxLayout(self)
        tip = QLabel("每行一对人工对应点。相邻点之间分段线性插值；首尾之外是外推。保存修订不会移动已有标签，已有真值需复核。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        # Device-clock origins are not human calibration observations.
        manual_anchors = mapping.anchors if mapping.basis == "manual" else []
        self.table = QTableWidget(len(manual_anchors), 2)
        self.table.setHorizontalHeaderLabels(["相机画面时间" if camera else "九轴内部秒数", "录像参考时间"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 310)
        for i, anchor in enumerate(manual_anchors):
            self.table.setItem(i, 0, QTableWidgetItem(wall_text(anchor.source_ms) if camera else str(anchor.source_ms / 1000)))
            self.table.setItem(i, 1, QTableWidgetItem(wall_text(anchor.reference_ms)))
        layout.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("增加锚点")
        add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        remove = QPushButton("删除选中锚点")
        remove.clicked.connect(self.remove_anchor)
        row.addWidget(add)
        row.addWidget(remove)
        layout.addLayout(row)
        self.breaks = QLineEdit("; ".join(f"{a / 1000:g},{b / 1000:g}" for a, b in mapping.breaks))
        if not camera:
            layout.addWidget(QLabel("未确认的九轴区间（秒），例如 100,200; 900,950；不跨这些区间确认真值"))
            layout.addWidget(self.breaks)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.validate)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def remove_anchor(self):
        if self.table.currentRow() >= 0 and QMessageBox.question(self, "删除锚点", "删除这一同步锚点？已有版本仍保留在历史记录中。") == QMessageBox.StandardButton.Yes:
            self.table.removeRow(self.table.currentRow())

    def validate(self):
        try:
            anchors = []
            for i in range(self.table.rowCount()):
                left, right = self.table.item(i, 0), self.table.item(i, 1)
                if left is None or right is None:
                    raise ValueError("请填写每行的两个时间")
                source = wall_ms(left.text()) if self.camera else float(left.text()) * 1000
                anchors.append(Anchor(source, wall_ms(right.text()), {"source": "manual_table"}))
            breaks = []
            if not self.camera:
                for item in self.breaks.text().split(";"):
                    if item.strip():
                        a, b = map(float, item.split(","))
                        breaks.append((a * 1000, b * 1000))
            self.value = ClockMap(anchors, breaks=breaks)
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "请检查同步数据", str(exc))
