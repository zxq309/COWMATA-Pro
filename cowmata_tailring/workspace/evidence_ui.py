"""Bounded background capture, one-image-per-camera review and archive checklist."""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .archive import verify_archive
from .clocks import ClockMap, wall_text
from .evidence import (
    capture_frames,
    context_matches,
    event_context,
    read_image,
    store_bundle,
    suggest_time,
)
from .storage import atomic_json


class EvidenceGallery(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setMinimumHeight(180)
        self.set_bundle(None)

    def set_bundle(self, bundle, root=None, blobs=None):
        canvas = QWidget()
        grid = QGridLayout(canvas)
        if not bundle:
            grid.addWidget(QLabel("此标签尚未留存证据图；原录像不在本机时仍可查看波形与标签。"), 0, 0)
        else:
            heading = QLabel(f"目标牛：{bundle.get('context', {}).get('cow_id', '')} · 对应时刻 {wall_text(bundle['reference_ms'])}\n"
                             "静态辅助证据 · 不参与算法 · 不代表整段动作复核")
            heading.setWordWrap(True)
            grid.addWidget(heading, 0, 0, 1, 2)
            for index, item in enumerate(bundle.get("items", [])):
                tile = QWidget()
                layout = QVBoxLayout(tile)
                title = QLabel(item["camera"])
                title.setWordWrap(True)
                layout.addWidget(title)
                try:
                    if item["status"] != "captured":
                        raise ValueError(item.get("reason", "无可用原片"))
                    payload = blobs[item["sha256"]] if blobs is not None else read_image(root, item)
                    pixmap = QPixmap()
                    if not pixmap.loadFromData(payload, "JPEG"):
                        raise ValueError("图像无法显示")
                    button = QPushButton()
                    button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                    button.setIcon(QIcon(pixmap))
                    button.setIconSize(QSize(440, 248))
                    button.setMinimumHeight(180)
                    button.setToolTip("点击查看原始分辨率证据图")
                    button.clicked.connect(lambda checked=False, p=pixmap, c=item["camera"]: self.enlarge(p, c))
                    layout.addWidget(button)
                    caption = QLabel(f"实际帧 {wall_text(item['reference_ms'])} · 偏差 {item['delta_ms']:+.1f} ms\n"
                                     f"{item['width']} × {item['height']} · 点击放大")
                    caption.setWordWrap(True)
                    if item.get("camera_clock_basis") == "shared_reference_assumed":
                        caption.setText(caption.text() + "\n沿用工程参考时钟，请核对视角是否同步")
                    layout.addWidget(caption)
                except (OSError, ValueError, KeyError) as exc:
                    missing = QLabel("不可用：" + str(exc))
                    missing.setWordWrap(True)
                    layout.addWidget(missing)
                grid.addWidget(tile, 1 + index // 2, index % 2)
        previous = self.takeWidget()
        self.setWidget(canvas)
        self.fit_images()
        if previous:
            previous.deleteLater()

    def fit_images(self):
        if self.widget():
            width = max(100, (self.viewport().width() - 70) // 2)
            for button in self.widget().findChildren(QPushButton):
                button.setIconSize(QSize(width, round(width * 9 / 16)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_images()

    def enlarge(self, pixmap, camera):
        dialog = QDialog(self)
        dialog.setWindowTitle(camera + " · 原始分辨率证据图（可滚动）")
        dialog.resize(1200, 820)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        label = QLabel()
        label.setPixmap(pixmap)
        scroll.setWidget(label)
        layout.addWidget(scroll)
        dialog.exec()


class CaptureDialog(QDialog):
    def __init__(self, owner, event):
        super().__init__(owner)
        self.owner, self.label_event = owner, event
        self.context = event_context(owner.work, event)
        self.clock = ClockMap.from_dict(copy.deepcopy(owner.work.clock.to_dict()))
        self.settings = copy.deepcopy(owner.settings)
        self.rows = copy.deepcopy(owner.rows)
        self.cameras = owner.checked_cameras()
        self.root, self.meta = owner.catalog.root, owner.catalog.meta
        self.motion = owner.motion
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evidence-review")
        self.future = None
        self.bundle, self.blobs = None, None
        self.operation = ""
        self.setWindowTitle("留存多视角证据 · 每个选中视角一张")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(1020, 780)
        outer = QVBoxLayout(self)
        explanation = QLabel("波形明显点只是建议；请确认画面中的目标牛和代表时刻。\n"
                             "从原录像提取同一对应时刻，不截图预览画面；保存后不会修改标签起止位置。")
        explanation.setWordWrap(True)
        outer.addWidget(explanation)
        bar = QHBoxLayout()
        self.position = QDoubleSpinBox()
        self.position.setDecimals(3)
        self.position.setSuffix(" s · 原九轴位置")
        self.position.setRange(max(0, event.t0) / 1000, min(self.motion.duration_ms, event.t1 if event.t1 is not None else event.t0) / 1000)
        self.suggested = suggest_time(self.motion, self.position.minimum() * 1000, self.position.maximum() * 1000)
        self.position.setValue(self.suggested / 1000)
        self.position.valueChanged.connect(self.invalidate)
        bar.addWidget(self.position)
        self.suggest = QPushButton("波形建议点")
        self.suggest.clicked.connect(lambda: self.position.setValue(self.suggested / 1000))
        bar.addWidget(self.suggest)
        self.current = QPushButton("当前视频时刻")
        self.current.clicked.connect(self.use_current)
        bar.addWidget(self.current)
        self.extract = QPushButton("提取 / 更新图片")
        self.extract.clicked.connect(self.begin_capture)
        bar.addWidget(self.extract)
        outer.addLayout(bar)
        self.gallery = EvidenceGallery()
        outer.addWidget(self.gallery, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.checked = QCheckBox("我已核对代表时刻与目标牛；已了解缺失/遮挡视角，截图不替代整段录像")
        self.checked.toggled.connect(self.update_save)
        outer.addWidget(self.checked)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save = QPushButton("保存证据图")
        self.save.setEnabled(False)
        self.save.clicked.connect(self.begin_save)
        buttons.addWidget(self.save)
        cancel = QPushButton("关闭")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        outer.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        owner.board.play(False)
        QTimer.singleShot(0, self.begin_capture)

    def invalidate(self):
        self.bundle, self.blobs = None, None
        self.checked.setChecked(False)
        self.save.setEnabled(False)
        self.status.setText("时刻已调整，请重新提取图片。")

    def use_current(self):
        value = self.clock.map(self.owner.board.reference_ms, inverse=True) / 1000
        if not self.position.minimum() <= value <= self.position.maximum():
            self.status.setText("当前视频位置不在所选标签区间，请先回看标签内的时刻。")
            return
        self.position.setValue(value)

    def busy(self, value):
        for widget in (self.position, self.suggest, self.current, self.extract, self.checked):
            widget.setEnabled(not value)
        self.save.setEnabled(False)

    def update_save(self):
        self.save.setEnabled(self.future is None and self.checked.isChecked() and bool(self.bundle)
                             and any(i["status"] == "captured" for i in self.bundle["items"]))

    def begin_capture(self):
        if self.future is not None or self.stop.is_set():
            return
        self.invalidate()
        self.busy(True)
        self.status.setText("正在后台提取原始帧（最多两路同时解码）…")
        self.operation = "capture"
        self.future = self.pool.submit(capture_frames, self.root, self.rows, self.settings, self.clock,
                                       self.context, self.position.value() * 1000, self.cameras, cancelled=self.stop.is_set)

    def begin_save(self):
        if not self.save.isEnabled():
            return
        self.bundle["human_checked"] = True
        self.busy(True)
        self.operation = "save"
        self.future = self.pool.submit(store_bundle, self.meta, self.bundle, self.blobs)

    def poll(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        self.busy(False)
        try:
            result = future.result()
            if self.operation == "capture":
                self.bundle, self.blobs = result
                self.gallery.set_bundle(self.bundle, blobs=self.blobs)
                count = sum(i["status"] == "captured" for i in self.bundle["items"])
                self.status.setText(f"已提取 {count}/{len(self.cameras)} 个视角；请检查画面，缺失视角不会伪造截图。")
            else:
                owner = self.owner
                if (not owner.work or self.label_event not in owner.work.project.events or not context_matches(result, owner.work, self.label_event)
                        or self.settings.get("camera_maps", {}) != owner.settings.get("camera_maps", {})):
                    raise ValueError("标签、牛号或同步关系已变化，证据未绑定，请重新提取")
                if any(i.get("status") == "captured" and not owner.catalog.source_path(i["video_path"]).is_file()
                       for i in result["items"]):
                    raise ValueError("核对期间录像已离开本机，请刷新后重新提取")
                owner.work.checkpoint()
                self.label_event.extras["screenshots"] = result
                owner.dirty = True
                owner.save_current()
                if owner.dirty:
                    raise ValueError("图片已写入，但标签保存失败，请勿关闭工程")
                owner.tell("多视角证据图已保存；导出标注时会一起复制到同级“证据”文件夹。")
                self.accept()
            self.update_save()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.status.setText(str(exc))
            self.update_save()

    def done(self, result):
        if self.future is not None and self.operation == "save":
            self.status.setText("正在保存证据，请等待写入完成再关闭。")
            return
        self.stop.set()
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)

    def closeEvent(self, event):
        if self.future is not None and self.operation == "save":
            event.ignore()
            return
        self.reject()
        event.accept()


class ArchiveDialog(QDialog):
    progress = Signal(str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.root = owner.catalog.root
        self.rows = copy.deepcopy(owner.rows)
        self.report = None
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-check")
        self.future = None
        self.setWindowTitle("录像归档核验 · 不移动、不删除任何原片")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(820, 450)
        outer = QVBoxLayout(self)
        label = QLabel("先把完整录像复制到另一台电脑或归档磁盘，再选副本目录。\n"
                       "本工具核对原片与副本的 SHA-256；不会自动删除本机录像，也不会把缺失录像当作负样本。")
        label.setWordWrap(True)
        outer.addWidget(label)
        self.choose = QPushButton("选择归档副本，开始核验…")
        self.choose.clicked.connect(self.begin)
        outer.addWidget(self.choose)
        self.status = QLabel("尚未核验。重要录像建议另留一份独立备份。")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress.connect(self.status.setText)
        outer.addWidget(self.status, 1)
        self.spot = QCheckBox("我已从归档副本抽查播放，确认可正常打开")
        self.evidence = QCheckBox("我已导出并打开检查本批标签与证据图，确认没有漏标或漏存")
        outer.addWidget(self.spot)
        outer.addWidget(self.evidence)
        self.save = QPushButton("保存核验记录")
        self.save.clicked.connect(self.save_report)
        self.save.setEnabled(False)
        outer.addWidget(self.save)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        outer.addWidget(close)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def begin(self):
        directory = QFileDialog.getExistingDirectory(self, "选择原录像的独立归档副本目录")
        if not directory:
            return
        self.choose.setEnabled(False)
        self.save.setEnabled(False)
        self.spot.setChecked(False)
        self.evidence.setChecked(False)
        self.future = self.pool.submit(verify_archive, self.root, self.rows, Path(directory),
                                       cancelled=self.stop.is_set, progress=self.progress.emit)

    def poll(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        self.choose.setEnabled(True)
        try:
            self.report = future.result()
            count = sum(r["status"] == "verified" for r in self.report["files"])
            issues = [r["source_path"] + "：" + r.get("reason", "") for r in self.report["files"] if r["status"] != "verified"]
            self.status.setText(f"一致副本 {count}/{len(self.report['files'])}；未入索引录像 {len(self.report['unindexed_videos'])}。\n"
                                + ("内容核验通过；仍需抽查播放及检查标注证据。" if self.report["all_verified"] else "未全部通过，请勿据此清理本机录像。")
                                + "\n" + "\n".join(issues[:6]))
            self.save.setEnabled(True)
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def save_report(self):
        if not self.report or self.owner.catalog.root != self.root:
            return
        self.report["playback_spot_checked"] = self.spot.isChecked()
        self.report["annotation_evidence_checked"] = self.evidence.isChecked()
        try:
            atomic_json(self.owner.catalog.meta / "录像归档核验.json", self.report)
            self.owner.settings["video_archive"] = copy.deepcopy(self.report)
            self.owner.dirty = True
            self.owner.save_current()
            if self.owner.dirty:
                raise ValueError("工程设置尚未保存，请处理保存错误")
            self.status.setText("核验记录已保存。请再次导出需要携带归档线索的标签。\n"
                                + ("两项人工检查已确认；如需腾空间，由你在文件管理器中清理本机副本。"
                                   if self.report["all_verified"] and self.spot.isChecked() and self.evidence.isChecked()
                                   else "核验或人工检查未完成，请暂不清理本机录像。"))
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def done(self, result):
        self.stop.set()
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)

    def closeEvent(self, event):
        self.reject()
        event.accept()
