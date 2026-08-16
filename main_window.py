from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QTableWidgetItem,
    QToolBar,
)

from defaults import DEFAULT_LABELS, DEFAULT_PROTOCOL
from media_engine import MediaEngine, MediaEngineError
from ui_helpers import (
    BEIJING,
    LabelDialog,
    as_dict,
    format_relative,
    format_wall,
    parse_video_filename,
)
from widgets import PlotSeries
from window_ui import WindowUi

try:
    import annotation_core
except ImportError:
    annotation_core = None

try:
    import data_core
except ImportError:
    data_core = None


CHANNEL_STYLE = {
    "ax": ("加速度 X", "m/s²", "#d9534f"),
    "ay": ("加速度 Y", "m/s²", "#299764"),
    "az": ("加速度 Z", "m/s²", "#3979bd"),
    "gx": ("角速度 X", "deg/s", "#d1782f"),
    "gy": ("角速度 Y", "deg/s", "#2b9a91"),
    "gz": ("角速度 Z", "deg/s", "#8462ad"),
    "mx": ("磁场 X", "Gauss", "#c14d79"),
    "my": ("磁场 Y", "Gauss", "#648a35"),
    "mz": ("磁场 Z", "Gauss", "#3181ac"),
    "temperature": ("温度", "°C", "#c46a2c"),
    "motion": ("运动", "次/分钟", "#334155"),
}

CHANNEL_ALIASES = {
    "acc_x": "ax",
    "acc_y": "ay",
    "acc_z": "az",
    "gyro_x": "gx",
    "gyro_y": "gy",
    "gyro_z": "gz",
    "mag_x": "mx",
    "mag_y": "my",
    "mag_z": "mz",
    "temp": "temperature",
}

VIDEO_FILE_SUFFIXES = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".ts",
        ".m2ts",
        ".ps",
        ".mpeg",
        ".mpg",
        ".h265",
        ".hevc",
    }
)


