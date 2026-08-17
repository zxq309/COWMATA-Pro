"""Non-modal batch review UI for previously exported event CSV files."""

from __future__ import annotations

import copy
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from PySide6.QtCore import QStandardPaths, QTimer, Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from annotation_review_core import (
    REVIEW_SOURCE,
    ReviewImportError,
    export_review_results,
    import_csv_files,
    infer_json_root,
    iter_review_events,
    load_workspace,
    new_workspace,
    resolve_session_json,
    resolve_workspace_jsons,
    review_counts,
    review_display_status,
    save_workspace,
    source_file_fingerprint,
)
from ui_helpers import format_relative


REVIEW_UID_ROLE = int(Qt.ItemDataRole.UserRole) + 41
STATUS_FILTERS = (
    ("全部状态", "all"),
    ("待复核", "pending"),
    ("已修改", "modified"),
    ("已通过", "reviewed"),
    ("缺少/待选/指纹异常 JSON", "missing"),
)


def _event_label(
    session: Mapping[str, Any], event: Mapping[str, Any]
) -> Mapping[str, Any]:
    labels = session.get("labels", [])
    index = int(event.get("li", -1) or -1)
    if isinstance(labels, list) and 0 <= index < len(labels):
        value = labels[index]
        if isinstance(value, dict):
            return value
    return {}


