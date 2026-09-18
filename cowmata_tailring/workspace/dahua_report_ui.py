"""Live, per-view report over the existing batched task models."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRegularExpression, QSortFilterProxyModel, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QTabBar,
    QTableView,
    QTabWidget,
    QVBoxLayout,
)

from .dahua_run import PHASES
from .dahua_source import TZ


class DahuaReportDialog(QDialog):
    def __init__(self, tables, parent=None):
        super().__init__(parent)
        self.tables = tables
        self.setWindowTitle("视频任务记录 · 各视角运行状态")
        self.resize(1180, 720)
        self.setModal(False)
        outer = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.view_tabs = QTabBar()
        self.view_tabs.setExpanding(False)
        self.view_tabs.setUsesScrollButtons(True)
        self.view_tabs.addTab("总览")
        outer.addWidget(self.view_tabs)
        self.pages = QStackedWidget()
        self.overview = tables.make_table(
            [
                "视角",
                "总段数",
                "已归档/复用",
                "处理中",
                "排队",
                "待核对",
                "已处理",
                "累计耗时 / 秒",
                "当前阶段",
                "当前录像开始时间",
            ]
        )
        self.pages.addWidget(self.overview)
        self.details = QTabWidget()
        self.proxies = []
        for name, original, column in (
            ("运行记录", tables.live, 2),
            ("耗时明细", tables.timing, 0),
            ("归档结果", tables.results, 1),
        ):
            table = QTableView()
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setWordWrap(False)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(30)
            table.horizontalHeader().setMinimumSectionSize(80)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            proxy = QSortFilterProxyModel(table)
            proxy.setFilterKeyColumn(column)
            table.setModel(proxy)
            self.proxies.append((proxy, original))
            self.details.addTab(table, name)
        self.live = self.details.widget(0)
        self.timing = self.details.widget(1)
        self.results = self.details.widget(2)
        self.results.doubleClicked.connect(self.open_result)
        self.pages.addWidget(self.details)
        outer.addWidget(self.pages, 1)
        hint = QLabel(
            "总览显示每个视角的实际运行状态；点击视角 sheet 查看记录。排队中的片段尚未开始处理。"
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self.view_tabs.currentChanged.connect(self.select_view)
        self.overview.cellDoubleClicked.connect(
            lambda row, _column: self.view_tabs.setCurrentIndex(row + 1)
        )
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def refresh(self):
        groups = {}
        for row in self.tables.records.values():
            owner = row.get("owner") or "未分配视角"
            groups.setdefault(owner, []).append(row)
        owners = sorted(groups)
        current = self.view_tabs.tabText(self.view_tabs.currentIndex())
        previous = [self.view_tabs.tabText(i) for i in range(1, self.view_tabs.count())]
        if owners != previous:
            self.view_tabs.blockSignals(True)
            while self.view_tabs.count() > 1:
                self.view_tabs.removeTab(1)
            for owner in owners:
                self.view_tabs.addTab(owner)
            self.view_tabs.setCurrentIndex(owners.index(current) + 1 if current in owners else 0)
            self.view_tabs.blockSignals(False)
            self.select_view(self.view_tabs.currentIndex())
        active_views = 0
        self.overview.setUpdatesEnabled(False)
        try:
            self.overview.setRowCount(len(owners))
            for position, owner in enumerate(owners):
                rows = groups[owner]
                active = [r for r in rows if r["status"] == "processing"]
                done = sum(r["status"] in {"done", "existing"} for r in rows)
                blocked = sum(r["status"] == "blocked" for r in rows)
                waiting = sum(r["status"] in {"waiting", "paused"} for r in rows)
                processed = done + blocked + sum(r["status"] == "skipped" for r in rows)
                active_views += bool(active)
                phases = "、".join(
                    dict.fromkeys(PHASES.get(r.get("phase"), "处理中") for r in active)
                )
                state = phases or (
                    "已暂停"
                    if any(r["status"] == "paused" for r in rows)
                    else "排队"
                    if waiting
                    else "待核对"
                    if blocked
                    else "已完成"
                )
                starts = "、".join(
                    datetime.fromtimestamp(r["record_start_ms"] / 1000, TZ).strftime(
                        "%m-%d %H:%M:%S"
                    )
                    for r in active
                    if r.get("record_start_ms")
                )
                self.tables.put(
                    self.overview,
                    position,
                    [
                        owner,
                        len(rows),
                        done,
                        len(active),
                        waiting,
                        blocked,
                        f"{processed}/{len(rows)}",
                        round(sum(r.get("file_seconds", 0) for r in rows), 2),
                        state,
                        starts,
                    ],
                )
        finally:
            self.overview.setUpdatesEnabled(True)
        self.status.setText(
            f"正在处理 {active_views} 个视角 · 共 {len(owners)} 个视角\n"
            + self.tables.summary.text()
        )

    def select_view(self, index):
        owner = self.view_tabs.tabText(index)
        self.pages.setCurrentIndex(0 if index == 0 else 1)
        for proxy, original in self.proxies:
            proxy.setFilterRegularExpression(
                QRegularExpression("^" + QRegularExpression.escape(owner) + "$")
            )
            proxy.setSourceModel(original.model() if index else None)

    def open_result(self, index):
        source = self.results.model().mapToSource(index)
        self.tables.open_result(source.row(), source.column())

    def showEvent(self, event):
        self.refresh()
        self.select_view(self.view_tabs.currentIndex())
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        for proxy, _original in self.proxies:
            proxy.setSourceModel(None)
        super().hideEvent(event)
