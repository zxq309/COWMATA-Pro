"""PySide6 download subtask window; all network and disk work stays off the UI."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDateTime, QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.ui.task_window import TaskWindow

from .core import CATEGORIES, CHINA, Cancelled, DownloadError, Job, Target, run_job


class Worker(QThread):
    message = Signal(str)
    failed = Signal(str)
    progress = Signal(int, int)
    cycle_done = Signal(object)

    def __init__(self, job, mode, interval, scheduled, tunnel=None, parent=None):
        super().__init__(parent)
        self.job, self.mode = job, mode
        self.interval, self.scheduled = interval, scheduled
        self.cancel = threading.Event()
        self.tunnel = tunnel

    def run(self):
        from .tunnel import Tunnel
        try:
            if self.mode == '定时':
                self.message.emit(f'已预约：{self.scheduled:%Y-%m-%d %H:%M:%S}（北京时间）；需保持标注工具运行')
                while datetime.now(CHINA) < self.scheduled:
                    if self.cancel.wait(min(1, max(0, (self.scheduled - datetime.now(CHINA)).total_seconds()))):
                        return
            while not self.cancel.is_set():
                try:
                    current = replace(self.job, end=datetime.now(CHINA).replace(microsecond=0)) if self.mode == '自动' else self.job
                    # Own and release only this cycle's tunnel. Automatic mode
                    # can reconnect after SSH/network/server recovery.
                    with Tunnel(self.tunnel, self.job.base_url, self.cancel, self.message.emit):
                        result = run_job(current, self.cancel, self.message.emit, self.progress.emit)
                    self.cycle_done.emit(result)
                except Cancelled:
                    raise
                except Exception as exc:
                    self.message.emit(f'本轮失败：{exc}')
                    self.failed.emit(f'本轮失败：{exc}')
                if self.mode != '自动' or self.cancel.is_set():
                    break
                self.message.emit(f'自动模式：{self.interval} 秒后检查新增及迟上传数据')
                if self.cancel.wait(self.interval):
                    break
        except Cancelled:
            self.message.emit('任务已停止')
        except Exception as exc:
            self.message.emit(f'任务失败：{exc}')
            self.failed.emit(f'任务失败：{exc}')


class DownloadDialog(TaskWindow):
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle('端侧数据下载')
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(850, 800)
        self.worker = None
        self.settings = settings or QSettings('COWMATA', 'EdgeDownload')
        try:
            self.profiles = json.loads(self.settings.value('farms', '[]'))
            if not isinstance(self.profiles, list):
                self.profiles = []
        except (ValueError, TypeError):
            self.profiles = []
        self.profiles = [p for p in self.profiles if isinstance(p, dict) and p.get('root')]
        self.active_index = -1
        outer = QVBoxLayout(self)
        self.inputs = QWidget()
        form = QFormLayout(self.inputs)
        self.farms = QComboBox()
        farm_row = QHBoxLayout()
        farm_row.addWidget(self.farms, 1)
        add_farm = QPushButton('选择 / 添加牧场…')
        add_farm.clicked.connect(self.add_farm)
        farm_row.addWidget(add_farm)
        form.addRow('牧场', farm_row)
        self.root_label = QLabel()
        self.root_label.setWordWrap(True)
        form.addRow('牧场根目录', self.root_label)
        self.server = QComboBox()
        self.server.setEditable(True)
        self.server.addItems(['http://device.cowmata.com:8010', 'http://127.0.0.1:18031'])
        form.addRow('下载服务器', self.server)
        self.category = QComboBox()
        self.category.addItems(CATEGORIES)
        form.addRow('采集类别', self.category)
        types = QHBoxLayout()
        self.motion, self.ppg = QCheckBox('Motion（九轴）'), QCheckBox('PPG（脉搏）')
        self.motion.setChecked(True)
        types.addWidget(self.motion)
        types.addWidget(self.ppg)
        self.temp = QCheckBox('温度（原始数据）')
        types.addWidget(self.temp)
        form.addRow('数据类型', types)
        self.mode = QComboBox()
        self.mode.addItems(['手动', '自动', '定时'])
        form.addRow('任务模式', self.mode)
        self.start_at, self.end_at, self.scheduled = (QDateTimeEdit() for _ in range(3))
        now = QDateTime.fromString(datetime.now(CHINA).strftime('%Y-%m-%d %H:%M:%S'), 'yyyy-MM-dd HH:mm:ss')
        for field in (self.start_at, self.end_at, self.scheduled):
            field.setDisplayFormat('yyyy-MM-dd HH:mm:ss')
            field.setCalendarPopup(True)
        self.start_at.setDateTime(now.addDays(-1))
        self.end_at.setDateTime(now)
        self.scheduled.setDateTime(now.addSecs(3600))
        period = QHBoxLayout()
        period.addWidget(self.start_at)
        period.addWidget(QLabel('至'))
        period.addWidget(self.end_at)
        form.addRow('采集范围（北京时间）', period)
        self.interval = QSpinBox()
        self.interval.setRange(10, 86400)
        self.interval.setValue(60)
        self.interval.setSuffix(' 秒')
        schedule = QHBoxLayout()
        schedule.addWidget(QLabel('自动检查间隔'))
        schedule.addWidget(self.interval)
        schedule.addWidget(QLabel('定时执行于'))
        schedule.addWidget(self.scheduled)
        form.addRow(schedule)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        form.addRow(self.hint)
        self.targets = QTableWidget(0, 3)
        self.targets.setHorizontalHeaderLabels(['完整设备编号', '牛耳标（可留空）', '现场记号（可留空）'])
        self.targets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.targets.setFixedHeight(125)
        form.addRow('下载对象', self.targets)
        rows = QHBoxLayout()
        for label, callback in [('添加一行', self.add_row), ('移除所选行', self.remove_rows),
                                ('从起始日期目录读取对象', self.import_targets)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            rows.addWidget(button)
        form.addRow(rows)
        self.ssh = QGroupBox('连接 3090 SSH 隧道（仅本机接口需要；已有隧道可不勾选）')
        self.ssh.setCheckable(True)
        self.ssh.setChecked(False)
        ssh_outer = QVBoxLayout(self.ssh)
        ssh_fields = QWidget()
        ssh_form = QFormLayout(ssh_fields)
        ssh_outer.addWidget(ssh_fields)
        ssh_fields.setVisible(False)
        self.ssh.toggled.connect(ssh_fields.setVisible)
        self.ssh_host = QLineEdit('administrator@61.177.77.222')
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(8022)
        self.ssh_key = QLineEdit(str(Path.home() / '.ssh/cowmata_remote_ed25519'))
        ssh_line = QHBoxLayout()
        ssh_line.addWidget(self.ssh_host)
        ssh_line.addWidget(QLabel('SSH 端口'))
        ssh_line.addWidget(self.ssh_port)
        ssh_form.addRow('用户@服务器', ssh_line)
        key_line = QHBoxLayout()
        key_line.addWidget(self.ssh_key)
        key_button = QPushButton('选择密钥…')
        key_button.clicked.connect(self.choose_key)
        key_line.addWidget(key_button)
        ssh_form.addRow('本机私钥路径', key_line)
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1, 65535)
        self.remote_port.setValue(8031)
        ssh_form.addRow('远端下载接口端口', self.remote_port)
        form.addRow(self.ssh)
        input_scroll = QScrollArea()
        input_scroll.setWidgetResizable(True)
        input_scroll.setWidget(self.inputs)
        input_scroll.setMinimumHeight(300)
        outer.addWidget(input_scroll, 3)
        self.path_hint = QLabel('保存：牧场 / 类别 / Motion、PPG 或 Temp / 日期 / 设备-牛耳标-现场记号 / JSON')
        self.path_hint.setWordWrap(True)
        outer.addWidget(self.path_hint)
        controls = QHBoxLayout()
        self.start_button, self.stop_button = QPushButton('开始下载'), QPushButton('停止任务')
        self.start_button.clicked.connect(self.start_task)
        self.stop_button.clicked.connect(self.stop_task)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        self.status = QLabel('就绪')
        controls.addWidget(self.status, 1)
        outer.addLayout(controls)
        self.progress_bar = QProgressBar()
        outer.addWidget(self.progress_bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setMinimumHeight(100)
        outer.addWidget(self.log, 1)
        footer = QLabel('关闭子窗口后任务继续；退出标注工具会停止任务。定时任务不唤醒关机或睡眠电脑。')
        outer.addWidget(footer)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.farms.currentIndexChanged.connect(self.load_farm)
        self.refresh_farms()
        self.mode_changed()

    @property
    def running(self):
        return self.worker is not None and self.worker.isRunning()

    def mode_changed(self):
        mode = self.mode.currentText()
        self.end_at.setEnabled(mode != '自动')
        self.interval.setEnabled(mode == '自动')
        self.scheduled.setEnabled(mode == '定时')
        self.start_button.setText('预约下载' if mode == '定时' else '开始下载')
        self.hint.setText({'手动': '执行一次；采集范围包含开始、不含结束。',
                           '自动': '立即下载，并按间隔补下数据；结束时间每轮推进到当前北京时间。',
                           '定时': '在指定北京时间执行一次，下载上面选定的采集范围。'}[mode]
                          + ' 3090 原始库只提供 Motion；PPG 和温度需选择支持相应数据的服务器。')

    def add_row(self, checked=False, values=('', '', '')):
        row = self.targets.rowCount()
        self.targets.insertRow(row)
        for column, value in enumerate(values):
            self.targets.setItem(row, column, QTableWidgetItem(value))

    def remove_rows(self):
        for row in sorted({item.row() for item in self.targets.selectedItems()}, reverse=True):
            self.targets.removeRow(row)

    def target_values(self):
        result = []
        for row in range(self.targets.rowCount()):
            values = tuple((self.targets.item(row, col).text().strip() if self.targets.item(row, col) else '') for col in range(3))
            if any(values):
                result.append(values)
        return result

    def snapshot(self):
        return dict(server=self.server.currentText().strip(), category=self.category.currentText(),
                    targets=self.target_values(), motion=self.motion.isChecked(), ppg=self.ppg.isChecked(), temp=self.temp.isChecked(),
                    interval=self.interval.value(), ssh_enabled=self.ssh.isChecked(),
                    ssh_host=self.ssh_host.text().strip(), ssh_port=self.ssh_port.value(),
                    ssh_key=self.ssh_key.text().strip(), remote_port=self.remote_port.value())

    def save_settings(self):
        if 0 <= self.active_index < len(self.profiles):
            self.profiles[self.active_index].update(self.snapshot())
        self.settings.setValue('farms', json.dumps(self.profiles, ensure_ascii=False))
        self.settings.setValue('selected', max(0, self.active_index))
        self.settings.sync()

    def refresh_farms(self, index=None):
        self.farms.blockSignals(True)
        self.farms.clear()
        for profile in self.profiles:
            root = Path(profile['root'])
            self.farms.addItem(f'{root.name} — {root}', str(root))
        if index is None:
            try:
                index = int(self.settings.value('selected', 0))
            except (TypeError, ValueError):
                index = 0
        self.active_index = -1
        self.farms.setCurrentIndex(min(index, len(self.profiles) - 1))
        self.farms.blockSignals(False)
        self.load_farm(self.farms.currentIndex())

    def load_farm(self, index):
        self.save_settings()
        self.active_index = index
        p = self.profiles[index] if 0 <= index < len(self.profiles) else {}
        self.root_label.setText(p.get('root', '请选择牧场根目录'))
        self.server.setCurrentText(p.get('server', 'http://device.cowmata.com:8010'))
        self.category.setCurrentText(p.get('category', CATEGORIES[0]))
        self.motion.setChecked(p.get('motion', True))
        self.ppg.setChecked(p.get('ppg', False))
        self.temp.setChecked(p.get('temp', False))
        self.interval.setValue(int(p.get('interval', 60)))
        self.ssh.setChecked(p.get('ssh_enabled', False))
        self.ssh_host.setText(p.get('ssh_host', 'administrator@61.177.77.222'))
        self.ssh_port.setValue(int(p.get('ssh_port', 8022)))
        self.ssh_key.setText(p.get('ssh_key', str(Path.home() / '.ssh/cowmata_remote_ed25519')))
        self.remote_port.setValue(int(p.get('remote_port', 8031)))
        self.targets.setRowCount(0)
        for row in p.get('targets', []):
            if isinstance(row, (list, tuple)) and len(row) == 3:
                self.add_row(values=tuple(str(v) for v in row))
        if not self.targets.rowCount():
            self.add_row()

    def add_farm(self):
        selected = QFileDialog.getExistingDirectory(self, '选择牧场根目录（其下是产犊、发情、怀孕等类别）')
        if selected:
            self.save_settings()
            root = str(Path(selected).resolve())
            found = next((i for i, p in enumerate(self.profiles) if Path(p['root']).resolve() == Path(root)), None)
            if found is None:
                self.profiles.append({'root': root})
                found = len(self.profiles) - 1
            self.refresh_farms(found)
            self.save_settings()

    def choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择本机 SSH 私钥')
        if path:
            self.ssh_key.setText(path)

    def import_targets(self):
        if self.active_index < 0:
            QMessageBox.information(self, '下载对象', '请先选择牧场')
            return
        root = Path(self.profiles[self.active_index]['root']) / self.category.currentText()
        date = self.start_at.dateTime().toString('yyyy-MM-dd')
        rows = set()
        for kind in ('Motion', 'PPG', 'Temp'):
            day = root / kind / date
            if day.is_dir():
                for folder in day.iterdir():
                    parts = folder.name.split('-', 2)
                    if folder.is_dir() and len(parts) == 3:
                        rows.add(tuple(parts))
        if not rows:
            QMessageBox.information(self, '下载对象', f'所选类别的 {date} 目录未找到设备-牛耳标-记号文件夹。可手动添加对象。')
            return
        self.targets.setRowCount(0)
        for row in sorted(rows):
            self.add_row(values=row)
        self.log.appendPlainText(f'已读取 {date} 的 {len(rows)} 个对象；跨日下载前请核对设备是否换绑。')

    @staticmethod
    def china_time(field):
        # Treat displayed wall-clock values explicitly as China time on any OS.
        return datetime.strptime(field.dateTime().toString('yyyy-MM-dd HH:mm:ss'), '%Y-%m-%d %H:%M:%S').replace(tzinfo=CHINA)

    def start_task(self):
        if self.running:
            return
        try:
            if self.active_index < 0:
                raise DownloadError('请先选择牧场')
            mode = self.mode.currentText()
            end = datetime.now(CHINA).replace(microsecond=0) if mode == '自动' else self.china_time(self.end_at)
            job = Job(self.server.currentText().strip().rstrip('/'), Path(self.profiles[self.active_index]['root']),
                      self.category.currentText(), tuple(Target(*row) for row in self.target_values()),
                      tuple(k for k, selected in [('motion', self.motion.isChecked()), ('pulse', self.ppg.isChecked()), ('temp', self.temp.isChecked())] if selected),
                      self.china_time(self.start_at), end)
            job.validate()
            scheduled = self.china_time(self.scheduled)
            if mode == '定时' and scheduled <= datetime.now(CHINA):
                raise DownloadError('定时执行时间必须晚于当前北京时间')
            tunnel = None
            if self.ssh.isChecked():
                tunnel = dict(host=self.ssh_host.text().strip(), port=self.ssh_port.value(),
                              key=self.ssh_key.text().strip(), remote_port=self.remote_port.value())
                from .tunnel import validate_config
                validate_config(tunnel, job.base_url)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, '无法开始下载', str(exc))
            return
        self.save_settings()
        self.inputs.setEnabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.status.setText('等待定时执行' if mode == '定时' else '下载中')
        self.log.appendPlainText(f'启动{mode}任务 · {job.farm.name} / {job.category}')
        self.worker = Worker(job, mode, self.interval.value(), scheduled, tunnel, self)
        self.worker.message.connect(self.log.appendPlainText)
        self.worker.failed.connect(self.status.setText)
        self.worker.progress.connect(self.update_progress)
        self.worker.cycle_done.connect(self.cycle_done)
        self.worker.finished.connect(self.task_finished)
        self.worker.start()

    def update_progress(self, done, total):
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat(f'本轮已处理 {done} 条（清单逐日查询）')

    def cycle_done(self, result):
        text = f'本轮下载 {result.saved} · 跳过 {result.skipped} · 失败 {result.failed}'
        if result.canceled:
            text = '已停止 · ' + text
        self.status.setText(text)
        self.log.appendPlainText(text)

    def stop_task(self):
        if self.running:
            self.worker.cancel.set()
            self.status.setText('正在停止，等待当前网络请求退出…')
            self.stop_button.setEnabled(False)

    def task_finished(self):
        self.inputs.setEnabled(True)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setFormat('任务已结束；详见下载日志')
        self.log.appendPlainText('任务已结束')
        if self.status.text() in ('下载中', '等待定时执行', '正在停止，等待当前网络请求退出…'):
            self.status.setText('任务已结束')
        old = self.worker
        self.worker = None
        if old:
            old.deleteLater()

    def reject(self):
        self.save_settings()
        self.hide()

    def closeEvent(self, event):
        self.save_settings()
        event.accept()