class ReviewEventEditDialog(QDialog):
    """Edit an imported event while retaining its CSV provenance."""

    def __init__(
        self,
        event: Mapping[str, Any],
        labels: list[dict[str, Any]],
        duration_ms: float,
        current_position: Callable[[], float],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.event = event
        self.labels = labels
        self.current_position = current_position
        self.setWindowTitle("修改导入标注")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        original = event.get("review_original", {})
        if not isinstance(original, dict):
            original = {}
        source = QLabel(
            "CSV 原始值："
            f"{original.get('label', original.get('code', ''))} · "
            f"{format_relative(float(original.get('start_ms', 0.0)))} — "
            + (
                "点标注"
                if original.get("end_ms") is None
                else format_relative(float(original.get("end_ms", 0.0)))
            )
        )
        source.setWordWrap(True)
        source.setStyleSheet(
            "background:#eef5ff;border:1px solid #b8d0ef;"
            "border-radius:4px;padding:6px;color:#24466d;"
        )
        layout.addWidget(source)

        form = QFormLayout()
        self.label_combo = QComboBox()
        current_index = int(event.get("li", -1) or -1)
        selected_combo = 0
        for label_index, label in enumerate(labels):
            if (
                str(label.get("layer", "")) == "sync_anchor"
                and label_index != current_index
            ):
                continue
            code = str(label.get("code", label.get("name", "")))
            legacy = " · 旧标签" if label.get("legacy") else ""
            self.label_combo.addItem(
                f"{label.get('name', code)}（{code}）{legacy}",
                label_index,
            )
            if label_index == current_index:
                selected_combo = self.label_combo.count() - 1
        self.label_combo.setCurrentIndex(selected_combo)

        maximum_seconds = max(
            0.001,
            float(duration_ms) / 1000.0,
            float(event.get("t0", 0.0)) / 1000.0,
            float(event.get("t1", event.get("t0", 0.0)) or 0.0) / 1000.0,
        )
        self.start_spin = self._time_spin(maximum_seconds)
        self.end_spin = self._time_spin(maximum_seconds)
        self.start_spin.setValue(float(event.get("t0", 0.0)) / 1000.0)
        end_value = event.get("t1")
        self.end_spin.setValue(
            float(end_value if end_value is not None else event.get("t0", 0.0))
            / 1000.0
        )
        self.evidence_combo = QComboBox()
        self.evidence_combo.addItem("两者", "both")
        self.evidence_combo.addItem("仅视频", "video")
        self.evidence_combo.addItem("仅曲线", "curve")
        evidence_index = self.evidence_combo.findData(
            str(event.get("ev", "both"))
        )
        if evidence_index >= 0:
            self.evidence_combo.setCurrentIndex(evidence_index)
        self.note_edit = QPlainTextEdit(str(event.get("note", "")))
        self.note_edit.setMaximumHeight(90)

        form.addRow("标签", self.label_combo)
        form.addRow("开始（相对九轴）", self.start_spin)
        form.addRow("结束（相对九轴）", self.end_spin)
        form.addRow("判定证据", self.evidence_combo)
        form.addRow("备注", self.note_edit)
        layout.addLayout(form)

        position_row = QHBoxLayout()
        start_here = QPushButton("开始 = 当前播放头")
        end_here = QPushButton("结束 = 当前播放头")
        start_here.clicked.connect(
            lambda: self.start_spin.setValue(self.current_position() / 1000.0)
        )
        end_here.clicked.connect(
            lambda: self.end_spin.setValue(self.current_position() / 1000.0)
        )
        position_row.addWidget(start_here)
        position_row.addWidget(end_here)
        layout.addLayout(position_row)

        hint = QLabel(
            "区间也可直接回到主界面拖动左右边界。修改后状态变为“已修改”，"
            "需要再次点击“确认通过”。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#58677b;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.label_combo.currentIndexChanged.connect(self._sync_label_type)
        self._sync_label_type()

    @staticmethod
    def _time_spin(maximum_seconds: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(3)
        spin.setRange(0.0, maximum_seconds)
        spin.setSingleStep(0.100)
        spin.setSuffix(" s")
        return spin

    def _selected_label(self) -> dict[str, Any]:
        index = int(self.label_combo.currentData())
        return self.labels[index]

    def _sync_label_type(self, _index: int = -1) -> None:
        is_point = str(self._selected_label().get("type", "interval")) == "point"
        self.end_spin.setEnabled(not is_point)

    def values(self) -> tuple[int, float, float | None, str, str]:
        label_index = int(self.label_combo.currentData())
        label = self.labels[label_index]
        end_ms = (
            None
            if str(label.get("type", "interval")) == "point"
            else self.end_spin.value() * 1000.0
        )
        return (
            label_index,
            self.start_spin.value() * 1000.0,
            end_ms,
            self.note_edit.toPlainText().strip(),
            str(self.evidence_combo.currentData() or "both"),
        )

    def accept(self) -> None:
        _label_index, start_ms, end_ms, _note, _evidence = self.values()
        if end_ms is not None and end_ms <= start_ms:
            QMessageBox.warning(self, "时间范围无效", "结束时间必须晚于开始时间。")
            return
        super().accept()


class AnnotationReviewDialog(QDialog):
    """Persistent non-modal queue spanning many source JSON sessions."""

    def __init__(
        self,
        workspace_callback: Callable[[], dict[str, Any]],
        locate_callback: Callable[[str], None],
        edit_callback: Callable[[str], None],
        approve_callback: Callable[[str], None],
        next_callback: Callable[[str], None],
        root_callback: Callable[[], None],
        export_callback: Callable[[], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.workspace_callback = workspace_callback
        self.locate_callback = locate_callback
        self.edit_callback = edit_callback
        self.approve_callback = approve_callback
        self.next_callback = next_callback
        self.root_callback = root_callback
        self.export_callback = export_callback
        self.setWindowTitle("已导出标注批量复核")
        # Normally hosted in a bottom QDockWidget. Keep a modest floating
        # size as well, so undocking never opens a giant window over the
        # waveform.
        self.setMinimumSize(620, 280)
        self.resize(1120, 390)
        self.setModal(False)

        layout = QVBoxLayout(self)
        description = QLabel(
            "单击一条标注会自动载入对应九轴 JSON，并在主界面定位真实波形。"
            "区间可在波形上拖动边界，也可使用“修改标签/时间/备注”。"
            "面板可拖动标题栏调整位置或浮动。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        summary_row = QHBoxLayout()
        self.summary_label = QLabel()
        self.root_label = QLabel()
        self.root_label.setStyleSheet("color:#58677b;")
        root_btn = QPushButton("设置 JSON 根目录")
        root_btn.clicked.connect(self.root_callback)
        summary_row.addWidget(self.summary_label)
        summary_row.addStretch()
        summary_row.addWidget(self.root_label)
        summary_row.addWidget(root_btn)
        layout.addLayout(summary_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("筛选"))
        self.status_filter = QComboBox()
        for text, value in STATUS_FILTERS:
            self.status_filter.addItem(text, value)
        self.status_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.status_filter)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "状态",
                "会话",
                "设备 / 牛号",
                "标签",
                "开始",
                "结束",
                "时长",
                "来源 CSV",
                "对应 JSON",
            ]
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.table.cellClicked.connect(self._locate_row)
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        self.locate_btn = QPushButton("定位选中")
        self.edit_btn = QPushButton("修改标签/时间/备注")
        self.approve_btn = QPushButton("确认通过并转到下一条")
        self.next_btn = QPushButton("下一条待复核")
        self.export_btn = QPushButton("导出复核结果")
        self.close_btn = QPushButton("关闭复核面板")
        self.locate_btn.clicked.connect(self._locate_selected)
        self.edit_btn.clicked.connect(self._edit_selected)
        self.approve_btn.clicked.connect(self._approve_selected)
        self.next_btn.clicked.connect(self._next_selected)
        self.export_btn.clicked.connect(self.export_callback)
        self.close_btn.clicked.connect(self._close_review_view)
        for button in (
            self.locate_btn,
            self.edit_btn,
            self.approve_btn,
            self.next_btn,
        ):
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.export_btn)
        controls.addWidget(self.close_btn)
        layout.addLayout(controls)
        self.refresh()

    def _close_review_view(self) -> None:
        """Hide the host dock, or this widget when used standalone."""

        host = self.parentWidget()
        while host is not None:
            if isinstance(host, QDockWidget):
                host.hide()
                return
            host = host.parentWidget()
        self.hide()

    def _refresh_controls(self) -> None:
        """Keep review actions honest when the filtered table is empty."""

        has_selection = bool(self.selected_uid())
        for button in (
            self.locate_btn,
            self.edit_btn,
            self.approve_btn,
        ):
            button.setEnabled(has_selection)
        # This action can also start at the first pending row, so it remains
        # useful when the current filter hides every selected row.
        workspace = self.workspace_callback()
        self.next_btn.setEnabled(bool(workspace.get("sessions")))
        self.export_btn.setEnabled(bool(workspace.get("sessions")))

    def _matches_filter(self, display_status: str) -> bool:
        value = str(self.status_filter.currentData() or "all")
        if value == "all":
            return True
        if value == "pending":
            return display_status == "待复核"
        if value == "modified":
            return display_status in {"已修改", "已删除"}
        if value == "reviewed":
            return display_status.startswith("已通过")
        if value == "missing":
            return display_status in {
                "缺少 JSON",
                "JSON 待选择",
                "JSON 指纹不一致",
            }
        return True

    def refresh(self, _index: int = -1, selected_uid: str = "") -> None:
        previous_uid = selected_uid or self.selected_uid()
        workspace = self.workspace_callback()
        counts = review_counts(workspace)
        self.summary_label.setText(
            f"共 {counts['total']} 条 · 待复核 {counts['pending']} · "
            f"已修改 {counts['modified']} · 已通过 {counts['reviewed']} · "
            f"缺少/待选/指纹异常 JSON {counts['missing_json']}"
        )
        root = str(workspace.get("json_root", "") or "")
        self.root_label.setText("JSON 根目录：" + (root or "未设置"))

        rows: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for session, event in iter_review_events(workspace):
            display = review_display_status(session, event)
            if self._matches_filter(display):
                rows.append((session, event, display))
        rows.sort(
            key=lambda item: (
                str(item[0].get("session_id", "")),
                float(item[1].get("t0", 0.0)),
                str(item[1].get("review_uid", "")),
            )
        )

        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        select_row = -1
        colors = {
            "待复核": QColor("#9a5b00"),
            "已修改": QColor("#9a5b00"),
            "已删除": QColor("#9b2c2c"),
            "已通过": QColor("#1f7a48"),
            "已通过（修改）": QColor("#1f7a48"),
            "缺少 JSON": QColor("#b42318"),
            "JSON 待选择": QColor("#b42318"),
            "JSON 指纹不一致": QColor("#b42318"),
        }
        for row_index, (session, event, display) in enumerate(rows):
            label = _event_label(session, event)
            start = float(event.get("t0", 0.0))
            end = event.get("t1")
            duration = (
                "点"
                if end is None
                else format_relative(float(end) - start)
            )
            json_path = str(session.get("json_path", "") or "")
            values = [
                display,
                str(session.get("session_id", "")),
                " / ".join(
                    value
                    for value in (
                        str(session.get("device", "")),
                        str(session.get("cow_id", "")),
                    )
                    if value
                ),
                str(label.get("name", event.get("label_code", ""))),
                format_relative(start),
                "点" if end is None else format_relative(float(end)),
                duration,
                Path(str(session.get("csv_path", ""))).name,
                Path(json_path).name if json_path else "—",
            ]
            uid = str(event.get("review_uid", ""))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(REVIEW_UID_ROLE, uid)
                if column == 0:
                    item.setForeground(colors.get(display, QColor("#172033")))
                self.table.setItem(row_index, column, item)
            if uid == previous_uid:
                select_row = row_index
        self.table.setSortingEnabled(True)
        if select_row >= 0:
            self.table.selectRow(select_row)
        self.table.blockSignals(False)
        self._refresh_controls()

    def selected_uid(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.table.item(rows[0].row(), 0)
        return str(item.data(REVIEW_UID_ROLE) or "") if item else ""

    def select_uid(self, uid: str) -> None:
        self.refresh(selected_uid=uid)

    def _uid_for_row(self, row: int) -> str:
        item = self.table.item(row, 0)
        return str(item.data(REVIEW_UID_ROLE) or "") if item else ""

    def _locate_row(self, row: int, _column: int) -> None:
        uid = self._uid_for_row(row)
        if uid:
            self.locate_callback(uid)

    def _locate_selected(self) -> None:
        uid = self.selected_uid()
        if uid:
            self.locate_callback(uid)

    def _edit_selected(self) -> None:
        uid = self.selected_uid()
        if uid:
            self.edit_callback(uid)

    def _approve_selected(self) -> None:
        uid = self.selected_uid()
        if uid:
            self.approve_callback(uid)

    def _next_selected(self) -> None:
        self.next_callback(self.selected_uid())


class AnnotationReviewMixin:
    """Integrate batch CSV review without changing model inference code."""

    def __init__(self) -> None:
        self._review_workspace: dict[str, Any] = new_workspace()
        self._review_dialog: AnnotationReviewDialog | None = None
        self._review_dock: QDockWidget | None = None
        self._review_active_session_key = ""
        self._review_loading_session = False
        self._review_entry_confirmed = False
        self._review_autosave_path: Path | None = None
        super().__init__()
        app_data = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        if not app_data:
            app_data = str(Path.home() / ".bovine-motion-workbench")
        self._review_autosave_path = Path(app_data) / "annotation_review_autosave.json"
        self._install_annotation_review_tools()

    def _install_annotation_review_tools(self) -> None:
        toolbar = self.main_toolbar
        self.review_import_action = QAction("导入事件 CSV…", self)
        self.review_import_folder_action = QAction("导入 CSV 文件夹…", self)
        self.review_json_root_action = QAction("设置原始 JSON 根目录…", self)
        self.review_open_action = QAction("打开复核队列", self)
        self.review_restore_action = QAction("恢复上次自动保存的复核", self)
        self.review_save_action = QAction("另存复核工作区…", self)
        self.review_export_action = QAction("导出复核结果…", self)
        self.review_clear_action = QAction("清空复核队列", self)

        self.review_import_action.triggered.connect(self.import_review_csvs)
        self.review_import_folder_action.triggered.connect(
            self.import_review_csv_folder
        )
        self.review_json_root_action.triggered.connect(self.choose_review_json_root)
        self.review_open_action.triggered.connect(self.show_annotation_review)
        self.review_restore_action.triggered.connect(self.restore_review_workspace)
        self.review_save_action.triggered.connect(self.save_review_workspace_as)
        self.review_export_action.triggered.connect(self.export_annotation_review)
        self.review_clear_action.triggered.connect(self.clear_annotation_review)

        self.annotation_review_button = QToolButton()
        self.annotation_review_button.setText("标注复核")
        self.annotation_review_button.setToolTip(
            "导入已导出的事件 CSV，定位原始 JSON 波形并逐条复核"
        )
        self.annotation_review_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.annotation_review_menu = self.export_tools_menu.__class__(
            self.annotation_review_button
        )
        for action in (
            self.review_import_action,
            self.review_import_folder_action,
            self.review_json_root_action,
        ):
            self.annotation_review_menu.addAction(action)
        self.annotation_review_menu.addSeparator()
        self.annotation_review_menu.addAction(self.review_open_action)
        self.annotation_review_menu.addAction(self.review_restore_action)
        self.annotation_review_menu.addAction(self.review_save_action)
        self.annotation_review_menu.addAction(self.review_export_action)
        self.annotation_review_menu.addSeparator()
        self.annotation_review_menu.addAction(self.review_clear_action)
        self.annotation_review_button.setMenu(self.annotation_review_menu)
        before = getattr(self, "toolbar_spacer_action", None)
        if before is not None:
            self.annotation_review_widget_action = toolbar.insertWidget(
                before, self.annotation_review_button
            )
        else:
            self.annotation_review_widget_action = toolbar.addWidget(
                self.annotation_review_button
            )
        self._refresh_review_actions()

    def _refresh_review_actions(self) -> None:
        has_sessions = bool(self._review_workspace.get("sessions"))
        for action in (
            getattr(self, "review_open_action", None),
            getattr(self, "review_save_action", None),
            getattr(self, "review_export_action", None),
            getattr(self, "review_clear_action", None),
        ):
            if action is not None:
                action.setEnabled(has_sessions)
        restore = getattr(self, "review_restore_action", None)
        if restore is not None:
            restore.setEnabled(
                bool(
                    self._review_autosave_path is not None
                    and self._review_autosave_path.is_file()
                )
            )

    def _workspace(self) -> dict[str, Any]:
        return self._review_workspace

    def _show_review_errors(self, errors: list[str]) -> None:
        if not errors:
            return
        shown = errors[:12]
        suffix = "" if len(errors) <= len(shown) else f"\n另有 {len(errors)-len(shown)} 项"
        QMessageBox.warning(
            self,
            "部分 CSV 未导入",
            "\n".join(f"• {value}" for value in shown) + suffix,
        )

    def import_review_csvs(
        self, paths: list[str] | tuple[str, ...] | None = None
    ) -> None:
        if paths is None or isinstance(paths, bool):
            selected, _ = QFileDialog.getOpenFileNames(
                self,
                "导入已导出的事件 CSV",
                str(self.settings.value("review_csv_dir", Path.cwd())),
                "事件 CSV (*.events.csv)",
            )
            paths = selected
        values = [str(value) for value in (paths or []) if str(value)]
        if not values:
            return
        imported, skipped, errors = import_csv_files(
            self._review_workspace, values
        )
        self.settings.setValue("review_csv_dir", str(Path(values[0]).parent))
        root = infer_json_root(values)
        if root is None:
            stored = str(self.settings.value("review_json_root", "") or "")
            root = Path(stored) if stored and Path(stored).is_dir() else None
        counts = None
        if root is not None:
            counts = resolve_workspace_jsons(self._review_workspace, root)
            self.settings.setValue("review_json_root", str(root))
        else:
            # A newer export may carry an absolute source_json_path in its
            # companion metadata.  Resolve that path even before a root
            # directory is selected; sessions without it remain explicitly
            # marked as missing instead of looking indefinitely unresolved.
            counts = {
                "resolved": 0,
                "missing": 0,
                "ambiguous": 0,
                "fingerprint_mismatch": 0,
            }
            for session in self._review_workspace.get("sessions", []):
                if not isinstance(session, dict):
                    continue
                status = resolve_session_json(session, {})
                counts[status] = counts.get(status, 0) + 1
        self._save_review_workspace()
        self._show_review_errors(errors)
        self.show_annotation_review()
        detail = f"已导入 {imported} 份事件 CSV"
        if skipped:
            detail += f"，跳过重复 {skipped} 份"
        if counts is not None:
            detail += (
                f"；JSON 已匹配 {counts.get('resolved', 0)}，"
                f"缺少 {counts.get('missing', 0)}，"
                f"待选择 {counts.get('ambiguous', 0)}，"
                f"指纹异常 {counts.get('fingerprint_mismatch', 0)}"
            )
        self.statusBar().showMessage(detail, 7000)

    def import_review_csv_folder(self, _checked: bool = False) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择包含事件 CSV 的文件夹",
            str(self.settings.value("review_csv_dir", Path.cwd())),
        )
        if not folder:
            return
        paths = sorted(
            Path(folder).rglob("*.events.csv"),
            key=lambda path: str(path).casefold(),
        )
        if not paths:
            QMessageBox.information(self, "导入事件 CSV", "文件夹中没有 *.events.csv")
            return
        self.import_review_csvs([str(path) for path in paths])

    def choose_review_json_root(
        self, path: str | os.PathLike[str] | None = None
    ) -> None:
        if path is None or isinstance(path, bool):
            selected = QFileDialog.getExistingDirectory(
                self,
                "选择原始九轴 JSON 根目录",
                str(
                    self._review_workspace.get("json_root")
                    or self.settings.value("review_json_root", Path.cwd())
                ),
            )
            path = selected
        if not path:
            return
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            self._show_error(f"JSON 根目录不存在：\n{root}")
            return
        counts = resolve_workspace_jsons(self._review_workspace, root)
        self.settings.setValue("review_json_root", str(root))
        self._save_review_workspace()
        self._refresh_review_dialog()
        self.statusBar().showMessage(
            f"JSON 匹配完成：已匹配 {counts.get('resolved', 0)}，"
            f"缺少 {counts.get('missing', 0)}，"
            f"待选择 {counts.get('ambiguous', 0)}，"
            f"指纹异常 {counts.get('fingerprint_mismatch', 0)}",
            7000,
        )

    def show_annotation_review(self, _checked: bool = False) -> None:
        if not self._review_workspace.get("sessions"):
            self.statusBar().showMessage("请先导入 *.events.csv", 3000)
            return
        if self._review_dialog is None:
            self._review_dialog = AnnotationReviewDialog(
                self._workspace,
                self.locate_review_event,
                self.edit_review_event,
                self.approve_review_event,
                self.locate_next_pending_review,
                self.choose_review_json_root,
                self.export_annotation_review,
                self,
            )
            # Keep the queue inside the main window so the waveform remains
            # visible while a row is being reviewed. The dock can still be
            # moved to another edge or floated from its title bar.
            self._review_dialog.setWindowFlags(Qt.WindowType.Widget)
            self._review_dock = QDockWidget("已导出标注批量复核", self)
            self._review_dock.setObjectName("annotationReviewDock")
            self._review_dock.setAllowedAreas(
                Qt.DockWidgetArea.BottomDockWidgetArea
                | Qt.DockWidgetArea.LeftDockWidgetArea
                | Qt.DockWidgetArea.RightDockWidgetArea
            )
            self._review_dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetClosable
                | QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            )
            self._review_dock.setMinimumSize(620, 280)
            self._review_dock.setWidget(self._review_dialog)
            self.addDockWidget(
                Qt.DockWidgetArea.BottomDockWidgetArea,
                self._review_dock,
            )
            initial_height = max(300, min(430, int(self.height() * 0.42)))
            QTimer.singleShot(
                0,
                lambda height=initial_height: self._resize_review_dock(height),
            )
        self._review_dialog.refresh()
        if self._review_dock is not None:
            self._review_dock.show()
            self._review_dock.raise_()
            self._review_dock.activateWindow()
        else:
            self._review_dialog.show()
            self._review_dialog.raise_()
            self._review_dialog.activateWindow()

    def _resize_review_dock(self, height: int) -> None:
        dock = self._review_dock
        if dock is None or not dock.isVisible() or dock.isFloating():
            return
        dock.resize(max(700, self.width() - 12), int(height))

    def _review_lookup(
        self, uid: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for session, event in iter_review_events(self._review_workspace):
            if str(event.get("review_uid", "")) == str(uid):
                return session, event
        return None

    def _review_session(self, key: str) -> dict[str, Any] | None:
        return next(
            (
                session
                for session in self._review_workspace.get("sessions", [])
                if isinstance(session, dict) and str(session.get("key", "")) == key
            ),
            None,
        )

    def _resolve_review_session_interactively(
        self, session: dict[str, Any]
    ) -> bool:
        path = str(session.get("json_path", "") or "")
        if path and Path(path).is_file():
            return True
        candidates = [
            str(value)
            for value in session.get("json_candidates", [])
            if Path(str(value)).is_file()
        ]
        start = (
            str(Path(candidates[0]).parent)
            if candidates
            else str(self._review_workspace.get("json_root") or Path.cwd())
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            f"为 {session.get('session_id', '')} 选择对应原始 JSON",
            start,
            "JSON 文件 (*.json)",
        )
        if not selected:
            return False
        candidate = Path(selected).resolve()
        expected_name = str(session.get("session_id", "")) + ".json"
        if candidate.name.casefold() != expected_name.casefold():
            answer = QMessageBox.warning(
                self,
                "JSON 文件名不一致",
                f"标注会话：{expected_name}\n当前选择：{candidate.name}\n\n仍然关联？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        meta = session.get("source_meta", {})
        expected_fingerprint = (
            str(meta.get("source_json_fingerprint", "") or "").strip()
            if isinstance(meta, dict)
            else ""
        )
        if expected_fingerprint:
            try:
                actual_fingerprint = source_file_fingerprint(candidate)
            except OSError:
                actual_fingerprint = ""
            if actual_fingerprint != expected_fingerprint:
                answer = QMessageBox.warning(
                    self,
                    "JSON 指纹不一致",
                    "当前文件与导出元数据中的原始 JSON 指纹不一致。\n\n"
                    "仍然关联该文件？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return False
        session["json_path"] = str(candidate)
        session["json_candidates"] = [str(candidate)]
        session["json_match_status"] = "resolved"
        self._save_review_workspace()
        return True

    @staticmethod
    def _review_event_signature(event: Mapping[str, Any]) -> tuple[Any, ...]:
        """Return only user-editable fields for change detection."""

        def number(value: Any) -> float | None:
            if value is None or value == "":
                return None
            try:
                return round(float(value), 6)
            except (TypeError, ValueError):
                return None

        reviewed_range = event.get("reviewed_range", {})
        if not isinstance(reviewed_range, dict):
            reviewed_range = {}
        return (
            int(event.get("li", -1) or -1),
            str(event.get("label_code", "")),
            number(event.get("t0")),
            number(event.get("t1")),
            str(event.get("note", "")),
            str(event.get("ev", "both")),
            str(event.get("ctx", "")),
            number(reviewed_range.get("start")),
            number(reviewed_range.get("end")),
        )

    def _sync_active_review_session(self) -> None:
        key = self._review_active_session_key
        if not key or self._review_loading_session:
            return
        session = self._review_session(key)
        if session is None or not hasattr(self, "events"):
            return
        existing = {
            str(event.get("review_uid", "")): copy.deepcopy(event)
            for event in session.get("events", [])
            if isinstance(event, dict) and event.get("review_uid")
        }
        current: list[dict[str, Any]] = []
        current_uids: set[str] = set()
        for event in self.events:
            value = copy.deepcopy(event)
            uid = str(value.get("review_uid", "") or "")
            if not uid:
                uid = f"{key}:added:{self.next_event_id}:{len(current)}"
                value.update(
                    {
                        "review_source": REVIEW_SOURCE,
                        "review_uid": uid,
                        "review_session_key": key,
                        "review_status": "modified",
                        "review_modified": True,
                        "review_added": True,
                        "review_deleted": False,
                        "review_original": {
                            "code": "",
                            "label": "",
                            "start_ms": None,
                            "end_ms": None,
                            "note": "",
                        },
                        "review_original_row": {},
                    }
                )
                event.update(value)
            else:
                previous = existing.get(uid)
                if (
                    previous is not None
                    and self._review_event_signature(previous)
                    != self._review_event_signature(value)
                ):
                    # Any edit performed through another main-window control
                    # (for example a shortcut or a future editor) must reset a
                    # prior approval and appear in the review queue.
                    value["review_modified"] = True
                    value["review_status"] = "modified"
                    value.pop("reviewed_at", None)
                    value.pop("reviewed_by", None)
                    event.update(
                        {
                            "review_modified": True,
                            "review_status": "modified",
                        }
                    )
                    event.pop("reviewed_at", None)
                    event.pop("reviewed_by", None)
            current_uids.add(uid)
            current.append(value)
        deleted: list[dict[str, Any]] = []
        for uid, old in existing.items():
            if uid in current_uids:
                continue
            old["review_deleted"] = True
            old["review_modified"] = True
            old["review_status"] = "modified"
            deleted.append(old)
        session["events"] = sorted(
            [*current, *deleted],
            key=lambda event: (
                float(event.get("t0", 0.0)),
                str(event.get("review_uid", "")),
            ),
        )
        session["labels"] = copy.deepcopy(self.labels)
        session["json_path"] = self.data_path
        session["json_match_status"] = "resolved"
        session["device"] = self.data_device or session.get("device", "")
        session["create_time_ms"] = self.data_create_time_ms
        session["acc_scale"] = int(self.acc_scale_combo.currentText())
        session["cow_id"] = self.cow_id_edit.text().strip()
        session["annotator"] = self.annotator_edit.text().strip()
        session["protocol"] = self.protocol_edit.text().strip()

    def _activate_review_session(self, session: dict[str, Any]) -> bool:
        key = str(session.get("key", ""))
        json_path = str(session.get("json_path", "") or "")
        if not json_path or not Path(json_path).is_file():
            return False
        if key == self._review_active_session_key and os.path.normcase(
            os.path.abspath(self.data_path or "")
        ) == os.path.normcase(os.path.abspath(json_path)):
            return True

        if not self._review_active_session_key and self.events and not (
            self._review_entry_confirmed
        ):
            answer = QMessageBox.warning(
                self,
                "进入标注复核",
                "当前界面已有标注。进入批量复核会切换当前 JSON 和事件表；"
                "请先保存现有工程。\n\n确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
            self._review_entry_confirmed = True

        self._sync_active_review_session()
        previous_labels = copy.deepcopy(self.labels)
        previous_events = copy.deepcopy(self.events)
        previous_selected = self.selected_event_id
        previous_active = self._review_active_session_key
        needs_load = os.path.normcase(os.path.abspath(self.data_path or "")) != (
            os.path.normcase(os.path.abspath(json_path))
        )
        self._review_loading_session = True
        try:
            if needs_load:
                self.events = []
                self.selected_event_id = None
                self.pending_intervals.clear()
                self.open_json(json_path)
                loaded = os.path.normcase(os.path.abspath(self.data_path or "")) == (
                    os.path.normcase(os.path.abspath(json_path))
                )
                if not loaded:
                    self.labels = previous_labels
                    self.events = previous_events
                    self.selected_event_id = previous_selected
                    self._review_active_session_key = previous_active
                    self._refresh_labels()
                    self._refresh_events()
                    return False
            self.labels = copy.deepcopy(session.get("labels", []))
            self.events = [
                copy.deepcopy(event)
                for event in session.get("events", [])
                if isinstance(event, dict)
                and not bool(event.get("review_deleted", False))
            ]
            times = np.asarray(getattr(self.data, "times_ms", []), dtype=float)
            if times.size:
                for event in self.events:
                    start_ms = float(event.get("t0", 0.0))
                    end_ms = event.get("t1")
                    event["json_sample_start"] = int(
                        np.searchsorted(times, start_ms, side="left")
                    )
                    event["json_sample_end"] = (
                        None
                        if end_ms is None
                        else int(
                            np.searchsorted(
                                times, float(end_ms), side="left"
                            )
                        )
                    )
            self.next_event_id = max(
                (int(event.get("id", 0)) for event in self.events), default=0
            ) + 1
            self.selected_event_id = None
            self.pending_intervals.clear()
            self.annotator_edit.setText(str(session.get("annotator", "")))
            self.cow_id_edit.setText(str(session.get("cow_id", "")))
            self.protocol_edit.setText(
                str(session.get("protocol", "v4") or "v4")
            )
            self._review_active_session_key = key
            session["device"] = self.data_device or session.get("device", "")
            session["create_time_ms"] = self.data_create_time_ms
            session["acc_scale"] = int(self.acc_scale_combo.currentText())
            self._refresh_labels()
            self._refresh_events()
            self._rebuild_shortcuts()
            undo_stack = getattr(self, "_undo_stack", None)
            if undo_stack is not None:
                undo_stack.clear()
        finally:
            self._review_loading_session = False
        self._save_review_workspace()
        return True

    def locate_review_event(self, uid: str) -> None:
        found = self._review_lookup(uid)
        if found is None:
            return
        session, event = found
        if event.get("review_deleted", False):
            self.statusBar().showMessage("该标注已在复核中删除", 3000)
            return
        if not self._resolve_review_session_interactively(session):
            self._refresh_review_dialog(uid)
            return
        if not self._activate_review_session(session):
            return
        live = next(
            (
                value
                for value in self.events
                if str(value.get("review_uid", "")) == str(uid)
            ),
            None,
        )
        if live is None:
            return
        self.selected_event_id = int(live.get("id", -1))
        self._refresh_events()
        if live.get("t1") is not None and hasattr(
            self, "_focus_interval_on_waveform"
        ):
            self._focus_interval_on_waveform(live, announce=True)
        self.set_playhead(float(live.get("t0", 0.0)))
        self.plot.setFocus()
        self._review_workspace["last_event_uid"] = uid
        self._save_review_workspace()
        self._refresh_review_dialog(uid)

    def edit_review_event(self, uid: str) -> None:
        self.locate_review_event(uid)
        found = self._review_lookup(uid)
        if found is None:
            return
        session, _stored = found
        live = next(
            (
                event
                for event in self.events
                if str(event.get("review_uid", "")) == str(uid)
            ),
            None,
        )
        if live is None:
            return
        dialog = ReviewEventEditDialog(
            live,
            self.labels,
            self.data_duration_ms,
            lambda: float(self.playhead_ms),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        label_index, start_ms, end_ms, note, evidence = dialog.values()
        label = self.labels[label_index]
        live.update(
            {
                "li": label_index,
                "label_code": str(label.get("code", label.get("name", ""))),
                "layer": str(label.get("layer", "")),
                "t0": float(start_ms),
                "t1": None if end_ms is None else float(end_ms),
                "note": note,
                "ev": evidence,
                "reviewed_range": {
                    "start": float(start_ms),
                    "end": None if end_ms is None else float(end_ms),
                },
                "review_status": "modified",
                "review_modified": True,
                "review_legacy_label": bool(label.get("legacy", False)),
            }
        )
        live.pop("reviewed_at", None)
        live.pop("reviewed_by", None)
        times = np.asarray(getattr(self.data, "times_ms", []), dtype=float)
        if times.size:
            live["json_sample_start"] = int(
                np.searchsorted(times, start_ms, side="left")
            )
            live["json_sample_end"] = (
                None
                if end_ms is None
                else int(np.searchsorted(times, end_ms, side="left"))
            )
        self.events.sort(
            key=lambda event: (
                float(event.get("t0", 0.0)),
                int(event.get("id", 0)),
            )
        )
        session["labels"] = copy.deepcopy(self.labels)
        self.selected_event_id = int(live.get("id", -1))
        self._refresh_events()
        self._autosave()
        self.locate_review_event(uid)
        self.statusBar().showMessage("已修改导入标注，原始 CSV 值已保留", 4000)

    def _mark_review_event_modified(self, event_id: int) -> None:
        event = next(
            (
                value
                for value in self.events
                if int(value.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if event is None or event.get("review_source") != REVIEW_SOURCE:
            return
        event["review_modified"] = True
        event["review_status"] = "modified"
        event.pop("reviewed_at", None)
        event.pop("reviewed_by", None)
        self._sync_active_review_session()
        self._save_review_workspace()
        self._refresh_review_dialog(str(event.get("review_uid", "")))

    def _event_changed_on_plot(
        self, event_id: int, start_ms: float, end_ms: Any
    ) -> None:
        before = next(
            (
                (
                    float(event.get("t0", 0.0)),
                    event.get("t1"),
                )
                for event in self.events
                if int(event.get("id", -1)) == int(event_id)
                and event.get("review_source") == REVIEW_SOURCE
            ),
            None,
        )
        super()._event_changed_on_plot(event_id, start_ms, end_ms)
        current = next(
            (
                event
                for event in self.events
                if int(event.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if current is None or before is None:
            return
        after = (float(current.get("t0", 0.0)), current.get("t1"))
        if after != before:
            self._mark_review_event_modified(event_id)

    def _annotation_meta_changed(self) -> None:
        event_id = self.selected_event_id
        before = None
        if event_id is not None:
            event = next(
                (
                    value
                    for value in self.events
                    if int(value.get("id", -1)) == int(event_id)
                    and value.get("review_source") == REVIEW_SOURCE
                ),
                None,
            )
            if event is not None:
                before = (
                    str(event.get("note", "")),
                    str(event.get("ev", "both")),
                )
        super()._annotation_meta_changed()
        if event_id is None or before is None:
            return
        event = next(
            (
                value
                for value in self.events
                if int(value.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if event is not None and before != (
            str(event.get("note", "")),
            str(event.get("ev", "both")),
        ):
            self._mark_review_event_modified(event_id)

    def approve_review_event(self, uid: str) -> None:
        self.locate_review_event(uid)
        live = next(
            (
                event
                for event in self.events
                if str(event.get("review_uid", "")) == str(uid)
            ),
            None,
        )
        if live is None:
            return
        live["review_status"] = "reviewed"
        live["reviewed_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        live["reviewed_by"] = self.annotator_edit.text().strip()
        self._sync_active_review_session()
        self._save_review_workspace()
        self._refresh_events()
        self._refresh_review_dialog(uid)
        self.locate_next_pending_review(uid)

    def _pending_review_uids(self) -> list[str]:
        values: list[tuple[str, float, str]] = []
        for session, event in iter_review_events(self._review_workspace):
            if event.get("review_deleted", False):
                continue
            if str(event.get("review_status", "pending")) == "reviewed":
                continue
            values.append(
                (
                    str(session.get("session_id", "")),
                    float(event.get("t0", 0.0)),
                    str(event.get("review_uid", "")),
                )
            )
        values.sort()
        return [uid for _session, _time, uid in values]

    def locate_next_pending_review(self, current_uid: str = "") -> None:
        pending = self._pending_review_uids()
        if not pending:
            self.statusBar().showMessage("所有可用标注均已复核", 5000)
            self._refresh_review_dialog()
            return
        target = pending[0]
        if current_uid in pending:
            index = pending.index(current_uid)
            target = pending[(index + 1) % len(pending)]
        self.locate_review_event(target)

    def _refresh_review_dialog(self, selected_uid: str = "") -> None:
        self._refresh_review_actions()
        if self._review_dialog is not None:
            self._review_dialog.refresh(selected_uid=selected_uid)

    def _save_review_workspace(self) -> None:
        if self._review_autosave_path is None or not self._review_workspace.get(
            "sessions"
        ):
            self._refresh_review_actions()
            return
        try:
            save_workspace(self._review_workspace, self._review_autosave_path)
        except (OSError, TypeError, ValueError):
            return
        self._refresh_review_actions()

    def save_review_workspace_as(
        self, path: str | os.PathLike[str] | None = None
    ) -> None:
        self._sync_active_review_session()
        if path is None or isinstance(path, bool):
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "保存标注复核工作区",
                str(Path.cwd() / "annotation_review.review.json"),
                "复核工作区 (*.review.json)",
            )
            path = selected
        if not path:
            return
        try:
            target = save_workspace(self._review_workspace, path)
        except (OSError, TypeError, ValueError) as exc:
            self._show_error(f"复核工作区保存失败：\n{exc}")
            return
        self.statusBar().showMessage(f"复核工作区已保存：{target}", 5000)

    def restore_review_workspace(
        self, path: str | os.PathLike[str] | None = None
    ) -> None:
        if path is None or isinstance(path, bool):
            if self._review_autosave_path and self._review_autosave_path.is_file():
                path = self._review_autosave_path
            else:
                selected, _ = QFileDialog.getOpenFileName(
                    self,
                    "载入标注复核工作区",
                    str(Path.cwd()),
                    "复核工作区 (*.review.json *.json)",
                )
                path = selected
        if not path:
            return
        try:
            self._review_workspace = load_workspace(path)
        except ReviewImportError as exc:
            self._show_error(str(exc))
            return
        self._review_active_session_key = ""
        self._review_entry_confirmed = False
        root = str(self._review_workspace.get("json_root", "") or "")
        if root and Path(root).is_dir():
            resolve_workspace_jsons(self._review_workspace, root)
        else:
            # Re-check source_json_path metadata even when the saved workspace
            # has no usable root directory yet.
            for session in self._review_workspace.get("sessions", []):
                if isinstance(session, dict):
                    resolve_session_json(session, {})
        self._save_review_workspace()
        self.show_annotation_review()
        last_uid = str(self._review_workspace.get("last_event_uid", "") or "")
        if last_uid:
            self._refresh_review_dialog(last_uid)
        self.statusBar().showMessage("已恢复标注复核进度", 5000)

    def export_annotation_review(
        self, output_directory: str | os.PathLike[str] | None = None
    ) -> None:
        self._sync_active_review_session()
        counts = review_counts(self._review_workspace)
        unfinished = counts["pending"] + counts["modified"] + counts["missing_json"]
        if unfinished and output_directory is None:
            answer = QMessageBox.warning(
                self,
                "仍有未完成复核",
                f"还有 {unfinished} 条待复核、待确认或缺少 JSON。\n\n仍然导出当前结果？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if output_directory is None or isinstance(output_directory, bool):
            default = self.settings.value("review_export_dir", "")
            if not default:
                sessions = self._review_workspace.get("sessions", [])
                first_csv = (
                    str(sessions[0].get("csv_path", ""))
                    if sessions and isinstance(sessions[0], dict)
                    else ""
                )
                default = str(Path(first_csv).parent / "复核结果") if first_csv else str(Path.cwd())
            selected = QFileDialog.getExistingDirectory(
                self, "选择复核结果输出目录", str(default)
            )
            output_directory = selected
        if not output_directory:
            return
        try:
            created = export_review_results(
                self._review_workspace, output_directory
            )
        except (OSError, TypeError, ValueError) as exc:
            self._show_error(f"复核结果导出失败：\n{exc}")
            return
        self.settings.setValue("review_export_dir", str(output_directory))
        self._save_review_workspace()
        self.statusBar().showMessage(
            f"复核结果已导出 {len(created)} 个文件；原 CSV 未覆盖",
            7000,
        )

    def clear_annotation_review(self, _checked: bool = False) -> None:
        if not self._review_workspace.get("sessions"):
            return
        if QMessageBox.question(
            self,
            "清空复核队列",
            "确定清空当前复核队列？已导出的文件不会删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._review_workspace = new_workspace(
            str(self.settings.value("review_json_root", "") or "")
        )
        self._review_active_session_key = ""
        self._review_entry_confirmed = False
        if self._review_autosave_path is not None:
            try:
                self._review_autosave_path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._review_dialog is not None:
            self._review_dialog.hide()
        if self._review_dock is not None:
            self._review_dock.hide()
        self._refresh_review_actions()

    def _autosave(self) -> None:
        super()._autosave()
        if self._review_loading_session:
            return
        self._sync_active_review_session()
        self._save_review_workspace()

    def open_json(self, path: str | None = None) -> None:
        manual_switch = bool(
            self._review_active_session_key and not self._review_loading_session
        )
        if manual_switch:
            self._sync_active_review_session()
            self._save_review_workspace()
            self._review_active_session_key = ""
        super().open_json(path)

    def load_project_dialog(self, path: str | None = None) -> None:
        if self._review_active_session_key and not self._review_loading_session:
            self._sync_active_review_session()
            self._save_review_workspace()
            self._review_active_session_key = ""
        super().load_project_dialog(path)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._sync_active_review_session()
        self._save_review_workspace()
        super().closeEvent(event)


__all__ = [
    "AnnotationReviewDialog",
    "AnnotationReviewMixin",
    "ReviewEventEditDialog",
]
