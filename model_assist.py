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
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
)

from ui_helpers import format_relative


APP_DIR = Path(__file__).resolve().parent
MODEL_RUNTIME_DIR = APP_DIR / "model_runtime"
DEFAULT_MODEL_PATH = APP_DIR / "models" / "production" / "best_model.pt"
PREDICTION_CACHE_DIR = APP_DIR / "prediction_cache"

BODY_CODES = {"LYING", "STANDING", "WALKING"}
RESEARCH_MODEL_CODES = {"DEFECATION", "TAIL_RAISED", "TAIL_WAGGING"}
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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.predictions = predictions
        self.locate_callback = locate_callback
        self.setWindowTitle("复核模型建议")
        self.resize(1180, 700)

        layout = QVBoxLayout(self)
        summary = QLabel(
            "模型结果只作为建议。勾选并点击“导入选中建议”后才会进入标注表；"
            "导入项会标记为“待人工复核”。双击任意行可定位到建议开始时间。"
            "点击“平均置信度”或“最高置信度”表头可切换升序/降序。"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        warning_text = (
            "v4 已删除采食和其他标签：历史采食证据归入直立；站/卧由已知初始站立状态和状态机生成，"
            "行走作为直立子状态叠加。排便、抬尾、甩尾仍是研究性候选，默认不勾选；"
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
        recommended_btn.clicked.connect(self._select_recommended)
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        locate_btn.clicked.connect(self._locate_selected)
        for button in (recommended_btn, all_btn, none_btn, locate_btn):
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
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        toolbar = QToolBar("模型辅助", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self.model_assist_toolbar = toolbar
        self.model_predict_action = QAction("模型辅助预测", self)
        self.model_predict_action.setToolTip(
            "对当前九轴 JSON 运行正式模型，复核后选择性导入建议"
        )
        self.model_select_action = QAction("选择模型…", self)
        self.model_select_action.setToolTip("选择另一份兼容的 best_model.pt")
        self.model_mark_reviewed_action = QAction("确认建议已复核", self)
        self.model_mark_reviewed_action.setToolTip(
            "把事件表中选中的待复核模型建议标记为已人工复核"
        )
        toolbar.addAction(self.model_predict_action)
        toolbar.addAction(self.model_mark_reviewed_action)
        toolbar.addAction(self.model_select_action)
        self.model_predict_action.triggered.connect(self.run_model_prediction)
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
        if action is None:
            return
        event = self._selected_model_event()
        action.setEnabled(
            event is not None
            and event.get("prediction_review_status") == "pending"
        )

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
        if configured and Path(configured).is_file():
            return Path(configured)
        return DEFAULT_MODEL_PATH

    def choose_prediction_model(self, _checked: bool = False) -> Path | None:
        current = self._configured_model_path()
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择兼容的模型权重",
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
                    "请选择 models_codex\\loco_v1、"
                    "models_codex\\loco_v2_domain_robust 下的 best.pt，"
                    "或新版 CausalMultiTaskTCN checkpoint。"
                )
                return None
            supported = {
                "HierarchicalMSResTCN",
                "MultiTaskResTCN",
                "CausalMultiTaskTCN",
            }
            if model_class not in supported:
                self._show_error(
                    f"不支持的模型类型：{model_class or '无法识别'}\n{path}"
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
            [str(value) for value in result.get("warnings", [])],
            self,
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
                    f"{support_note}"
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