def _natural_file_sort_key(
    path: Path,
) -> tuple[tuple[tuple[int, object], ...], str]:
    """Sort numbered file names in human order (2 before 10)."""

    folded = path.name.casefold()
    parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", folded)
        if part
    )
    return parts, folded


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.ui = WindowUi()
        self.ui.setup_ui(self)
        # Expose UI widgets directly to keep handlers readable.
        self.__dict__.update(self.ui.__dict__)

        self.settings = QSettings(
            "BovineMotionWorkbench", "牛尾环九轴桌面标注工具"
        )
        self.data: Any | None = None
        self.data_path = ""
        self.video_path = ""
        self._video_fullscreen_active = False
        self._video_fullscreen_restore: dict[str, Any] | None = None
        self.video_start_wall_ms: int | None = None
        self.align_method = "none"
        self.playhead_ms = 0.0
        self.labels = [dict(item) for item in DEFAULT_LABELS]
        self.events: list[dict[str, Any]] = []
        self.cow_id = ""
        self.selected_label = 0
        self.selected_event_id: int | None = None
        self.next_event_id = 1
        self.pending_intervals: dict[int, float] = {}
        self._shortcut_objects: list[QShortcut] = []
        self._autopause_on_load = False
        self._restoring_project = False
        self._project_path = ""

        self.media: MediaEngine | None = None
        if os.environ.get("BOVINE_NO_MEDIA") != "1":
            try:
                self.media = MediaEngine(self.video_surface, parent=self)
            except MediaEngineError as exc:
                QTimer.singleShot(
                    0, lambda: self._show_error(f"原生播放器初始化失败：\n{exc}")
                )
            else:
                self.media.status_changed.connect(self._on_media_status)
                self.media.error_occurred.connect(self._show_error)
                self.media.time_changed.connect(self._on_media_time)
                self.media.duration_changed.connect(self._on_media_duration)
                self.media.playing_changed.connect(self._on_playing_changed)
                self.media.ended.connect(self._on_media_ended)

        self._connect_actions()
        self._rebuild_shortcuts()
        self._install_video_fullscreen_shortcut()
        self._refresh_labels()
        self._refresh_events()
        self._refresh_enabled()

        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(30_000)
        self.autosave_timer.timeout.connect(self._autosave)
        self.autosave_timer.start()

    # ------------------------------------------------------------------
    # Connections and common state
    # ------------------------------------------------------------------

    def _connect_actions(self) -> None:
        self.open_json_action.triggered.connect(self.open_json)
        self.open_video_action.triggered.connect(self.open_video)
        self.load_project_action.triggered.connect(self.load_project_dialog)
        self.save_project_action.triggered.connect(self.save_project_dialog)
        self.export_action.triggered.connect(self.export_event_csv)
        self.export_training_action.triggered.connect(self.export_training_csv)
        self.export_boris_action.triggered.connect(self.export_boris_csv)
        self.irr_action.triggered.connect(self.compare_irr)
        self.acc_scale_combo.currentTextChanged.connect(self._reload_scale)
        self.auto_y_check.toggled.connect(self.plot.set_auto_y)

        self.plot.seekRequested.connect(self.set_playhead)
        self.plot.rangeSelected.connect(self._range_selected)
        self.previous_json_btn.clicked.connect(self.open_previous_json)
        self.next_json_btn.clicked.connect(self.open_next_json)
        self.prev_activity_btn.clicked.connect(
            lambda: self._jump_activity(-1)
        )
        self.next_activity_btn.clicked.connect(
            lambda: self._jump_activity(1)
        )
        self.full_view_btn.clicked.connect(self.plot.show_all)

        self.play_btn.clicked.connect(self.toggle_play)
        self.previous_video_btn.clicked.connect(self.open_previous_video)
        self.next_video_btn.clicked.connect(self.open_next_video)
        self.prev_frame_btn.clicked.connect(self.previous_frame)
        self.next_frame_btn.clicked.connect(self.next_frame)
        self.rate_combo.currentIndexChanged.connect(self._apply_rate)
        self.mute_check.toggled.connect(self._apply_mute)
        self.volume_slider.valueChanged.connect(self._apply_volume)
        self.video_fullscreen_btn.clicked.connect(self.toggle_video_fullscreen)
        self.video_surface.doubleClicked.connect(self.toggle_video_fullscreen)

        self.pin_btn.clicked.connect(self.pin_alignment)
        self.filename_btn.clicked.connect(self.align_from_filename)
        self.corner_btn.clicked.connect(self.align_from_corner)
        for button, delta in self.nudge_buttons:
            button.clicked.connect(
                lambda _checked=False, value=delta: self.nudge_alignment(value)
            )

        self.label_list.currentRowChanged.connect(self._select_visible_label)
        self.label_list.itemDoubleClicked.connect(lambda _item: self.edit_label())
        self.add_label_btn.clicked.connect(self.add_label)
        self.edit_label_btn.clicked.connect(self.edit_label)
        self.delete_label_btn.clicked.connect(self.delete_label)
        self.evidence_combo.currentIndexChanged.connect(
            self._annotation_meta_changed
        )
        self.context_combo.currentIndexChanged.connect(
            self._annotation_meta_changed
        )
        self.reason_edit.editingFinished.connect(self._annotation_meta_changed)
        self.event_table.itemSelectionChanged.connect(self._event_selected)
        self.event_table.cellDoubleClicked.connect(
            self._event_double_clicked
        )
        self.delete_event_btn.clicked.connect(self.delete_selected_event)
        self.clear_event_btn.clicked.connect(self.clear_events)

    @property
    def data_duration_ms(self) -> float:
        if self.data is None:
            return 0.0
        return float(
            _value(
                self.data,
                "duration_ms",
                "tspan",
                "duration",
                default=0.0,
            )
            or 0.0
        )

    @property
    def data_create_time_ms(self) -> int:
        if self.data is None:
            return 0
        return int(
            _value(
                self.data,
                "create_time_ms",
                "create_time",
                "createTime",
                default=0,
            )
            or 0
        )

    @property
    def data_device(self) -> str:
        if self.data is None:
            return ""
        return str(
            _value(
                self.data,
                "device",
                "device_id",
                "uid",
                default="",
            )
            or ""
        )

    def _refresh_enabled(self) -> None:
        has_data = self.data is not None
        has_video = self.media is not None and bool(self.video_path)
        for widget in (
            self.play_btn,
            self.prev_frame_btn,
            self.next_frame_btn,
            self.rate_combo,
            self.mute_check,
            self.volume_slider,
            self.video_fullscreen_btn,
        ):
            widget.setEnabled(has_video)
        previous_video, next_video = self._neighboring_video_paths()
        self.previous_video_btn.setEnabled(
            has_video and previous_video is not None
        )
        self.next_video_btn.setEnabled(has_video and next_video is not None)
        previous_json, next_json = self._neighboring_json_paths()
        self.previous_json_btn.setEnabled(
            has_data and previous_json is not None
        )
        self.next_json_btn.setEnabled(has_data and next_json is not None)
        for widget in (
            self.pin_btn,
            self.filename_btn,
            self.corner_btn,
            self.corner_time_edit,
        ):
            widget.setEnabled(has_video and has_data)
        self.prev_activity_btn.setEnabled(has_data)
        self.next_activity_btn.setEnabled(has_data)
        self.full_view_btn.setEnabled(has_data)
        self.export_action.setEnabled(has_data and bool(self.events))
        self.export_training_action.setEnabled(has_data and bool(self.events))
        self.export_boris_action.setEnabled(has_data and bool(self.events))
        self.statusBar().showMessage(
            f"数据：{'已载入' if has_data else '未载入'}　"
            f"视频：{'已载入' if has_video else '未载入'}"
        )

    def _files_in_current_directory(
        self,
        current_path: str,
        suffixes: frozenset[str],
    ) -> list[Path]:
        """Return matching sibling files without parsing their contents."""

        if not current_path:
            return []
        folder = Path(current_path).parent
        try:
            files = [
                candidate
                for candidate in folder.iterdir()
                if candidate.is_file()
                and candidate.suffix.casefold() in suffixes
            ]
        except OSError:
            return []
        return sorted(files, key=_natural_file_sort_key)

    def _videos_in_current_directory(self) -> list[Path]:
        """Return sibling videos without probing or indexing media."""

        return self._files_in_current_directory(
            self.video_path,
            VIDEO_FILE_SUFFIXES,
        )

    def _json_files_in_current_directory(self) -> list[Path]:
        return self._files_in_current_directory(
            self.data_path,
            frozenset({".json"}),
        )

    @staticmethod
    def _normalized_path_key(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _neighboring_file_paths(
        self,
        current_path: str,
        files: list[Path],
    ) -> tuple[Path | None, Path | None]:
        if not current_path:
            return None, None
        current_key = self._normalized_path_key(current_path)
        current_index = next(
            (
                index
                for index, candidate in enumerate(files)
                if self._normalized_path_key(candidate) == current_key
            ),
            None,
        )
        if current_index is None:
            return None, None
        previous_file = files[current_index - 1] if current_index > 0 else None
        next_file = (
            files[current_index + 1]
            if current_index + 1 < len(files)
            else None
        )
        return previous_file, next_file

    def _neighboring_video_paths(self) -> tuple[Path | None, Path | None]:
        return self._neighboring_file_paths(
            self.video_path,
            self._videos_in_current_directory(),
        )

    def _neighboring_json_paths(self) -> tuple[Path | None, Path | None]:
        return self._neighboring_file_paths(
            self.data_path,
            self._json_files_in_current_directory(),
        )

    def _open_neighboring_video(self, direction: int) -> None:
        if getattr(self, "_source_switch_active", False):
            return
        previous_video, next_video = self._neighboring_video_paths()
        target = previous_video if direction < 0 else next_video
        if target is None:
            boundary = "上一个" if direction < 0 else "下一个"
            self._refresh_enabled()
            self.statusBar().showMessage(f"当前没有{boundary}视频", 3000)
            return
        self.open_video(str(target))

    def open_previous_video(self, _checked: bool = False) -> None:
        self._open_neighboring_video(-1)

    def open_next_video(self, _checked: bool = False) -> None:
        self._open_neighboring_video(1)

    def _open_neighboring_json(self, direction: int) -> None:
        if getattr(self, "_source_switch_active", False):
            return
        previous_json, next_json = self._neighboring_json_paths()
        target = previous_json if direction < 0 else next_json
        if target is None:
            boundary = "上一个" if direction < 0 else "下一个"
            self._refresh_enabled()
            self.statusBar().showMessage(f"当前没有{boundary} JSON", 3000)
            return
        self.open_json(str(target))

    def open_previous_json(self, _checked: bool = False) -> None:
        self._open_neighboring_json(-1)

    def open_next_json(self, _checked: bool = False) -> None:
        self._open_neighboring_json(1)

    def _install_video_fullscreen_shortcut(self) -> None:
        self._video_fullscreen_shortcut = QShortcut(QKeySequence("F11"), self)
        self._video_fullscreen_shortcut.setContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self._video_fullscreen_shortcut.activated.connect(
            self.toggle_video_fullscreen
        )
        self._video_fullscreen_escape_shortcut = QShortcut(
            QKeySequence("Esc"), self
        )
        self._video_fullscreen_escape_shortcut.setContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self._video_fullscreen_escape_shortcut.activated.connect(
            self.exit_video_fullscreen
        )
        self._video_fullscreen_escape_shortcut.setEnabled(False)

    def toggle_video_fullscreen(self, _checked: bool = False) -> None:
        if self._video_fullscreen_active:
            self.exit_video_fullscreen()
        else:
            self.enter_video_fullscreen()

    def enter_video_fullscreen(self) -> None:
        if self.media is None or not self.video_path:
            self.statusBar().showMessage("请先打开视频后再进入全屏", 3000)
            return
        if self._video_fullscreen_active:
            return
        toolbars = self.findChildren(QToolBar)
        hidden_widgets = (
            self.data_panel,
            getattr(self, "annotation_container", self.annotation_splitter),
            *self.video_fullscreen_hidden_widgets,
        )
        self._video_fullscreen_restore = {
            "geometry": self.saveGeometry(),
            "maximized": self.isMaximized(),
            "toolbars": [(widget, widget.isVisible()) for widget in toolbars],
            "status_visible": self.statusBar().isVisible(),
            "widgets": [
                (widget, widget.isVisible()) for widget in hidden_widgets
            ],
            "splitter_sizes": self.top_splitter.sizes(),
            "splitter_handle_width": self.top_splitter.handleWidth(),
            "outer_margins": self.outer_layout.getContentsMargins(),
        }
        for toolbar in toolbars:
            toolbar.hide()
        self.statusBar().hide()
        for widget in hidden_widgets:
            widget.hide()
        self.outer_layout.setContentsMargins(0, 0, 0, 0)
        self.top_splitter.setHandleWidth(0)
        self.top_splitter.setSizes([0, 1])
        self._video_fullscreen_active = True
        self._video_fullscreen_escape_shortcut.setEnabled(True)
        self.video_fullscreen_btn.setText("退出全屏")
        self.video_status.setText("双击 / Esc / F11 退出全屏")
        self.showFullScreen()
        self.video_surface.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        QApplication.processEvents()

    def exit_video_fullscreen(self) -> None:
        if not self._video_fullscreen_active:
            return
        restore = self._video_fullscreen_restore or {}
        self._video_fullscreen_active = False
        self._video_fullscreen_restore = None
        self._video_fullscreen_escape_shortcut.setEnabled(False)
        self.showNormal()
        geometry = restore.get("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if restore.get("maximized"):
            self.showMaximized()
        margins = restore.get("outer_margins")
        if margins is not None:
            self.outer_layout.setContentsMargins(*margins)
        self.top_splitter.setHandleWidth(
            int(restore.get("splitter_handle_width", 4))
        )
        sizes = restore.get("splitter_sizes")
        if sizes:
            self.top_splitter.setSizes(sizes)
        for widget, visible in restore.get("widgets", []):
            widget.setVisible(bool(visible))
        for toolbar, visible in restore.get("toolbars", []):
            toolbar.setVisible(bool(visible))
        self.statusBar().setVisible(bool(restore.get("status_visible", True)))
        self.video_fullscreen_btn.setText("⛶ 全屏")
        self._on_media_status(
            self.media.current_status if self.media is not None else "ready"
        )
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------
    # Real JSON data
    # ------------------------------------------------------------------

    def open_json(self, path: str | None = None) -> None:
        if path is None or isinstance(path, bool):
            start = self.settings.value(
                "last_data_dir",
                str(Path.cwd() / "九轴JSON数据在这里"),
            )
            path, _ = QFileDialog.getOpenFileName(
                self,
                "打开真实九轴 JSON",
                str(start),
                "JSON 文件 (*.json);;所有文件 (*.*)",
            )
        if not path:
            return
        if self.events and not self._restoring_project:
            answer = QMessageBox.question(
                self,
                "切换数据",
                "打开新数据会清空当前标注，是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if data_core is None:
            self._show_error("data_core.py 尚未就绪。")
            return
        loader = getattr(data_core, "load_motion_json", None) or getattr(
            data_core, "load_json", None
        )
        if loader is None:
            self._show_error("data_core.py 缺少 load_motion_json。")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            parsed = loader(path, acc_scale=int(self.acc_scale_combo.currentText()))
        except TypeError:
            parsed = loader(path, int(self.acc_scale_combo.currentText()))
        except Exception as exc:
            self._show_error(f"九轴 JSON 解析失败：\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.data = parsed
        self.data_path = os.path.abspath(path)
        self.settings.setValue("last_data_dir", str(Path(path).parent))
        if not self._restoring_project:
            self.labels = [dict(item) for item in DEFAULT_LABELS]
            self.events.clear()
            self.next_event_id = 1
            self.selected_label = 0
            self.pending_intervals.clear()
            self.protocol_edit.setText(DEFAULT_PROTOCOL)
            self._refresh_labels()
            self._rebuild_shortcuts()
        self._display_motion_data()
        if self.video_path and self.video_start_wall_ms is None:
            self.align_from_filename(silent=True)
        self._refresh_events()
        self._refresh_enabled()

    def _reload_scale(self, _text: str) -> None:
        if self.data_path and not self._restoring_project:
            self.open_json(self.data_path)

    def _display_motion_data(self) -> None:
        assert self.data is not None
        plot_series = self._make_plot_series(self.data)
        gap_threshold = float(
            _value(
                self.data,
                "gap_threshold_ms",
                default=100.0,
            )
            or 100.0
        )
        self.plot.set_data(
            plot_series,
            self.data_duration_ms,
            gap_threshold_ms=gap_threshold,
        )
        self.plot.set_events(self.labels, self.events)
        name = Path(self.data_path).stem
        self.data_title.setText(
            f"{self.data_device or '未知设备'} / {name}"
        )
        sample_count = int(
            _value(
                self.data,
                "sample_count",
                "samples",
                default=len(plot_series[0].values) if plot_series else 0,
            )
            or 0
        )
        sample_rate = float(
            _value(
                self.data,
                "sample_rate_hz",
                "sample_rate",
                "fs",
                default=0.0,
            )
            or 0.0
        )
        gaps = int(
            _value(self.data, "gap_count", "drop_count", default=0) or 0
        )
        self.data_info.setText(
            f"{sample_count:,} 样本 · {sample_rate:.2f} Hz · "
            f"{format_relative(self.data_duration_ms)} · 间隙 {gaps}"
        )
        self.set_playhead(0.0, seek_video=False)

    def _make_plot_series(self, data: Any) -> list[PlotSeries]:
        if hasattr(data, "plot_series"):
            supplied = data.plot_series()
            converted: list[PlotSeries] = []
            for item in supplied:
                if isinstance(item, PlotSeries):
                    converted.append(item)
                else:
                    info = as_dict(item)
                    converted.append(
                        PlotSeries(
                            key=str(info["key"]),
                            name=str(info["name"]),
                            unit=str(info.get("unit", "")),
                            color=str(info.get("color", "#4f8cff")),
                            times_ms=np.asarray(info["times_ms"], dtype=float),
                            values=np.asarray(info["values"], dtype=float),
                        )
                    )
            if converted:
                return converted

        channels = _value(data, "channels", default={}) or {}
        times = np.asarray(
            _value(data, "times_ms", "time_ms", "t", default=[]),
            dtype=float,
        )
        result: list[PlotSeries] = []
        for raw_key, raw_values in channels.items():
            key = CHANNEL_ALIASES.get(str(raw_key), str(raw_key))
            if key not in CHANNEL_STYLE:
                continue
            name, unit, color = CHANNEL_STYLE[key]
            values = np.asarray(raw_values, dtype=float)
            channel_times = times
            if key == "temperature":
                channel_times = np.asarray(
                    _value(
                        data,
                        "temperature_times_ms",
                        "temp_times_ms",
                        default=np.linspace(
                            0, self.data_duration_ms, values.size
                        ),
                    ),
                    dtype=float,
                )
            elif key == "motion":
                channel_times = np.asarray(
                    _value(
                        data,
                        "motion_times_ms",
                        default=np.linspace(
                            0, self.data_duration_ms, values.size
                        ),
                    ),
                    dtype=float,
                )
            result.append(
                PlotSeries(
                    key,
                    name,
                    unit,
                    color,
                    channel_times,
                    values,
                )
            )
        order = {key: i for i, key in enumerate(CHANNEL_STYLE)}
        result.sort(key=lambda item: order.get(item.key, 999))
        return result

