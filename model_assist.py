from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
)

from prediction_edit import (
    BODY_CODES,
    MODEL_EDIT_CODES,
    MODEL_LABEL_LAYERS,
    MODEL_LABEL_NAMES,
    RESEARCH_MODEL_CODES,
    PredictionEditDialog,
    adjust_prediction_row,
)
from ui_helpers import format_relative


APP_DIR = Path(__file__).resolve().parent
MODEL_RUNTIME_DIR = APP_DIR / "model_runtime"
CAUSAL_PACKAGE_ROOT = APP_DIR.parent / "牛尾环IMU_20260815_整理包"
DEFAULT_MODEL_PATH = (
    CAUSAL_PACKAGE_ROOT
    / "复现实验"
    / "development_all"
    / "fold_0_None_20260815_111619"
    / "best.pt"
)
PREDICTION_CACHE_DIR = APP_DIR / "prediction_cache"

RECOMMENDATION_THRESHOLDS = {
    "STANDING": 0.60,
    "LYING": 0.60,
    "WALKING": 0.60,
    "STANDING_UP": 0.55,
    "LYING_DOWN": 0.55,
    "URINATION": 0.55,
}
TABLE_SORT_ROLE = int(Qt.ItemDataRole.UserRole)
TABLE_PREDICTION_INDEX_ROLE = TABLE_SORT_ROLE + 1


def prediction_fingerprint(row: dict[str, Any]) -> str:
    """Stable identity used to prevent repeated imports of one suggestion."""

    code = str(row.get("code", "")).strip().upper()
    start = int(round(float(row.get("start_ms", 0.0))))
    end = int(round(float(row.get("end_ms", 0.0))))
    return f"{code}:{start}:{end}"


