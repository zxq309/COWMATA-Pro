from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from widgets import SignalPlotWidget


class VideoSurface(QFrame):
    doubleClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoSurface")
        self.setMinimumSize(420, 260)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setStyleSheet(
            "#videoSurface{background:#05070a;border:1px solid #273448;"
            "border-radius:4px;}"
        )

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.doubleClicked.emit()
        event.accept()


class WindowUi:
    def setup_ui(self, window: QMainWindow) -> None:
        window.setWindowTitle("牛尾环九轴桌面标注工具")
        window.resize(1500, 920)

        toolbar = window.addToolBar("主工具")
        toolbar.setMovable(False)
        self.open_json_action = QAction("打开九轴 JSON", window)
        self.open_video_action = QAction("打开视频", window)
        self.load_project_action = QAction("载入工程", window)
        self.save_project_action = QAction("保存工程", window)
        self.export_action = QAction("导出 CSV", window)
        self.export_training_action = QAction("导出训练样本", window)
        self.export_boris_action = QAction("导出聚合 CSV", window)
        self.irr_action = QAction("对比两份标注", window)
        for action in (
            self.open_json_action,
            self.open_video_action,
            self.load_project_action,
            self.save_project_action,
            self.export_action,
            self.export_training_action,
            self.export_boris_action,
            self.irr_action,
        ):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" 标注者 "))
        self.annotator_edit = QLineEdit()
        self.annotator_edit.setPlaceholderText("姓名/工号")
        self.annotator_edit.setMaximumWidth(130)
        toolbar.addWidget(self.annotator_edit)
        toolbar.addWidget(QLabel(" 牛号 "))
        self.cow_id_edit = QLineEdit()
        self.cow_id_edit.setPlaceholderText("cow_id（完成后填写）")
        self.cow_id_edit.setMaximumWidth(150)
        toolbar.addWidget(self.cow_id_edit)
        toolbar.addWidget(QLabel(" 协议 "))
        self.protocol_edit = QLineEdit("v3")
        self.protocol_edit.setMaximumWidth(65)
        self.protocol_edit.setReadOnly(True)
        self.protocol_edit.setToolTip("新建项目为标注协议 v3；旧工程保留原协议版本")
        toolbar.addWidget(self.protocol_edit)
        toolbar.addWidget(QLabel(" 加速度 raw/"))
        self.acc_scale_combo = QComboBox()
        self.acc_scale_combo.addItems(["4096", "8192"])
        toolbar.addWidget(self.acc_scale_combo)

        central = QWidget()
        window.setCentralWidget(central)
        outer = QVBoxLayout(central)
        self.outer_layout = outer
        outer.setContentsMargins(7, 7, 7, 7)
        outer.setSpacing(6)

        self.top_splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self.top_splitter, 1)
        self._build_data_panel()
        self._build_video_panel()
        self.top_splitter.setSizes([980, 520])

        self.annotation_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.annotation_splitter.setMaximumHeight(300)
        outer.addWidget(self.annotation_splitter)
        self._build_label_panel()
        self._build_event_panel()
        self.annotation_splitter.setSizes([570, 930])

        window.setStatusBar(QStatusBar())
        window.statusBar().showMessage("就绪")
        window.setStyleSheet(
            """
            QMainWindow,QWidget{
                font-family:"Microsoft YaHei UI";
                font-size:9pt;
                color:#172033
            }
            QMainWindow{background:#eef2f7}
            QDialog,QMessageBox,QInputDialog,QProgressDialog{
                background:#eef2f7;
                color:#172033
            }
            QLabel{
                color:#172033;
                background:transparent
            }
            QGroupBox{
                font-weight:600;
                border:1px solid #cfd8e5;
                border-radius:5px;
                margin-top:8px;
                padding-top:7px;
                background:#fff
            }
            QGroupBox::title{
                subcontrol-origin:margin;
                left:8px;
                padding:0 4px
            }
            QPushButton{
                padding:5px 9px;
                border:1px solid #b9c5d4;
                border-radius:4px;
                background:#fff
            }
            QPushButton:hover{background:#edf4ff;border-color:#6b9ee8}
            QPushButton:disabled{color:#9aa6b4;background:#f4f6f8}
            QLineEdit,QComboBox,QAbstractSpinBox,QTextEdit,QPlainTextEdit,
            QTableWidget,QListWidget{
                background:#fff;
                color:#172033;
                border:1px solid #c5cfdb;
                border-radius:3px;
                padding:3px;
                selection-background-color:#2f7bd9;
                selection-color:#fff
            }
            QLineEdit:disabled,QComboBox:disabled,QAbstractSpinBox:disabled,
            QTextEdit:disabled,QPlainTextEdit:disabled{
                background:#e8edf3;
                color:#65758b;
                border-color:#d5dde8
            }
            QComboBox QAbstractItemView{
                background:#fff;
                color:#172033;
                border:1px solid #9eacbe;
                border-radius:2px;
                outline:0;
                padding:2px;
                selection-background-color:#2f7bd9;
                selection-color:#fff
            }
            QComboBox QAbstractItemView::item{
                min-height:24px;
                padding:3px 8px
            }
            QHeaderView::section,QTableCornerButton::section{
                background:#e8edf3;
                color:#172033;
                border:0;
                border-right:1px solid #c5cfdb;
                border-bottom:1px solid #c5cfdb;
                padding:4px
            }
            QMenu{
                background:#fff;
                color:#172033;
                border:1px solid #9eacbe;
                padding:3px
            }
            QMenu::item{padding:5px 22px 5px 9px}
            QMenu::item:selected{
                background:#2f7bd9;
                color:#fff
            }
            QToolTip{
                background:#172033;
                color:#fff;
                border:1px solid #445269;
                padding:3px
            }
            QToolBar{
                background:#f8fafc;
                border-bottom:1px solid #cfd8e5;
                spacing:4px;
                padding:4px
            }
            """
        )

    def _build_data_panel(self) -> None:
        panel = QWidget()
        self.data_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.data_title = QLabel("尚未打开九轴 JSON")
        self.data_title.setStyleSheet("font-weight:600;font-size:14px;")
        self.data_info = QLabel("真实数据")
        self.data_info.setStyleSheet("color:#65758b;")
        self.auto_y_check = QCheckBox("Y 自适应")
        self.auto_y_check.setChecked(True)
        header.addWidget(self.data_title, 1)
        header.addWidget(self.data_info)
        header.addWidget(self.auto_y_check)
        layout.addLayout(header)

        self.plot = SignalPlotWidget()
        layout.addWidget(self.plot, 1)

        nav = QHBoxLayout()
        self.prev_activity_btn = QPushButton("◀ 活动")
        self.next_activity_btn = QPushButton("活动 ▶")
        self.full_view_btn = QPushButton("显示全程")
        self.playhead_label = QLabel("00:00:00.000")
        nav.addWidget(self.prev_activity_btn)
        nav.addWidget(self.next_activity_btn)
        nav.addWidget(self.full_view_btn)
        nav.addStretch()
        nav.addWidget(QLabel("播放头"))
        nav.addWidget(self.playhead_label)
        layout.addLayout(nav)
        self.top_splitter.addWidget(panel)

    def _build_video_panel(self) -> None:
        panel = QWidget()
        self.video_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        title = QLabel("辅助视频 · 原生播放")
        self.video_title = title
        title.setStyleSheet("font-weight:600;font-size:14px;")
        self.video_status = QLabel("未打开视频")
        self.video_status.setStyleSheet("color:#65758b;")
        self.video_fullscreen_btn = QPushButton("⛶ 全屏")
        self.video_fullscreen_btn.setToolTip(
            "全屏查看视频细节（F11 或双击视频；Esc 退出）"
        )
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.video_status)
        header.addWidget(self.video_fullscreen_btn)
        layout.addLayout(header)

        self.video_surface = VideoSurface()
        layout.addWidget(self.video_surface, 1)

        controls = QHBoxLayout()
        self.play_btn = QPushButton("▶ 播放")
        self.prev_frame_btn = QPushButton("⟨ 帧")
        self.next_frame_btn = QPushButton("帧 ⟩")
        self.rate_combo = QComboBox()
        for value in (0.25, 0.5, 1.0, 2.0, 4.0):
            self.rate_combo.addItem(f"{value:g}×", value)
        self.rate_combo.setCurrentIndex(2)
        self.mute_check = QCheckBox("静音")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setMaximumWidth(90)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.prev_frame_btn)
        controls.addWidget(self.next_frame_btn)
        controls.addWidget(self.rate_combo)
        controls.addWidget(self.mute_check)
        controls.addWidget(self.volume_slider)
        layout.addLayout(controls)

        clocks = QHBoxLayout()
        self.video_clock_label = QLabel("视频 00:00:00.000")
        self.wall_clock_label = QLabel("画面时间 —")
        clocks.addWidget(self.video_clock_label)
        clocks.addStretch()
        clocks.addWidget(self.wall_clock_label)
        layout.addLayout(clocks)

        align_box = QGroupBox("视频与九轴对齐")
        self.video_align_box = align_box
        align_layout = QVBoxLayout(align_box)
        row = QHBoxLayout()
        self.pin_btn = QPushButton("钉住 当前帧=播放头")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip(
            "未钉住时九轴与视频可独立调整；钉住后两条时间轴双向联动"
        )
        self.filename_btn = QPushButton("按文件名")
        self.corner_time_edit = QLineEdit()
        self.corner_time_edit.setPlaceholderText("HH:MM:SS.mmm")
        self.corner_btn = QPushButton("设为当前帧")
        row.addWidget(self.pin_btn)
        row.addWidget(self.filename_btn)
        row.addWidget(self.corner_time_edit)
        row.addWidget(self.corner_btn)
        align_layout.addLayout(row)

        nudge = QHBoxLayout()
        nudge.addWidget(QLabel("微调"))
        self.nudge_buttons: list[tuple[QPushButton, float]] = []
        for value in (-10, -1, -0.1, 0.1, 1, 10):
            button = QPushButton(f"{value:+g}s")
            self.nudge_buttons.append((button, value * 1000))
            nudge.addWidget(button)
        self.align_status = QLabel("尚未对齐")
        self.align_status.setStyleSheet("color:#b42318;")
        nudge.addWidget(self.align_status, 1)
        align_layout.addLayout(nudge)
        layout.addWidget(align_box)
        self.video_fullscreen_hidden_widgets = (
            self.play_btn,
            self.prev_frame_btn,
            self.next_frame_btn,
            self.rate_combo,
            self.mute_check,
            self.volume_slider,
            self.video_clock_label,
            self.wall_clock_label,
            self.video_align_box,
        )
        self.top_splitter.addWidget(panel)

    def _build_label_panel(self) -> None:
        box = QGroupBox("标签 · 在曲线上拖动创建区间")
        layout = QVBoxLayout(box)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("正式标签 11 项"))
        filter_row.addStretch()
        filter_row.addWidget(QLabel("0：同步敲击"))
        layout.addLayout(filter_row)
        self.label_list = QListWidget()
        self.label_list.setFlow(QListWidget.Flow.LeftToRight)
        self.label_list.setWrapping(True)
        self.label_list.setMaximumHeight(100)
        layout.addWidget(self.label_list)

        buttons = QHBoxLayout()
        metadata = QHBoxLayout()
        self.add_label_btn = QPushButton("＋ 新标签")
        self.edit_label_btn = QPushButton("编辑")
        self.delete_label_btn = QPushButton("删除")
        self.evidence_combo = QComboBox()
        self.evidence_combo.addItem("两者皆可", "both")
        self.evidence_combo.addItem("仅视频", "video")
        self.evidence_combo.addItem("仅曲线", "curve")
        self.context_combo = QComboBox()
        self.context_combo.addItem("（无情境）", "")
        self.context_combo.setEnabled(False)
        self.context_combo.setVisible(False)
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("事件备注（可选）")
        buttons.addWidget(self.add_label_btn)
        buttons.addWidget(self.edit_label_btn)
        buttons.addWidget(self.delete_label_btn)
        buttons.addStretch()
        buttons.addWidget(self.evidence_combo)
        layout.addLayout(buttons)
        metadata.addWidget(QLabel("备注"))
        metadata.addWidget(self.reason_edit, 1)
        layout.addLayout(metadata)
        self.label_definition = QLabel("")
        self.label_definition.setWordWrap(True)
        self.label_definition.setStyleSheet("color:#58677b;")
        layout.addWidget(self.label_definition)
        self.annotation_splitter.addWidget(box)

    def _build_event_panel(self) -> None:
        box = QGroupBox("标注事件")
        layout = QVBoxLayout(box)
        self.event_table = QTableWidget(0, 7)
        self.event_table.setHorizontalHeaderLabels(
            [
                "#", "层", "标签", "开始记录时间",
                "结束记录时间", "时长", "备注",
            ]
        )
        self.event_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.event_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.event_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.event_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.event_table)
        row = QHBoxLayout()
        self.delete_event_btn = QPushButton("删除选中")
        self.clear_event_btn = QPushButton("清空")
        self.event_count_label = QLabel("0 条")
        row.addWidget(self.delete_event_btn)
        row.addWidget(self.clear_event_btn)
        row.addStretch()
        row.addWidget(self.event_count_label)
        layout.addLayout(row)
        self.annotation_splitter.addWidget(box)
