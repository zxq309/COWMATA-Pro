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
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from widgets import SignalPlotWidget


class VideoSurface(QFrame):
    doubleClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoSurface")
        self.setMinimumSize(480, 300)
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
        window.resize(1640, 960)
        window.setMinimumSize(1180, 760)

        toolbar = window.addToolBar("主工具")
        toolbar.setMovable(False)
        self.main_toolbar = toolbar
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
        ):
            toolbar.addAction(action)
        toolbar.addSeparator()
        self.export_tools_button = QToolButton()
        self.export_tools_button.setText("导出与工具")
        self.export_tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.export_tools_menu = QMenu(self.export_tools_button)
        self.export_tools_menu.addAction(self.export_action)
        self.export_tools_menu.addAction(self.export_training_action)
        self.export_tools_menu.addAction(self.export_boris_action)
        self.export_tools_menu.addSeparator()
        self.export_tools_menu.addAction(self.irr_action)
        self.export_tools_button.setMenu(self.export_tools_menu)
        toolbar.addWidget(self.export_tools_button)
        toolbar.addSeparator()
        toolbar_spacer = QWidget()
        toolbar_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.toolbar_spacer_action = toolbar.addWidget(toolbar_spacer)
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
        outer.setContentsMargins(6, 5, 6, 5)
        outer.setSpacing(5)

        self.top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.top_splitter.setHandleWidth(5)
        self.top_splitter.setChildrenCollapsible(False)
        outer.addWidget(self.top_splitter, 1)
        self._build_data_panel()
        self._build_video_panel()
        self.top_splitter.setStretchFactor(0, 1)
        self.top_splitter.setStretchFactor(1, 1)
        self.top_splitter.setSizes([820, 820])

        self.annotation_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.annotation_splitter.setHandleWidth(5)
        self.annotation_splitter.setChildrenCollapsible(False)
        annotation_container = QWidget()
        annotation_layout = QHBoxLayout(annotation_container)
        annotation_layout.setContentsMargins(0, 0, 0, 0)
        annotation_layout.addWidget(self.annotation_splitter)
        annotation_container.setFixedHeight(218)
        self.annotation_container = annotation_container
        outer.addWidget(annotation_container)
        self._build_label_panel()
        self._build_event_panel()
        self.annotation_splitter.setStretchFactor(0, 4)
        self.annotation_splitter.setStretchFactor(1, 6)
        self.annotation_splitter.setSizes([650, 990])

        window.setStatusBar(QStatusBar())
        window.statusBar().showMessage("就绪")
        window.setStyleSheet(
            """
            QMainWindow,QWidget{
                font-family:"Microsoft YaHei UI";
                font-size:9pt;
                color:#172033
            }
            QMainWindow{background:#f1f4f8}
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
                border:1px solid #d6dde8;
                border-radius:7px;
                margin-top:7px;
                padding-top:6px;
                background:#fff
            }
            QGroupBox::title{
                subcontrol-origin:margin;
                left:8px;
                padding:0 4px
            }
            QPushButton,QToolButton{
                padding:4px 8px;
                border:1px solid #c5cfdb;
                border-radius:5px;
                background:#fff
            }
            QPushButton:hover,QToolButton:hover{
                background:#edf4ff;
                border-color:#6b9ee8
            }
            QPushButton:disabled,QToolButton:disabled{
                color:#9aa6b4;
                background:#f4f6f8
            }
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
                spacing:3px;
                padding:3px 5px
            }
            QSplitter::handle{
                background:#e3e8f0
            }
            QSplitter::handle:hover{
                background:#9fb7d7
            }
            #dataPanel,#videoPanel{
                background:#fff;
                border:1px solid #d8e0ea;
                border-radius:7px
            }
            #compactBar,#videoControlBar{
                background:#f7f9fc;
                border:1px solid #e0e6ef;
                border-radius:5px
            }
            #alignmentPanel{
                background:#f8fafc;
                border:1px solid #dbe3ee;
                border-radius:5px
            }
            QTableWidget{
                gridline-color:#e6ebf2;
                alternate-background-color:#f8fafc
            }
            """
        )

    def _build_data_panel(self) -> None:
        panel = QWidget()
        panel.setObjectName("dataPanel")
        self.data_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(7)
        self.data_title = QLabel("尚未打开九轴 JSON")
        self.data_title.setStyleSheet("font-weight:650;font-size:13px;")
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

        nav_bar = QWidget()
        nav_bar.setObjectName("compactBar")
        nav = QHBoxLayout(nav_bar)
        nav.setContentsMargins(5, 3, 5, 3)
        nav.setSpacing(5)
        self.data_nav_bar = nav_bar
        self.data_nav_layout = nav
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
        layout.addWidget(nav_bar)
        self.top_splitter.addWidget(panel)

    def _build_video_panel(self) -> None:
        panel = QWidget()
        panel.setObjectName("videoPanel")
        self.video_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(7)
        title = QLabel("辅助视频 · 原生播放")
        self.video_title = title
        title.setStyleSheet("font-weight:650;font-size:13px;")
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

        control_bar = QWidget()
        control_bar.setObjectName("videoControlBar")
        controls = QHBoxLayout(control_bar)
        controls.setContentsMargins(5, 3, 5, 3)
        controls.setSpacing(5)
        self.video_control_bar = control_bar
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
        self.video_clock_label = QLabel("视频 00:00:00.000")
        self.wall_clock_label = QLabel("画面时间 —")
        self.video_clock_label.setStyleSheet("color:#475569;")
        self.wall_clock_label.setStyleSheet("color:#64748b;")
        controls.addStretch()
        controls.addWidget(self.video_clock_label)
        controls.addWidget(self.wall_clock_label)
        layout.addWidget(control_bar)

        align_box = QFrame()
        align_box.setObjectName("alignmentPanel")
        self.video_align_box = align_box
        align_layout = QVBoxLayout(align_box)
        align_layout.setContentsMargins(6, 4, 6, 4)
        align_layout.setSpacing(4)

        primary = QHBoxLayout()
        primary.setSpacing(6)
        self.pin_btn = QPushButton("钉住 当前帧=播放头")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip(
            "未钉住时九轴与视频可独立调整；钉住后两条时间轴双向联动"
        )
        self.align_status = QLabel("尚未对齐")
        self.align_status.setStyleSheet("color:#b42318;font-weight:600;")
        self.alignment_toggle_btn = QToolButton()
        self.alignment_toggle_btn.setText("校准设置")
        self.alignment_toggle_btn.setCheckable(True)
        self.alignment_toggle_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.alignment_toggle_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.alignment_toggle_btn.setToolTip(
            "展开角标时间、微调和视频维护工具"
        )
        primary.addWidget(self.pin_btn)
        primary.addWidget(self.align_status)
        primary.addStretch()
        primary.addWidget(self.alignment_toggle_btn)
        align_layout.addLayout(primary)

        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 2, 0, 0)
        advanced_layout.setSpacing(4)
        self.alignment_advanced = advanced

        source_row = QHBoxLayout()
        source_row.setSpacing(5)
        source_row.addWidget(QLabel("时间校准"))
        self.filename_btn = QPushButton("按文件名")
        self.corner_time_edit = QLineEdit()
        self.corner_time_edit.setPlaceholderText("HH:MM:SS.mmm")
        self.corner_time_edit.setMaximumWidth(145)
        self.corner_btn = QPushButton("设为当前帧")
        source_row.addWidget(self.filename_btn)
        source_row.addWidget(self.corner_time_edit)
        source_row.addWidget(self.corner_btn)
        source_row.addStretch()
        advanced_layout.addLayout(source_row)

        nudge = QHBoxLayout()
        nudge.setSpacing(4)
        nudge.addWidget(QLabel("微调"))
        self.nudge_buttons: list[tuple[QPushButton, float]] = []
        for value in (-10, -1, -0.1, 0.1, 1, 10):
            button = QPushButton(f"{value:+g}s")
            self.nudge_buttons.append((button, value * 1000))
            nudge.addWidget(button)
        nudge.addStretch()
        advanced_layout.addLayout(nudge)
        self.video_utility_layout = QHBoxLayout()
        self.video_utility_layout.setSpacing(4)
        self.video_utility_layout.addStretch()
        advanced_layout.addLayout(self.video_utility_layout)
        advanced.setVisible(False)
        align_layout.addWidget(advanced)
        self.alignment_toggle_btn.toggled.connect(
            self._toggle_alignment_advanced
        )
        layout.addWidget(align_box)
        self.video_fullscreen_hidden_widgets = (
            self.video_control_bar,
            self.video_align_box,
        )
        self.top_splitter.addWidget(panel)

    def _build_label_panel(self) -> None:
        box = QGroupBox("标签与当前事件")
        self.label_panel = box
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 7, 8, 6)
        layout.setSpacing(4)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("快捷键选择标签；在曲线上拖动创建区间"))
        filter_row.addStretch()
        filter_row.addWidget(QLabel("0：同步敲击"))
        layout.addLayout(filter_row)
        self.label_list = QListWidget()
        self.label_list.setFlow(QListWidget.Flow.LeftToRight)
        self.label_list.setWrapping(True)
        self.label_list.setSpacing(2)
        self.label_list.setMinimumHeight(58)
        self.label_list.setMaximumHeight(68)
        self.label_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.label_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        layout.addWidget(self.label_list)

        buttons = QHBoxLayout()
        buttons.setSpacing(5)
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
        buttons.addWidget(self.evidence_combo)
        buttons.addWidget(QLabel("备注"))
        buttons.addWidget(self.reason_edit, 1)
        layout.addLayout(buttons)
        self.label_definition = QLabel("")
        self.label_definition.setWordWrap(False)
        self.label_definition.setMinimumHeight(18)
        self.label_definition.setStyleSheet("color:#58677b;")
        layout.addWidget(self.label_definition)
        self.annotation_splitter.addWidget(box)

    def _build_event_panel(self) -> None:
        box = QGroupBox("标注事件")
        self.event_panel = box
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 7, 8, 6)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(5)
        self.delete_event_btn = QPushButton("删除选中")
        self.clear_event_btn = QPushButton("清空")
        self.event_count_label = QLabel("0 条")
        row.addWidget(self.delete_event_btn)
        row.addWidget(self.clear_event_btn)
        row.addWidget(self.event_count_label)
        row.addStretch()
        layout.addLayout(row)

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
        self.event_table.setAlternatingRowColors(True)
        self.event_table.setShowGrid(False)
        self.event_table.verticalHeader().setVisible(False)
        self.event_table.verticalHeader().setDefaultSectionSize(24)
        header = self.event_table.horizontalHeader()
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((40, 82, 68, 158, 158, 88)):
            self.event_table.setColumnWidth(column, width)
        layout.addWidget(self.event_table, 1)
        self.annotation_splitter.addWidget(box)

    def _toggle_alignment_advanced(self, expanded: bool) -> None:
        self.alignment_advanced.setVisible(bool(expanded))
        self.alignment_toggle_btn.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
