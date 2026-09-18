"""Batched task tables; codecs and filesystem work stay in the worker process."""
from __future__ import annotations

import time
from itertools import islice
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
)

from .dahua_run import PHASES, STATES

METHODS = {"stream_copy": "快速封装", "h264_nvenc": "NVIDIA 硬件编码",
           "h264_qsv": "Intel 硬件编码", "libx264": "CPU 编码", "encoded": "编码转换"}


class DahuaRunTables(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.records, self.outputs, self.pending = {}, {}, {}
        self.positions = {}
        self.output_positions = {}
        self.started = None
        self.active = False
        self.elapsed = 0
        self.summary = QLabel("等待开始 · 每段完成后即归档")
        self.live = self.make_table(["状态", "开始时间", "视角", "原始来源", "归档目标", "耗时 / 秒", "当前阶段 / 说明"])
        self.timing = self.make_table(["视角", "原始来源", "读取 / 秒", "转换 / 秒", "校验 / 秒",
                                      "归档 / 秒", "总耗时 / 秒", "大小 / MiB", "处理方式", "当前阶段 / 进度"])
        self.results = self.make_table(["结果", "视角", "录像开始时间", "归档文件", "大小 / MiB", "耗时 / 秒", "说明"])
        self.addTab(self.live, "运行记录")
        self.addTab(self.timing, "耗时明细")
        self.addTab(self.results, "归档结果")
        self.results.cellDoubleClicked.connect(self.open_result)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.flush)
        self.timer.start()

    @staticmethod
    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.horizontalHeader().setMinimumSectionSize(80)
        for i in range(len(headers)):
            table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        return table

    def begin(self):
        self.records.clear()
        self.outputs.clear()
        self.pending.clear()
        self.positions.clear()
        self.output_positions.clear()
        for table in (self.live, self.timing, self.results):
            table.setRowCount(0)
        self.started, self.active, self.elapsed = time.monotonic(), True, 0
        self.setCurrentWidget(self.live)

    def accept(self, row):
        kind = row.get("event_kind")
        if kind == "task_record":
            self.pending[("task", row["source_id"])] = dict(row)
        elif kind == "archive_record" and row.get("status") in {"done", "blocked"}:
            key = row.get("target") or row.get("source", "")
            self.pending[("output", key)] = dict(row)

    @staticmethod
    def put(table, position, values):
        if position >= table.rowCount():
            table.setRowCount(position + 1)
        for column, value in enumerate(values):
            item = table.item(position, column)
            if item is None:
                item = QTableWidgetItem()
                table.setItem(position, column, item)
            text = str(value if value is not None else "")
            if item.text() != text:
                item.setText(text)
                item.setToolTip(text)

    def flush(self):
        # Bound each paint batch so a large scan cannot monopolize the GUI thread.
        pending = {key: self.pending.pop(key) for key in list(islice(self.pending, 150))}
        dirty_outputs = set()
        for table in (self.live, self.timing, self.results):
            table.setUpdatesEnabled(False)
        try:
            for (kind, key), row in pending.items():
                if kind == "task":
                    self.records[key] = row
                    self.positions.setdefault(key, len(self.positions))
                    self.draw_record(key)
                    if row["status"] == "blocked":
                        failure = dict(row, status="blocked", target="", record_start_ms=row.get("record_start_ms"))
                        self.outputs.setdefault("failed:" + key, failure)
                        dirty_outputs.add("failed:" + key)
                else:
                    self.outputs[key] = row
                    dirty_outputs.add(key)
            if dirty_outputs:
                for key in dirty_outputs:
                    row = self.outputs[key]
                    self.output_positions.setdefault(key, len(self.output_positions))
                    position = self.output_positions[key]
                    moment = row.get("record_start_ms")
                    if moment:
                        from datetime import datetime

                        from .dahua_source import TZ
                        moment = datetime.fromtimestamp(moment/1000, TZ).strftime("%Y-%m-%d %H:%M:%S")
                    status = "已复用" if row.get("existing_verified") else STATES.get(row["status"], row["status"])
                    self.put(self.results, position, [status, row.get("owner", ""), moment or "",
                        row.get("target", ""), round(row.get("size", 0)/1048576, 2),
                        row.get("file_seconds", 0), row.get("message", "")])
        finally:
            for table in (self.live, self.timing, self.results):
                table.setUpdatesEnabled(True)
        self.timer.setInterval(16 if self.pending else 200)
        elapsed = time.monotonic() - self.started if self.started and self.active else self.elapsed
        done = sum(r["status"] in {"done", "existing"} for r in self.records.values())
        errors = sum(r["status"] == "blocked" for r in self.records.values())
        amount = sum(r.get("size", 0) for r in self.records.values() if r["status"] in {"done", "existing"})
        speed = f"{amount/1048576/elapsed:.2f} MiB/秒" if elapsed > 0 and amount else "统计中"
        running = sorted({r.get("owner", "") for r in self.records.values() if r["status"] == "processing"})
        activity = " / ".join(running) or "无"
        self.summary.setText(f"处理中 {len(running)} 路（{activity}） · 总计 {len(self.records)} 段 · 已归档/复用 {done} · 待核对 {errors}"
                             f" · 总耗时 {int(elapsed)//60:02}:{int(elapsed)%60:02} · 平均处理速度 {speed}")

    def draw_record(self, key):
        row = self.records[key]
        pos = self.positions[key]
        seconds = round(row.get("file_seconds", 0), 2)
        started = row.get("started_at", "")
        if started:
            from datetime import datetime

            from .dahua_source import TZ
            started = datetime.fromisoformat(started).astimezone(TZ).strftime("%m-%d %H:%M:%S")
        self.put(self.live, pos, [STATES.get(row["status"], row["status"]), started,
            row.get("owner", ""), row.get("source", ""),
            " | ".join(row.get("targets", [])), seconds,
            (PHASES.get(row.get("phase"), "") + " · " + row.get("message", "")).strip(" ·")])
        method = METHODS.get(row.get("method"), row.get("method", ""))
        amount = round(row.get("size", 0)/1048576, 2)
        if not row.get("size") and row.get("output_bytes") and row["status"] == "processing":
            amount = "暂存 " + str(round(row["output_bytes"]/1048576, 2))
        self.put(self.timing, pos, [row.get("owner", ""), row.get("source", ""),
            *[round(row.get(name+"_seconds", 0), 2) for name in ("read", "convert", "verify", "archive")],
            seconds, amount, method, row.get("message", "")])

    def finish(self):
        if self.started:
            self.elapsed = time.monotonic() - self.started
        self.active = False
        self.flush()

    def restore(self, report):
        self.begin()
        for row in report.get("records", []):
            self.accept(dict(row, event_kind="task_record"))
        for row in report.get("outputs", []):
            self.accept(dict(row, event_kind="archive_record"))
        self.active = False
        self.elapsed = report.get("elapsed_seconds", 0)
        self.flush()

    def open_result(self, row, _column):
        path = self.results.item(row, 3)
        if path and path.text() and Path(path.text()).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path.text()))