def intervals_overlap(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> bool:
    return left_start < right_end and left_end > right_start


def interval_iou(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> float:
    intersection = max(
        0.0, min(left_end, right_end) - max(left_start, right_start)
    )
    union = max(left_end, right_end) - min(left_start, right_start)
    return intersection / union if union > 0 else 0.0


def json_safe(value: Any) -> Any:
    """Convert model metadata to strict JSON, including non-finite metrics."""

    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


class SortableTableWidgetItem(QTableWidgetItem):
    """Display formatted text while sorting by the underlying numeric value."""

    def __init__(self, text: str, sort_value: Any = None) -> None:
        super().__init__(text)
        if sort_value is not None:
            self.setData(TABLE_SORT_ROLE, sort_value)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(TABLE_SORT_ROLE)
        right = other.data(TABLE_SORT_ROLE)
        if left is not None and right is not None:
            try:
                return left < right
            except TypeError:
                return str(left) < str(right)
        return super().__lt__(other)


class PredictionWorker(QThread):
    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, model_path: Path, imu_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.model_path = model_path
        self.imu_path = imu_path

    def run(self) -> None:
        try:
            runtime = str(MODEL_RUNTIME_DIR)
            if runtime not in sys.path:
                sys.path.insert(0, runtime)
            from imu_behavior.inference import predict_imu

            result = predict_imu(
                self.model_path,
                self.imu_path,
                progress=lambda percent, message: self.progress.emit(
                    percent, message
                ),
            )
            # The dense probability arrays are not required by the annotation UI.
            result.pop("_arrays", None)
            self.succeeded.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())


class PredictionReviewDialog(QDialog):
    """Review model intervals before any suggestion becomes an annotation."""

    def __init__(
        self,
        predictions: list[dict[str, Any]],
        locate_callback: Callable[[float], None],
        runtime_warnings: list[str] | None = None,
        duration_ms: float = 0.0,
        current_position_callback: Callable[[], float] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.predictions = predictions
        self.locate_callback = locate_callback
        self.duration_ms = float(duration_ms)
        self.current_position_callback = current_position_callback
        self.setWindowTitle("复核模型建议")
        self.resize(1180, 700)

        layout = QVBoxLayout(self)
        summary = QLabel(
            "模型结果只作为建议。勾选并点击“导入选中建议”后才会进入标注表；"
            "导入项会标记为“待人工复核”。双击任意行可定位到建议开始时间。"
            "选择一行后可修改标签和起止边界。点击“平均置信度”或“最高置信度”"
            "表头可切换升序/降序。"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        warning_text = (
            "当前使用新版因果模型：站立/躺卧来自姿态头，行走作为直立子状态叠加；"
            "事件候选按 checkpoint 阈值和新版分类别合并规则生成。排便、抬尾、甩尾"
            "仍是研究性候选，默认不勾选；"
            "努责和分娩节点仅支持人工标注。"
        )
        if runtime_warnings:
            warning_text += "\n\n运行提示：" + "；".join(
                str(value) for value in runtime_warnings if str(value).strip()
            )
        warning = QLabel(warning_text)
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background:#fff4d6;color:#7a4b00;border:1px solid #e8c66a;"
            "border-radius:4px;padding:7px;"
        )
        layout.addWidget(warning)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "导入", "层", "标签", "候选级别", "开始", "结束", "时长",
                "平均置信度", "最高置信度", "建议说明",
            ]
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        for column in (7, 8):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setToolTip("点击切换升序/降序；按真实数值排序")
        self.table.cellDoubleClicked.connect(self._locate_row)
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        recommended_btn = QPushButton("恢复推荐选择")
        all_btn = QPushButton("全选")
        none_btn = QPushButton("全不选")
        locate_btn = QPushButton("定位选中")
        edit_btn = QPushButton("修改选中建议")
        recommended_btn.clicked.connect(self._select_recommended)
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        locate_btn.clicked.connect(self._locate_selected)
        edit_btn.clicked.connect(self._edit_selected)
        for button in (recommended_btn, all_btn, none_btn, locate_btn, edit_btn):
            controls.addWidget(button)
        controls.addStretch()
        cancel_btn = QPushButton("取消")
        accept_btn = QPushButton("导入选中建议")
        accept_btn.setDefault(True)
        cancel_btn.clicked.connect(self.reject)
        accept_btn.clicked.connect(self.accept)
        controls.addWidget(cancel_btn)
        controls.addWidget(accept_btn)
        layout.addLayout(controls)

        self._populate()

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.predictions))
        layer_names = {
            "body_state": "身体行为",
            "tail_action": "尾部动作",
            "posture_transition": "姿态转换",
            "objective_event": "客观事件",
        }
        priority_names = {
            "high_score": "高分",
            "uncertain": "边界/不确定",
            "research": "研究扩样",
            "standard": "常规",
        }
        for row_index, prediction in enumerate(self.predictions):
            check = QTableWidgetItem("")
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setData(TABLE_PREDICTION_INDEX_ROLE, row_index)
            check.setCheckState(
                Qt.CheckState.Checked
                if prediction.get("_recommended", False)
                else Qt.CheckState.Unchecked
            )
            self.table.setItem(row_index, 0, check)

            start = float(prediction.get("start_ms", 0.0))
            end = float(prediction.get("end_ms", 0.0))
            mean_confidence = float(prediction.get("confidence_mean", 0.0))
            max_confidence = float(prediction.get("confidence_max", 0.0))
            values = [
                (
                    layer_names.get(
                        str(prediction.get("layer", "")),
                        str(prediction.get("layer", "")),
                    ),
                    str(prediction.get("layer", "")),
                ),
                (
                    str(prediction.get("label", prediction.get("code", ""))),
                    str(prediction.get("label", prediction.get("code", ""))),
                ),
                (
                    priority_names.get(
                        str(prediction.get("review_priority", "standard")),
                        str(prediction.get("review_priority", "")),
                    ),
                    str(prediction.get("review_priority", "standard")),
                ),
                (format_relative(start), start),
                (format_relative(end), end),
                (format_relative(max(0.0, end - start)), max(0.0, end - start)),
                (f"{mean_confidence:.1%}", mean_confidence),
                (f"{max_confidence:.1%}", max_confidence),
                (
                    str(prediction.get("_review_note", "建议导入")),
                    str(prediction.get("_review_note", "建议导入")),
                ),
            ]
            for column, (value, sort_value) in enumerate(values, 1):
                item = SortableTableWidgetItem(value, sort_value)
                item.setData(TABLE_PREDICTION_INDEX_ROLE, row_index)
                if not prediction.get("_recommended", False):
                    item.setBackground(QColor("#fff8e8"))
                self.table.setItem(row_index, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(4, Qt.SortOrder.AscendingOrder)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _select_recommended(self) -> None:
        for row in range(self.table.rowCount()):
            prediction = self._prediction_for_table_row(row)
            item = self.table.item(row, 0)
            if item is not None and prediction is not None:
                item.setCheckState(
                    Qt.CheckState.Checked
                    if prediction.get("_recommended", False)
                    else Qt.CheckState.Unchecked
                )

    def _prediction_for_table_row(self, row: int) -> dict[str, Any] | None:
        item = self.table.item(row, 0)
        if item is None:
            return None
        source_index = item.data(TABLE_PREDICTION_INDEX_ROLE)
        try:
            index = int(source_index)
        except (TypeError, ValueError):
            return None
        return self.predictions[index] if 0 <= index < len(self.predictions) else None

    def _locate_row(self, row: int, _column: int) -> None:
        prediction = self._prediction_for_table_row(row)
        if prediction is not None:
            self.locate_callback(float(prediction["start_ms"]))

    def _locate_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._locate_row(rows[0].row(), 0)

    def _edit_selected(self) -> None:
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "修改预测标注", "请先选择一条模型建议。")
            return
        table_row = selected_rows[0].row()
        prediction = self._prediction_for_table_row(table_row)
        if prediction is None:
            return
        check_states: dict[int, Qt.CheckState] = {}
        for row in range(self.table.rowCount()):
            source = self._prediction_for_table_row(row)
            check_item = self.table.item(row, 0)
            if source is not None and check_item is not None:
                check_states[id(source)] = check_item.checkState()
        options = [
            (code, MODEL_LABEL_NAMES[code], MODEL_LABEL_LAYERS[code])
            for code in MODEL_EDIT_CODES
        ]
        dialog = PredictionEditDialog(
            prediction,
            options,
            self.duration_ms,
            self.current_position_callback,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        code, name, layer, start_ms, end_ms = dialog.values()
        updated = adjust_prediction_row(
            prediction,
            code=code,
            label=name,
            layer=layer,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        prediction.clear()
        prediction.update(updated)
        self._populate()
        for row in range(self.table.rowCount()):
            source = self._prediction_for_table_row(row)
            check_item = self.table.item(row, 0)
            if source is not None and check_item is not None:
                check_item.setCheckState(
                    Qt.CheckState.Checked
                    if source is prediction
                    else check_states.get(id(source), check_item.checkState())
                )
            if source is prediction:
                self.table.selectRow(row)

    def selected_predictions(self) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            prediction = self._prediction_for_table_row(row)
            if (
                item is not None
                and prediction is not None
                and item.checkState() == Qt.CheckState.Checked
            ):
                selected.append(prediction)
        return selected


class ModelAssistMixin:
    """Adds safe, review-first model suggestions to the copied v7 tool."""

    def __init__(self) -> None:
        self._prediction_worker: PredictionWorker | None = None
        self._prediction_progress: QProgressDialog | None = None
        self._prediction_source_path = ""
        self._last_prediction_cache_path = ""
        super().__init__()
        self._install_model_assist_ui()

    def _install_model_assist_ui(self) -> None:
        toolbar = self.main_toolbar
        self.model_assist_toolbar = toolbar
        self.model_predict_action = QAction("开始模型预测", self)
        self.model_predict_action.setToolTip(
            "对当前九轴 JSON 运行新版因果模型，复核后选择性导入建议"
        )
        self.model_select_action = QAction("模型设置…", self)
        self.model_select_action.setToolTip("选择 CausalMultiTaskTCN checkpoint")
        self.model_waveform_adjust_action = QAction("调整预测区间", self)
        self.model_waveform_adjust_action.setToolTip(
            "在真实九轴波形上高亮预测区间，拖动左右边界进行校准（Ctrl+E）"
        )
        self.model_waveform_adjust_action.setShortcut("Ctrl+E")
        self.model_edit_prediction_action = QAction("修改标签/时间…", self)
        self.model_edit_prediction_action.setToolTip(
            "通过输入框修改已导入模型建议的标签、开始时间或结束时间"
        )
        self.model_mark_reviewed_action = QAction("确认已复核", self)
        self.model_mark_reviewed_action.setToolTip(
            "把事件表中选中的待复核模型建议标记为已人工复核"
        )
        self.model_assist_button = QToolButton()
        self.model_assist_button.setText("模型辅助")
        self.model_assist_button.setToolTip("模型预测、复核调整与模型设置")
        self.model_assist_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.model_assist_menu = QMenu(self.model_assist_button)
        self.model_assist_menu.addAction(self.model_predict_action)
        self.model_assist_menu.addSeparator()
        self.model_assist_menu.addAction(self.model_waveform_adjust_action)
        self.model_assist_menu.addAction(self.model_edit_prediction_action)
        self.model_assist_menu.addAction(self.model_mark_reviewed_action)
        self.model_assist_menu.addSeparator()
        self.model_assist_menu.addAction(self.model_select_action)
        self.model_assist_button.setMenu(self.model_assist_menu)
        before = getattr(self, "toolbar_spacer_action", None)
        if before is not None:
            self.model_assist_widget_action = toolbar.insertWidget(
                before, self.model_assist_button
            )
        else:
            self.model_assist_widget_action = toolbar.addWidget(
                self.model_assist_button
            )
        self.model_predict_action.triggered.connect(self.run_model_prediction)
        self.model_waveform_adjust_action.triggered.connect(
            self.focus_selected_prediction_on_waveform
        )
        self.model_edit_prediction_action.triggered.connect(
            self.edit_selected_prediction
        )
        self.model_mark_reviewed_action.triggered.connect(
            self.mark_selected_prediction_reviewed
        )
        self.model_select_action.triggered.connect(self.choose_prediction_model)
        self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        super()._refresh_enabled()
        action = getattr(self, "model_predict_action", None)
        if action is not None:
            action.setEnabled(
                self.data is not None
                and bool(self.data_path)
                and not self._prediction_is_running()
            )
        self._update_model_review_action()

    def _event_selected(self) -> None:
        super()._event_selected()
        self._update_model_review_action()
        event = self._selected_model_event()
        if (
            event is not None
            and getattr(self.plot, "_event_drag", None) is None
        ):
            self._focus_prediction_on_waveform(event, announce=True)

    def _selected_model_event(self) -> dict[str, Any] | None:
        if self.selected_event_id is None:
            return None
        return next(
            (
                event
                for event in self.events
                if int(event.get("id", -1)) == self.selected_event_id
                and event.get("prediction_source") == "imu_model"
            ),
            None,
        )

    def _update_model_review_action(self) -> None:
        action = getattr(self, "model_mark_reviewed_action", None)
        edit_action = getattr(self, "model_edit_prediction_action", None)
        waveform_action = getattr(self, "model_waveform_adjust_action", None)
        event = self._selected_model_event()
        if edit_action is not None:
            edit_action.setEnabled(event is not None)
        if waveform_action is not None:
            waveform_action.setEnabled(event is not None)
        if action is not None:
            action.setEnabled(
                event is not None
                and event.get("prediction_review_status") == "pending"
            )

    def _event_double_clicked(self, row: int, column: int) -> None:
        item = self.event_table.item(row, 0)
        if item is not None:
            event_id = int(item.data(256))
            event = next(
                (
                    value
                    for value in self.events
                    if int(value.get("id", -1)) == event_id
                ),
                None,
            )
            if event is not None and event.get("prediction_source") == "imu_model":
                self.selected_event_id = event_id
                self.focus_selected_prediction_on_waveform()
                return
        super()._event_double_clicked(row, column)

    def _focus_prediction_on_waveform(
        self, event: dict[str, Any], *, announce: bool
    ) -> None:
        focus_event = getattr(self.plot, "focus_event", None)
        if callable(focus_event):
            focus_event(int(event.get("id", -1)))
        else:
            self.plot.set_selected_event(int(event.get("id", -1)))
        if announce:
            self.statusBar().showMessage(
                "预测区间已覆盖在真实波形上：拖动左右竖线调整边界；"
                "滚轮缩放、Shift+拖动平移；整体移动可使用“修改标签/时间…”",
                6000,
            )

    def focus_selected_prediction_on_waveform(
        self, _checked: bool = False
    ) -> None:
        event = self._selected_model_event()
        if event is None:
            self.statusBar().showMessage(
                "请先在事件表中选择一条模型预测标注", 3000
            )
            return
        self._focus_prediction_on_waveform(event, announce=True)
        self.set_playhead(float(event.get("t0", 0.0)))
        self.plot.setFocus()

    def _event_changed_on_plot(
        self, event_id: int, start_ms: float, end_ms: Any
    ) -> None:
        snapshot = getattr(self, "_drag_history_snapshot", None)
        previous_event = None
        if isinstance(snapshot, dict):
            previous_event = next(
                (
                    value
                    for value in snapshot.get("events", [])
                    if int(value.get("id", -1)) == int(event_id)
                ),
                None,
            )
        current_event = next(
            (
                value
                for value in self.events
                if int(value.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if (
            current_event is not None
            and previous_event is not None
            and current_event.get("prediction_source") == "imu_model"
            and current_event.get("t1") is not None
        ):
            previous_start = float(previous_event.get("t0", 0.0))
            previous_end = float(previous_event.get("t1", previous_start))
            current_start = float(current_event.get("t0", 0.0))
            current_end = float(current_event["t1"])
            changed = (
                abs(previous_start - current_start) >= 0.5
                or abs(previous_end - current_end) >= 0.5
            )
            if changed:
                candidate = {
                    "code": self._event_code(current_event),
                    "start_ms": current_start,
                    "end_ms": current_end,
                }
                other_events = [
                    value
                    for value in self.events
                    if int(value.get("id", -1)) != int(event_id)
                ]
                conflict_message = ""
                if self._body_conflicts(candidate, other_events):
                    conflict_message = (
                        "调整后的身体行为与已有身体行为区间重叠，"
                        "本次拖动已撤回。"
                    )
                elif self._matches_existing_annotation(candidate, other_events):
                    conflict_message = (
                        "调整后与已有同类标注高度重合，本次拖动已撤回。"
                    )
                if conflict_message:
                    current_event["t0"] = previous_start
                    current_event["t1"] = previous_end
                    self.plot.set_events(self.labels, self.events)
                    super()._event_changed_on_plot(
                        event_id, previous_start, previous_end
                    )
                    QMessageBox.warning(self, "预测区间冲突", conflict_message)
                    return
        super()._event_changed_on_plot(event_id, start_ms, end_ms)
        event = next(
            (
                value
                for value in self.events
                if int(value.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if (
            event is None
            or previous_event is None
            or event.get("prediction_source") != "imu_model"
            or event.get("t1") is None
        ):
            return
        previous_start = float(previous_event.get("t0", 0.0))
        previous_end = float(previous_event.get("t1", previous_start))
        current_start = float(event.get("t0", 0.0))
        current_end = float(event["t1"])
        if (
            abs(previous_start - current_start) < 0.5
            and abs(previous_end - current_end) < 0.5
        ):
            return
        code = self._event_code(event)
        trace = adjust_prediction_row(
            {
                "code": code,
                "start_ms": previous_start,
                "end_ms": previous_end,
                "prediction_original_code": event.get(
                    "prediction_original_code", code
                ),
                "prediction_original_start_ms": event.get(
                    "prediction_original_start_ms", previous_start
                ),
                "prediction_original_end_ms": event.get(
                    "prediction_original_end_ms", previous_end
                ),
                "prediction_adjustment_history": event.get(
                    "prediction_adjustment_history", []
                ),
            },
            code=code,
            label=MODEL_LABEL_NAMES.get(code, code),
            layer=str(event.get("layer", "")),
            start_ms=current_start,
            end_ms=current_end,
        )
        event["prediction_original_code"] = trace["prediction_original_code"]
        event["prediction_original_start_ms"] = trace[
            "prediction_original_start_ms"
        ]
        event["prediction_original_end_ms"] = trace["prediction_original_end_ms"]
        event["prediction_human_adjusted"] = True
        event["prediction_review_status"] = "pending"
        event.pop("prediction_reviewed_at", None)
        event.pop("prediction_reviewed_by", None)
        event["prediction_adjustment_history"] = trace[
            "prediction_adjustment_history"
        ]
        event["prediction_fingerprint"] = prediction_fingerprint(
            {"code": code, "start_ms": current_start, "end_ms": current_end}
        )
        marker = "[人工调整预测边界]"
        note = str(event.get("note", "")).replace(
            "[模型建议·已人工复核]", "[模型建议·待人工复核]"
        )
        if marker not in note:
            note = f"{note} {marker}".strip()
        event["note"] = note
        self._refresh_events()
        self._autosave()
        self.statusBar().showMessage("已调整预测边界，并保留原始模型时间", 3000)

    def _model_label_options(self) -> list[tuple[str, str, str]]:
        by_code = {
            str(label.get("code", "")).upper(): label for label in self.labels
        }
        return [
            (
                code,
                str(by_code.get(code, {}).get("name", MODEL_LABEL_NAMES[code])),
                str(by_code.get(code, {}).get("layer", MODEL_LABEL_LAYERS[code])),
            )
            for code in MODEL_EDIT_CODES
            if code in by_code
        ]

    def edit_selected_prediction(self, _checked: bool = False) -> None:
        event = self._selected_model_event()
        if event is None:
            self.statusBar().showMessage("请先在事件表中选择一条模型预测标注", 3000)
            return
        code = self._event_code(event)
        label_index = int(event.get("li", -1))
        current_label = (
            self.labels[label_index]
            if 0 <= label_index < len(self.labels)
            else {}
        )
        editable = {
            "code": code,
            "label": str(current_label.get("name", MODEL_LABEL_NAMES.get(code, code))),
            "layer": str(current_label.get("layer", event.get("layer", ""))),
            "start_ms": float(event.get("t0", 0.0)),
            "end_ms": float(event.get("t1", event.get("t0", 0.0))),
            "prediction_original_code": event.get("prediction_original_code", code),
            "prediction_original_start_ms": event.get(
                "prediction_original_start_ms", float(event.get("t0", 0.0))
            ),
            "prediction_original_end_ms": event.get(
                "prediction_original_end_ms",
                float(event.get("t1", event.get("t0", 0.0))),
            ),
            "prediction_adjustment_history": event.get(
                "prediction_adjustment_history", []
            ),
        }
        dialog = PredictionEditDialog(
            editable,
            self._model_label_options(),
            self.data_duration_ms,
            lambda: float(self.playhead_ms),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_code, name, layer, start_ms, end_ms = dialog.values()
        candidate = {
            "code": new_code,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        other_events = [
            value
            for value in self.events
            if int(value.get("id", -1)) != int(event.get("id", -1))
        ]
        if self._body_conflicts(candidate, other_events):
            QMessageBox.warning(
                self,
                "身体行为冲突",
                "修改后的身体行为与已有身体行为区间重叠，请先调整边界。",
            )
            return
        if self._matches_existing_annotation(candidate, other_events):
            QMessageBox.warning(
                self,
                "重复标注",
                "已有高度重合的同类标注，本次修改未保存。",
            )
            return

        before = self._annotation_snapshot() if hasattr(self, "_annotation_snapshot") else None
        updated = adjust_prediction_row(
            editable,
            code=new_code,
            label=name,
            layer=layer,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        new_label_index = self._label_index_for_code(new_code)
        if new_label_index is None:
            self._show_error(f"当前标注协议中不存在标签 {new_code}")
            return
        event.update(
            {
                "li": new_label_index,
                "label_code": new_code,
                "layer": layer,
                "t0": start_ms,
                "t1": end_ms,
                "reviewed_range": {"start": start_ms, "end": end_ms},
                "prediction_fingerprint": prediction_fingerprint(updated),
                "prediction_original_code": updated["prediction_original_code"],
                "prediction_original_start_ms": updated[
                    "prediction_original_start_ms"
                ],
                "prediction_original_end_ms": updated["prediction_original_end_ms"],
                "prediction_human_adjusted": True,
                "prediction_review_status": "pending",
                "prediction_candidate_type": updated["candidate_type"],
                "prediction_model_support": updated["model_support"],
                "prediction_review_priority": updated["review_priority"],
                "prediction_adjustment_history": updated[
                    "prediction_adjustment_history"
                ],
            }
        )
        times = np.asarray(getattr(self.data, "times_ms", []), dtype=float)
        if times.size:
            event["json_sample_start"] = int(
                np.searchsorted(times, start_ms, side="left")
            )
            event["json_sample_end"] = int(
                np.searchsorted(times, end_ms, side="left")
            )
        note = str(event.get("note", ""))
        note = note.replace(
            "[模型建议·已人工复核]", "[模型建议·待人工复核]"
        )
        adjustment_marker = "[人工调整预测标签/边界]"
        if adjustment_marker not in note:
            event["note"] = f"{note} {adjustment_marker}".strip()
        else:
            event["note"] = note
        event.pop("prediction_reviewed_at", None)
        event.pop("prediction_reviewed_by", None)
        self.events.sort(
            key=lambda item: (float(item.get("t0", 0.0)), int(item.get("id", 0)))
        )
        self._refresh_events()
        self._autosave()
        if before is not None and hasattr(self, "_checkpoint_if_changed"):
            self._checkpoint_if_changed(before)
        self.set_playhead(start_ms)
        self.statusBar().showMessage("已修改预测标注，并保留原始模型结果", 4000)

    def mark_selected_prediction_reviewed(
        self, _checked: bool = False
    ) -> None:
        event = self._selected_model_event()
        if event is None:
            self.statusBar().showMessage("请先在事件表中选择一条模型建议", 3000)
            return
        if event.get("prediction_review_status") == "reviewed":
            self.statusBar().showMessage("该模型建议已经人工复核", 3000)
            return
        event["prediction_review_status"] = "reviewed"
        event["prediction_reviewed_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        event["prediction_reviewed_by"] = str(
            self.annotator_edit.text()
        ).strip()
        note = str(event.get("note", ""))
        event["note"] = note.replace(
            "[模型建议·待人工复核]", "[模型建议·已人工复核]", 1
        )
        self._refresh_events()
        self._autosave()
        self.statusBar().showMessage("已将选中的模型建议标记为人工复核", 4000)

    def _prediction_is_running(self) -> bool:
        return (
            self._prediction_worker is not None
            and self._prediction_worker.isRunning()
        )

    def _configured_model_path(self) -> Path:
        configured = str(
            self.settings.value("model_assist/model_path", "") or ""
        ).strip()
        configured_path = Path(configured) if configured else None
        if configured_path is not None and configured_path.is_file():
            try:
                runtime = str(MODEL_RUNTIME_DIR)
                if runtime not in sys.path:
                    sys.path.insert(0, runtime)
                from imu_behavior.checkpoint import inspect_checkpoint

                _, payload = inspect_checkpoint(configured_path)
                if payload.get("model_class") == "CausalMultiTaskTCN":
                    return configured_path
            except Exception:
                pass
        return DEFAULT_MODEL_PATH

    def choose_prediction_model(self, _checked: bool = False) -> Path | None:
        current = self._configured_model_path()
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择新版因果模型权重",
            str(current.parent if current.exists() else APP_DIR),
            "PyTorch 模型 (*.pt);;所有文件 (*.*)",
        )
        if not selected:
            return None
        path = Path(selected).resolve()
        try:
            runtime = str(MODEL_RUNTIME_DIR)
            if runtime not in sys.path:
                sys.path.insert(0, runtime)
            from imu_behavior.checkpoint import inspect_checkpoint

            _, payload = inspect_checkpoint(path)
            model_class = str(
                payload.get("model_class") or payload.get("architecture") or ""
            )
            if model_class in {"SSLTemporalEncoder", "LegacySSLTemporalEncoder"}:
                self._show_error(
                    "这是一份自监督 SSL 表征权重，只有编码器、没有行为分类头，"
                    "不能直接用于自动标注。\n\n"
                    "请选择复现实验\\development_all 下的新版 "
                    "CausalMultiTaskTCN checkpoint。"
                )
                return None
            if model_class != "CausalMultiTaskTCN":
                self._show_error(
                    "当前工具已切换到新版预测流程，不再从界面运行旧固定中心窗模型。\n\n"
                    f"所选类型：{model_class or '无法识别'}\n{path}\n\n"
                    "请选择 CausalMultiTaskTCN checkpoint。"
                )
                return None
        except Exception as exc:
            self._show_error(f"无法安全读取所选模型：\n{path}\n\n{exc}")
            return None
        self.settings.setValue("model_assist/model_path", str(path))
        self.statusBar().showMessage(
            f"已选择 {model_class} 模型：{path}", 5000
        )
        return path

    def run_model_prediction(self, _checked: bool = False) -> None:
        if self._prediction_is_running():
            self.statusBar().showMessage("模型预测仍在运行，请稍候", 3000)
            return
        if self.data is None or not self.data_path:
            self._show_error("请先打开需要辅助标注的九轴 JSON。")
            return
        model_path = self._configured_model_path()
        if not model_path.is_file():
            selected = self.choose_prediction_model()
            if selected is None:
                return
            model_path = selected
        if not MODEL_RUNTIME_DIR.is_dir():
            self._show_error(
                f"模型推理模块不存在：\n{MODEL_RUNTIME_DIR}\n"
                "请重新复制完整的整合工具。"
            )
            return

        imu_path = Path(self.data_path).resolve()
        self._prediction_source_path = os.path.normcase(str(imu_path))
        progress = QProgressDialog(
            "正在准备模型预测…", "", 0, 100, self
        )
        progress.setWindowTitle("模型辅助预测")
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        self._prediction_progress = progress

        worker = PredictionWorker(model_path, imu_path, self)
        self._prediction_worker = worker
        worker.progress.connect(self._on_prediction_progress)
        worker.succeeded.connect(self._on_prediction_succeeded)
        worker.failed.connect(self._on_prediction_failed)
        worker.finished.connect(self._on_prediction_finished)
        worker.start()
        self._refresh_enabled()

    def _on_prediction_progress(self, percent: int, message: str) -> None:
        if self._prediction_progress is not None:
            self._prediction_progress.setValue(int(percent))
            self._prediction_progress.setLabelText(message)
        self.statusBar().showMessage(message)

    def _close_prediction_progress(self) -> None:
        if self._prediction_progress is not None:
            self._prediction_progress.close()
            self._prediction_progress.deleteLater()
            self._prediction_progress = None

    def _on_prediction_failed(self, details: str) -> None:
        self._close_prediction_progress()
        lines = [line.strip() for line in details.splitlines() if line.strip()]
        summary = lines[-1] if lines else "未知错误"
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Critical)
        message.setWindowTitle("模型预测失败")
        message.setText("模型预测未完成")
        message.setInformativeText(summary)
        message.setDetailedText(details)
        message.exec()

    def _on_prediction_finished(self) -> None:
        worker = self._prediction_worker
        self._prediction_worker = None
        if worker is not None:
            worker.deleteLater()
        self._refresh_enabled()

    def _on_prediction_succeeded(self, result: dict[str, Any]) -> None:
        self._close_prediction_progress()
        current_source = os.path.normcase(str(Path(self.data_path).resolve()))
        if current_source != self._prediction_source_path:
            QMessageBox.warning(
                self,
                "数据已经切换",
                "预测期间当前九轴 JSON 已改变，本次结果不会导入。",
            )
            return

        rows = [dict(row) for row in result.get("prediction_intervals", [])]
        cache_path = self._save_prediction_cache(result)
        self._last_prediction_cache_path = str(cache_path) if cache_path else ""
        if not rows:
            QMessageBox.information(self, "模型辅助预测", "模型没有生成任何建议区间。")
            return

        prepared = self._prepare_predictions_for_review(rows)
        dialog = PredictionReviewDialog(
            prepared,
            lambda position: self.set_playhead(position),
            runtime_warnings=[str(value) for value in result.get("warnings", [])],
            duration_ms=self.data_duration_ms,
            current_position_callback=lambda: float(self.playhead_ms),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.statusBar().showMessage(
                f"预测完成，共 {len(rows)} 条建议；未导入", 5000
            )
            return
        selected = dialog.selected_predictions()
        imported, duplicates, conflicts = self._import_predictions(
            selected, result
        )
        message = f"已导入 {imported} 条待复核模型建议"
        if duplicates:
            message += f"，跳过重复 {duplicates} 条"
        if conflicts:
            message += f"，跳过身体行为冲突 {conflicts} 条"
        if cache_path:
            message += f"。预测摘要：{cache_path}"
        QMessageBox.information(self, "模型辅助预测", message)

    def _existing_fingerprints(self) -> set[str]:
        return {
            str(event.get("prediction_fingerprint", ""))
            for event in self.events
            if event.get("prediction_fingerprint")
        }

    def _body_conflicts(
        self,
        prediction: dict[str, Any],
        events: list[dict[str, Any]] | None = None,
    ) -> bool:
        code = str(prediction.get("code", "")).upper()
        if code not in BODY_CODES:
            return False
        start = float(prediction.get("start_ms", 0.0))
        end = float(prediction.get("end_ms", 0.0))
        for event in self.events if events is None else events:
            label_index = int(event.get("li", -1))
            if not (0 <= label_index < len(self.labels)):
                continue
            label = self.labels[label_index]
            if str(label.get("layer", "")) != "body_state":
                continue
            event_end = event.get("t1")
            if event_end is None:
                continue
            if intervals_overlap(
                start,
                end,
                float(event.get("t0", 0.0)),
                float(event_end),
            ):
                return True
        return False

    def _event_code(self, event: dict[str, Any]) -> str:
        code = str(event.get("label_code", "")).strip().upper()
        if code:
            return code
        label_index = int(event.get("li", -1))
        if 0 <= label_index < len(self.labels):
            return str(self.labels[label_index].get("code", "")).strip().upper()
        return ""

    def _matches_existing_annotation(
        self,
        prediction: dict[str, Any],
        events: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Catch the same suggestion even when a newer model shifts its edges."""

        code = str(prediction.get("code", "")).strip().upper()
        start = float(prediction.get("start_ms", 0.0))
        end = float(prediction.get("end_ms", 0.0))
        if not code or end <= start:
            return False
        for event in self.events if events is None else events:
            if self._event_code(event) != code or event.get("t1") is None:
                continue
            existing_start = float(event.get("t0", 0.0))
            existing_end = float(event["t1"])
            if interval_iou(start, end, existing_start, existing_end) >= 0.70:
                return True
            tolerance = max(500.0, 0.10 * max(end - start, existing_end - existing_start))
            if (
                abs(start - existing_start) <= tolerance
                and abs(end - existing_end) <= tolerance
            ):
                return True
        return False

    def _prepare_predictions_for_review(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        fingerprints = self._existing_fingerprints()
        prepared: list[dict[str, Any]] = []
        for source_row in rows:
            row = dict(source_row)
            code = str(row.get("code", "")).upper()
            confidence = float(row.get("confidence_mean", 0.0))
            notes: list[str] = []
            threshold = RECOMMENDATION_THRESHOLDS.get(
                code, 0.60 if code in BODY_CODES else 0.55
            )
            recommended = bool(
                row.get("review_recommended", confidence >= threshold)
            )
            if code in RESEARCH_MODEL_CODES or row.get("model_support") == "research":
                recommended = False
                notes.append("研究性候选，仅用于扩样")
            if prediction_fingerprint(row) in fingerprints:
                recommended = False
                notes.append("已导入过")
            elif self._matches_existing_annotation(row):
                recommended = False
                notes.append("已有高度重合的同类标注")
            if self._body_conflicts(row):
                recommended = False
                notes.append("与现有身体行为重叠")
            if confidence < threshold:
                recommended = False
                notes.append("平均置信度偏低")
            if row.get("review_priority") == "uncertain":
                notes.append("阈值附近，优先核对边界/误报")
            row["_recommended"] = recommended
            row["_review_note"] = "；".join(notes) if notes else "建议导入并人工复核"
            prepared.append(row)
        return prepared

    def _label_index_for_code(self, code: str) -> int | None:
        for index, label in enumerate(self.labels):
            if str(label.get("code", "")).upper() == code.upper():
                return index
        return None

    def _import_predictions(
        self,
        selected: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> tuple[int, int, int]:
        existing_fingerprints = self._existing_fingerprints()
        imported = 0
        duplicates = 0
        conflicts = 0
        model_path = str(result.get("model", self._configured_model_path()))
        source_path = str(Path(self.data_path).resolve())
        working_events = list(self.events)
        times = np.asarray(getattr(self.data, "times_ms", []), dtype=float)
        duration = float(self.data_duration_ms)

        for row in selected:
            code = str(row.get("code", "")).strip().upper()
            label_index = self._label_index_for_code(code)
            if label_index is None:
                continue
            start = max(0.0, float(row.get("start_ms", 0.0)))
            end = min(duration, float(row.get("end_ms", duration)))
            if end <= start:
                continue
            normalized_row = {**row, "start_ms": start, "end_ms": end}
            fingerprint = prediction_fingerprint(normalized_row)
            if fingerprint in existing_fingerprints:
                duplicates += 1
                continue
            if self._matches_existing_annotation(normalized_row, working_events):
                duplicates += 1
                continue
            if self._body_conflicts(normalized_row, working_events):
                conflicts += 1
                continue

            mean_confidence = float(row.get("confidence_mean", 0.0))
            max_confidence = float(row.get("confidence_max", 0.0))
            model_support = str(row.get("model_support", "formal"))
            support_note = (
                "，研究性扩样候选" if model_support == "research" else ""
            )
            adjusted_note = "，人工已调整标签或边界" if row.get("human_adjusted") else ""
            fold_metadata = result.get("fold", {})
            if not isinstance(fold_metadata, dict):
                fold_metadata = {}
            event = {
                "id": self.next_event_id,
                "li": label_index,
                "label_code": code,
                "layer": str(self.labels[label_index].get("layer", "")),
                "t0": start,
                "t1": end,
                "note": (
                    "[模型建议·待人工复核] "
                    f"平均置信度 {mean_confidence:.1%}，最高 {max_confidence:.1%}"
                    f"{support_note}{adjusted_note}"
                ),
                "ev": "curve",
                "ctx": "",
                "reviewed_range": {"start": start, "end": end},
                "prediction_source": "imu_model",
                "prediction_review_status": "pending",
                "prediction_model": model_path,
                "prediction_imu": source_path,
                "prediction_confidence_mean": mean_confidence,
                "prediction_confidence_max": max_confidence,
                "prediction_fingerprint": fingerprint,
                "prediction_algorithm": str(result.get("algorithm", "")),
                "prediction_model_class": str(result.get("model_class", "")),
                "prediction_candidate_type": str(row.get("candidate_type", "")),
                "prediction_review_priority": str(row.get("review_priority", "")),
                "prediction_model_support": model_support,
                "prediction_source_head": str(row.get("source_head", "")),
                "prediction_decision_threshold": float(
                    row.get("decision_threshold", 0.0)
                ),
                "prediction_original_code": str(
                    row.get("prediction_original_code", code)
                ),
                "prediction_original_start_ms": float(
                    row.get("prediction_original_start_ms", start)
                ),
                "prediction_original_end_ms": float(
                    row.get("prediction_original_end_ms", end)
                ),
                "prediction_human_adjusted": bool(row.get("human_adjusted", False)),
                "prediction_adjustment_history": [
                    dict(item)
                    for item in row.get("prediction_adjustment_history", [])
                    if isinstance(item, dict)
                ],
                "prediction_fold": fold_metadata.get("fold"),
                "prediction_test_cow": fold_metadata.get("test_cow"),
            }
            if times.size:
                event["json_sample_start"] = int(
                    np.searchsorted(times, start, side="left")
                )
                event["json_sample_end"] = int(
                    np.searchsorted(times, end, side="left")
                )
            self.next_event_id += 1
            self.events.append(event)
            working_events.append(event)
            existing_fingerprints.add(fingerprint)
            imported += 1

        if imported:
            self.events.sort(
                key=lambda item: (float(item["t0"]), int(item["id"]))
            )
            self.pending_intervals.clear()
            self.selected_event_id = None
            self._refresh_events()
            self._autosave()
        return imported, duplicates, conflicts

    def _save_prediction_cache(self, result: dict[str, Any]) -> Path | None:
        try:
            PREDICTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            source = str(Path(self.data_path).resolve())
            model = str(result.get("model", self._configured_model_path()))

            def identity(value: str) -> str:
                path = Path(value)
                try:
                    stat = path.stat()
                    return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
                except OSError:
                    return value

            cache_token = "|".join(
                (
                    identity(source),
                    identity(model),
                    str(result.get("algorithm", "")),
                )
            )
            source_key = hashlib.sha256(cache_token.encode("utf-8")).hexdigest()[:12]
            output_path = (
                PREDICTION_CACHE_DIR
                / f"{Path(source).stem}.{source_key}.prediction_summary.json"
            )
            payload = dict(result)
            payload["annotation_source"] = source
            payload["cache_key"] = source_key
            temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(
                    json_safe(payload),
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            temp_path.replace(output_path)
            return output_path
        except Exception:
            self.statusBar().showMessage("预测完成，但预测摘要缓存写入失败", 5000)
            return None

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._prediction_is_running():
            QMessageBox.information(
                self,
                "模型预测仍在运行",
                "请等待本次模型预测完成后再关闭工具。",
            )
            event.ignore()
            return
        super().closeEvent(event)
