"""Observation-first shell; the tested workspace controller remains the owner.

Only initial construction reparents the empty board. Subsequent layout changes
use geometry alone and retain native handles, source identity and playhead.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QActionGroup, QIcon, QPainter
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .materials import GLASS_STYLE, FrostedCanvas, apply_mica
from .presentation import PresentationVideoBoard, WorkspaceStage
from .signal_panel import SignalPanel
from .theme import STYLE
from .window import MainWindow as ControllerWindow


class ElidingLabel(QLabel):
    """Full text remains available to the controller, tooltip and accessibility."""
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, value):
        super().setText(value)
        self.setToolTip(value)
        self.setAccessibleName(value)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(self.palette().windowText().color())
        p.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignVCenter,
                   self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, self.width()))


class MainWindow(ControllerWindow):
    def create_plot(self):
        return SignalPanel()

    def create_board(self):
        return PresentationVideoBoard()

    def _build_ui(self):
        super()._build_ui()
        # Reuse controller-created controls and connections, before opening any
        # project or creating a native media engine. Retire only empty shells.
        old = self.takeCentralWidget()
        self.resize(1600, 1000)
        self.setMinimumSize(1080, 720)
        self.setStyleSheet(STYLE)
        self.setWindowTitle("COWMATA Annotator · 行为真值标注")
        central = FrostedCanvas()
        self.shell = central
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 8, 14, 8)
        outer.setSpacing(7)
        header = QHBoxLayout()
        brand = QSvgWidget(str(Path(__file__).resolve().parents[2] / "assets/brand/official-wordmark.svg"))
        brand.setFixedSize(150, 24)
        brand.setAccessibleName("COWMATA")
        brand.setToolTip("COWMATA Annotator")
        header.addWidget(brand)
        self._icon_button("Folder Open", "打开工程", self.choose_project, header)
        self.root_label = ElidingLabel("九轴与多视角录像 · 原始文件保持不变")
        header.addWidget(self.root_label, 1)
        self.source_toggle = self._icon_button("Panel Left", "素材", self.toggle_sources, header)
        self.source_toggle.setCheckable(True)
        self.source_toggle.setToolTip("悬停展开设备与九轴列表；点击固定 / 收起（Ctrl+L）")
        self.source_toggle.installEventFilter(self)
        self.source_hide_timer = QTimer(self)
        self.source_hide_timer.setSingleShot(True)
        self.source_hide_timer.setInterval(700)
        self.source_hide_timer.timeout.connect(self.hide_source_peek)
        self.layout_buttons = QButtonGroup(self)
        for i, title in enumerate(("观察", "多视角", "波形")):
            button = QPushButton(title)
            button.setCheckable(True)
            button.setObjectName("layout" + "ABC"[i])
            button.setToolTip(("大主视频 + 辅视角 + 底部九轴", "多视角网格 + 底部九轴", "大波形 + 可拖动画中画")[i])
            self.layout_buttons.addButton(button, i)
            header.addWidget(button)
        self.layout_buttons.idClicked.connect(lambda i: self.set_presentation("ABC"[i]))
        menus = [action.menu() for action in self.menuBar().actions()]
        files, materials, sync, edit, view = menus
        self.menuBar().clear()
        for menu, title in ((files, "文件(&F)"), (edit, "编辑(&E)"), (view, "视图(&V)")):
            menu.setTitle(title)
            self.menuBar().addMenu(menu)
        files.addSeparator()
        self._action(files, "保存并退出", self.close, "Alt+F4")
        view.addSeparator()
        self._action(view, "固定 / 收起素材列表", self.toggle_sources, "Ctrl+L")
        self._action(view, "显示 / 隐藏标注列表", self.toggle_events)
        self._action(view, "界面与播放设置…", self.presentation_settings)
        tools = self.menuBar().addMenu("工具(&T)")
        self._organize_menus(files, materials, sync, edit, view, tools)
        tools.addMenu(materials)
        tools.addMenu(sync)
        organize = self.menuBar().addMenu("数据整理(&D)")
        self._action(organize, "数据审查…", lambda: self.open_organization(0))
        self._action(organize, "数据归类…", lambda: self.open_organization(1))
        self._action(organize, "数据异常报告…", lambda: self.open_organization(2))
        self._build_algorithm_menus()
        help_menu = self.menuBar().addMenu("帮助(&H)")
        from cowmata_tailring.ui.about import show_about
        self._action(help_menu, "快速开始", self.quick_help, "F1")
        self._action(help_menu, "新手图文教程…", self.open_tutorial)
        self._action(help_menu, "关于", lambda: show_about(self)).setToolTip("软件说明、公司信息、版本号与检查更新")
        self.menuBar().show()
        for toolbar in self.findChildren(QToolBar):
            self.removeToolBar(toolbar)
            toolbar.deleteLater()
        outer.addLayout(header)
        self.banner.setStyleSheet("background:#e1eeea; color:#3d645e; padding:5px 9px; border-radius:6px; font-size:11px")
        self.banner.setParent(central)
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.banner)
        self.banner.hide()
        self.coverage_label.setStyleSheet("color:#9a6132; font-size:11px")
        outer.addWidget(self.coverage_label)

        self.body = QSplitter(Qt.Orientation.Horizontal)
        self.source_panel = QFrame()
        self.source_panel.setObjectName("sourcePanel")
        sources = QVBoxLayout(self.source_panel)
        sources.addWidget(self._heading("设备与九轴记录"))
        legend = QLabel('<span style="color:#075bb5">▶ 正在标注</span>　<span style="color:#915514">● 未完成</span><br>'
                        '<span style="color:#617277">○ 未开始</span>　<span style="color:#217044">✓ 已完成</span>')
        legend.setToolTip("切换记录自动保存并恢复上次位置；只有明确点击完成才变为已完成。")
        sources.addWidget(legend)
        sources.addWidget(self.devices)
        self.record_search = QLineEdit()
        self.record_search.setPlaceholderText("搜索文件名 / 查看进度")
        self.record_search.setClearButtonEnabled(True)
        self.record_search.textChanged.connect(self.filter_records)
        sources.addWidget(self.record_search)
        sources.addWidget(self.records, 3)
        sources.addWidget(self.cow)
        sources.addWidget(self.data_category)
        sources.addWidget(self._heading("视角 · 勾选并拖动排序"))
        sources.addWidget(self.cameras, 2)
        source_actions = QHBoxLayout()
        self._button("刷新", self.refresh_sources, source_actions)
        self._button("索引核验", self.source_manager, source_actions)
        sources.addLayout(source_actions)
        self.source_panel.setMinimumWidth(220)
        self.source_panel.setMaximumWidth(360)
        self.source_panel.installEventFilter(self)
        self.body.addWidget(self.source_panel)

        center = QWidget()
        review = QVBoxLayout(center)
        review.setContentsMargins(0, 0, 0, 0)
        review.setSpacing(5)
        self.stage = WorkspaceStage(self.board, self.plot)
        self.board.focusRequested.connect(self.focus_video)
        self.plot.setMinimumSize(300, 160)
        self.imu_position.setMaximumWidth(140)
        self.imu_position.setToolTip("九轴文件内的位置，不等于服务器收包时间")
        self.plot.toolbar.addWidget(self.imu_position)
        self.plot.toolbar.addWidget(self.link)
        self._icon_button("Pin", "对齐", self.pin, self.plot.toolbar)
        review.addWidget(self.stage, 1)
        self.alignment_label.setStyleSheet("font-size:11px; color:#7b693d")
        self.alignment_label.setParent(central)
        self.alignment_label.hide()
        review.addWidget(self.video_slider)
        transport = QHBoxLayout()
        transport.setSpacing(5)
        self._button("−10s", lambda: self.board.seek(self.board.reference_ms - 10000), transport)
        self._button("‹ 帧", lambda: self.board.step(-1), transport)
        self.play_button.setObjectName("primary")
        self.play_button.setMinimumWidth(80)
        transport.addWidget(self.play_button)
        self._button("帧 ›", lambda: self.board.step(1), transport)
        self._button("+10s", lambda: self.board.seek(self.board.reference_ms + 10000), transport)
        self.speed.setMaximumWidth(75)
        transport.addWidget(self.speed)
        self.playback_policy = QComboBox()
        self.playback_policy.addItems(["多路全速", "流畅优先", "单路优先"])
        self.playback_policy.setToolTip("单路优先：只播放主视角，其他视角逐个加载暂停图；悬浮控件切换播放，暂停时对齐各路原片")
        self.playback_policy.currentIndexChanged.connect(self.change_playback_policy)
        self.board.policyChanged.connect(lambda policy: self.playback_policy.setCurrentIndex(["full", "balanced", "focus"].index(policy)))
        self.playback_policy.setCurrentIndex(2)
        transport.addWidget(self.playback_policy)
        transport.addStretch(1)
        self.wall_input.setMaximumWidth(235)
        self.wall_input.setMinimumWidth(205)
        transport.addWidget(self.wall_input)
        self._button("跳转", self.jump_wall, transport)
        review.addLayout(transport)
        annotation = QHBoxLayout()
        annotation.addWidget(self.labels, 1)
        self.mark_button.setObjectName("primary")
        self.mark_button.setText("动作起止")
        self.mark_button.setToolTip("开始 / 结束当前视频动作；也可使用标签对应的快捷键")
        annotation.addWidget(self.mark_button)
        self.event_toggle = self._icon_button("Text Bullet List", "标注列表", self.toggle_events, annotation)
        self.event_toggle.setCheckable(True)
        self._icon_button("Save", "保存", self.save_current, annotation)
        self._button("完成本份…", self.finish_record, annotation).setToolTip("确认整份已检查，选择下一份或保存退出；Ctrl+Enter")
        review.addLayout(annotation)
        self.event_status.setStyleSheet("font-size:11px; color:#6b8179")
        self.event_status.setWordWrap(True)
        review.addWidget(self.event_status)
        self.body.addWidget(center)

        self.event_panel = QFrame()
        self.event_panel.setObjectName("eventPanel")
        details = QVBoxLayout(self.event_panel)
        details.addWidget(self._heading("标注与视频草稿"))
        self.events.setAlternatingRowColors(True)
        self.events.verticalHeader().hide()
        for col, width in enumerate((75, 110, 145, 145, 100, 170)):
            self.events.setColumnWidth(col, width)
        details.addWidget(self.events, 1)
        event_actions = QMenu(self)
        for title, explanation, handler in (("生成候选", "将选中的九轴区间添加为候选标注", self.mark_selection),
                               ("确认真值", "核对画面与同步关系后确认所选草稿", self.confirm_selected),
                               ("编辑", "编辑标签、起止边界和备注", self.edit_selected),
                               ("补充证据", "补充当前画面对应的证据线索", self.update_evidence),
                               ("证据截图…", "每个所选视角留存一张原片证据图", self.capture_evidence),
                               ("回看结束点", "跳到所选标注的结束位置", lambda: self.review_selected(at_end=True)),
                               ("删除", "删除所选标注或草稿，可撤销", self.delete_selected)):
            action = self._action(event_actions, title, handler)
            action.setToolTip(explanation)
        event_actions.setToolTipsVisible(True)
        event_button = QToolButton()
        event_button.setText("所选标注操作")
        event_button.setMenu(event_actions)
        event_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        details.addWidget(event_button)
        self.event_panel.setMinimumWidth(300)
        self.body.addWidget(self.event_panel)
        from .algorithm_panel import AlgorithmPanel
        self.algorithm_panel = AlgorithmPanel(self)
        self.algorithm_panel.exitRequested.connect(self.exit_algorithm)
        self.body.addWidget(self.algorithm_panel)
        self.body.setCollapsible(3, False)
        self.algorithm_panel.hide()
        self._algorithm_restore = None
        self.body.setSizes([245, 1100, 380])
        self.body.setCollapsible(1, False)
        outer.addWidget(self.body, 1)
        # Infrequent options retain the exact controller widgets/connections.
        self.options = QDialog(self)
        self.options.setWindowTitle("界面与播放设置")
        options = QVBoxLayout(self.options)
        options.addWidget(self._heading("播放与索引"))
        options.addWidget(self.strict)
        options.addWidget(self.compatibility)
        self.glass = QCheckBox("磨砂玻璃质感 / 系统 Mica（支持时）")
        self.glass.setChecked(True)
        self.glass.toggled.connect(self.set_glass)
        options.addWidget(self.glass)
        self.layout_choice.setParent(self.options)
        self.layout_choice.hide()  # legacy indices remain persisted separately
        self.pip_size = QComboBox()
        self.pip_size.addItems(["画中画 · 小", "画中画 · 中", "画中画 · 大"])
        self.pip_size.setCurrentIndex(1)
        self.pip_size.currentIndexChanged.connect(self.resize_pip)
        options.addWidget(self.pip_size)
        self.wave_size = QComboBox()
        self.wave_size.addItems(["底部波形 · 紧凑", "底部波形 · 标准", "底部波形 · 较大"])
        self.wave_size.setCurrentIndex(1)
        self.wave_size.currentIndexChanged.connect(self.resize_wave)
        options.addWidget(self.wave_size)
        self._button("重置画中画位置", self.reset_pip, options)
        self._button("性能与索引诊断…", self.diagnostics, options)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.options.reject)
        options.addWidget(box)
        self.setCentralWidget(central)
        self.statusBar().removeWidget(self.index_status)
        self.index_status.deleteLater()
        self.index_status = ElidingLabel("打开工程即可逐份开始")
        self.statusBar().addPermanentWidget(self.index_status, 1)
        self.status_details_button = QPushButton("加载记录…")
        self.status_details_button.setToolTip("展开完整状态、路径和加载记录；可选择复制")
        self.status_details_button.clicked.connect(self.show_status_details)
        self.statusBar().addPermanentWidget(self.status_details_button)
        self.set_glass(True)
        self.source_panel.hide()
        self.event_panel.hide()
        old.deleteLater()
        self.set_presentation("A", persist=False)
        # Scope shortcut protection to our input widgets. A process-wide Qt
        # filter also intercepts native decoder/widget teardown and is unsafe
        # when several old/new windows coexist.
        for control in self.findChildren(QWidget):
            if isinstance(control, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)):
                control.installEventFilter(self)

    def _heading(self, text):
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _build_algorithm_menus(self):
        from .algorithm_catalog import BEHAVIORS, HEALTH
        self.algorithm_actions = {}
        self.algorithm_group = QActionGroup(self)
        self.algorithm_group.setExclusive(True)
        for title, specs in (("行为识别(&B)", BEHAVIORS), ("健康与繁殖(&R)", HEALTH)):
            menu = self.menuBar().addMenu(title)
            menu.setToolTipsVisible(True)
            for spec in specs:
                action = self._action(menu, spec.title, lambda _checked=False, s=spec: self.open_algorithm(s))
                action.setCheckable(True)
                action.setToolTip("单摄像头算法检查 · " + ("对应标签 " + spec.code if spec.domain == "behavior" else "待接入，不生成健康结论"))
                self.algorithm_group.addAction(action)
                self.algorithm_actions[spec.code] = action
            menu.addSeparator()
            self._action(menu, "返回标注布局", self.exit_algorithm)

    def open_algorithm(self, spec):
        panel = self.algorithm_panel
        if panel.running:
            panel.cancel()
        if self._candidate_window is not None:
            if self._candidate_window.running:
                self.tell("请先结束或取消候选预测，再进入独立算法检查。")
                self.algorithm_actions[spec.code].setChecked(False)
                return
            self._candidate_window.hide()
        if self._algorithm_restore is None:
            self._algorithm_restore = (self.stage.mode, self.playback_policy.currentIndex(),
                                       self.board.expanded, not self.event_panel.isHidden())
            self._algorithm_splitter_sizes = self.body.sizes()
        self.board.play(False)
        self.board.set_single_camera_only(True)
        self.set_presentation("A", persist=False)
        for button in self.layout_buttons.buttons():
            button.setEnabled(False)
        self.playback_policy.setEnabled(False)
        self.event_panel.hide()
        self.event_toggle.setChecked(False)
        panel.set_algorithm(spec)
        panel.show()
        self.body.setSizes([250, 1000, 0, 330])
        self.algorithm_actions[spec.code].setChecked(True)

    def exit_algorithm(self):
        if getattr(self, "_algorithm_restore", None) is None:
            return
        self.algorithm_panel.cancel()
        self.algorithm_panel.hide()
        mode, policy, expanded, events = self._algorithm_restore
        self._algorithm_restore = None
        self.board.set_single_camera_only(False)
        self.board.expanded = expanded
        self.set_presentation(mode, persist=False)
        self.playback_policy.setCurrentIndex(policy)
        for button in self.layout_buttons.buttons():
            button.setEnabled(True)
        self.playback_policy.setEnabled(True)
        self.event_panel.setVisible(events)
        self.event_toggle.setChecked(events)
        self.body.setSizes(self._algorithm_splitter_sizes)
        self.algorithm_group.setExclusive(False)
        for action in self.algorithm_actions.values():
            action.setChecked(False)
        self.algorithm_group.setExclusive(True)
        self.board.relayout()

    def open_candidates(self):
        if self.algorithm_panel.running:
            self.tell("请先结束或取消独立算法运行，再打开候选标注。")
            return
        self.exit_algorithm()
        super().open_candidates()

    def open_tutorial(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        path = Path(__file__).resolve().parents[2] / "docs/quick-start-illustrated.pdf"
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.quick_help()

    def _organize_menus(self, files, materials, sync, edit, view, tools):
        """One-level categories; existing actions retain handlers and shortcuts."""
        exports = QMenu("导出", self)
        legacy = QMenu("旧版兼容", self)
        collaboration = QMenu("多人协作", self)
        evidence = QMenu("标注与证据", self)
        for menu in (files, materials, sync, edit, view):
            for action in list(menu.actions()):
                original = action.text()
                action.setToolTip(original)
                action.setStatusTip(original)
                target = None
                if original.startswith(("导出当前", "导出所选", "训练与兼容")):
                    target = exports
                elif "旧" in original:
                    target = legacy
                elif "多人" in original:
                    target = collaboration
                elif "候选预测" in original:
                    target = tools
                elif menu is edit and any(word in original for word in ("真值", "证据")):
                    target = evidence
                if target:
                    menu.removeAction(action)
                    target.addAction(action)
                short = {
                    "打开数据工程…": "打开工程…", "打开指定九轴 JSON…": "打开九轴…",
                    "保存人工成果": "保存", "打开历史标注回看…": "历史回看…",
                    "导出当前成果…": "完整成果…", "导出所选九轴片段（含标签）…": "所选片段…",
                    "训练与兼容格式（批量导出）…": "训练数据…", "接收多人标注成果…": "接收成果…",
                    "多人协作与回传设置…": "回传设置…", "继续扩大当前时段检索": "扩大当前检索",
                    "后台完整索引（可选、耗时）": "完整索引…", "暂停后台检索": "暂停检索",
                    "刷新 / 复制完成，重新检查": "刷新素材", "素材与时间核验…": "素材核验…",
                    "新增唯一拷贝批次…": "新建拷贝批次…", "全文件内容核验（耗时）": "内容核验…",
                    "录像归档副本核验（不删除原片）…": "归档核验…",
                    "九轴同步锚点与未确认区间…": "九轴校准…", "当前主视角相机时钟校准…": "相机校准…",
                    "新版事件候选预测…": "事件候选预测…", "将本份重新标为进行中": "重新标注本份",
                    "固定 / 收起素材列表": "素材列表", "显示 / 隐藏标注列表": "标注列表",
                    "性能与索引诊断…": "性能诊断…"}.get(original)
                if short:
                    action.setText(short)
            menu.setToolTipsVisible(True)
        files.addMenu(exports)
        files.addMenu(legacy)
        tools.addMenu(collaboration)
        edit.addMenu(evidence)
        materials.setTitle("录像索引")
        sync.setTitle("时间同步")
        for menu in (exports, legacy, collaboration, evidence, tools):
            menu.setToolTipsVisible(True)

    def set_glass(self, enabled):
        self.shell.set_effects(enabled)
        self.setStyleSheet(STYLE + (GLASS_STYLE if enabled else ""))
        self.material_result = apply_mica(int(self.winId()), enabled)
        if self.catalog:
            self.dirty = True

    def change_playback_policy(self, index):
        policy = ["full", "balanced", "focus"][index]
        self.board.set_policy(policy)
        self.strict.setToolTip("预览模式只等待主路原片；辅路预览不参与同步真值确认")
        if self.catalog:
            self.dirty = True

    def _icon_button(self, name, title, handler, layout):
        button = self._button(title, handler, layout)
        path = Path(__file__).resolve().parents[2] / "assets" / "fluent" / (name.lower().replace(" ", "_") + ".svg")
        if path.is_file():
            button.setIcon(QIcon(str(path)))
            button.setIconSize(QSize(18, 18))
        button.setAccessibleName(title)
        button.setToolTip(title)
        return button

    def eventFilter(self, watched, event):
        if hasattr(self, "source_hide_timer") and watched in (self.source_toggle, getattr(self, "source_panel", None)):
            if event.type() == QEvent.Type.Enter:
                self.source_hide_timer.stop()
                if watched is self.source_toggle:
                    self.source_panel.show()
            elif event.type() == QEvent.Type.Leave:
                self.source_hide_timer.start()
        if event.type() == QEvent.Type.ShortcutOverride:
            focus = QApplication.focusWidget()
            if focus and focus.window() == self and isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)):
                modifiers = event.modifiers()
                if not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
                    event.accept()
                    return True
        return False

    def toggle_sources(self):
        visible = not self.source_toggle.isChecked() if self.sender() is not self.source_toggle else self.source_toggle.isChecked()
        self.source_panel.setVisible(visible)
        self.source_toggle.setChecked(visible)

    def hide_source_peek(self):
        if not self.source_toggle.isChecked() and not self.source_panel.underMouse() and not self.source_toggle.underMouse() and QApplication.activePopupWidget() is None:
            self.source_panel.hide()

    def filter_records(self):
        if not hasattr(self, "record_search"):
            return
        text = self.record_search.text().strip().casefold()
        for i in range(self.records.count()):
            item = self.records.item(i)
            item.setHidden(text not in (item.text() + item.toolTip()).casefold())

    def refresh_records(self, *_):
        super().refresh_records()
        self.filter_records()

    def tell(self, message):
        super().tell(message)
        self.statusBar().showMessage(str(message), 12000)
        self.statusBar().setToolTip(str(message))
        if str(message).startswith(("加载成功", "加载失败", "无法打开工程", "正在取消")):
            self.banner.show()

    def show_status_details(self):
        if not hasattr(self, "status_dialog"):
            self.status_dialog = QDialog(self)
            self.status_dialog.setWindowTitle("完整加载记录")
            self.status_dialog.resize(900, 480)
            layout = QVBoxLayout(self.status_dialog)
            self.status_log = QPlainTextEdit()
            self.status_log.setReadOnly(True)
            self.status_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            self.status_log.setMaximumBlockCount(500)
            self.status_log.setPlainText("\n".join(f"{stamp}  {text}" for stamp, text in self._status_history))
            layout.addWidget(self.status_log)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.status_dialog.hide)
            layout.addWidget(buttons)
        self.status_dialog.show()
        self.status_dialog.raise_()

    def update_alignment_text(self):
        super().update_alignment_text()
        self.link.setToolTip(self.alignment_label.text())

    def update_coverage(self, *, force=False):
        super().update_coverage(force=force)
        text = self.coverage_label.text()
        self.coverage_label.setToolTip(text)
        self.coverage_label.setVisible(bool(self.motion) and not text.startswith("当前参考时刻有录像覆盖"))
        if text.startswith("录像仍在索引"):
            self.coverage_label.setText("当前时刻暂未匹配录像 · 仍有未检索素材，可在「工具 → 录像索引」继续检索")

    def refresh_events(self):
        super().refresh_events()
        self.mark_button.setToolTip(self.event_status.text())
        self.event_status.setVisible(bool(self.active_event))

    def quick_help(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, "逐份标注 · 快速开始",
            "1. 文件 → 打开数据工程：先清点文件，不全量解码。\n"
            "2. 素材列表选择一份九轴：按实际采集时间检索多视角录像。\n"
            "3. 核对同步、牛号，观察录像并标注；保存不会修改原始文件。\n"
            "4. 点击「完成本份」：下一份 / 保存退出；没做完则暂存。\n"
            "5. 重开工程恢复未完成位置；已完成记录仍可从列表回看。\n\n"
            "未知录像的时间需要首次 OCR；文件编号只用于加速搜索，不是真值。\n"
            "未检索不等于无录像。未找到时可用「工具 → 素材 → 继续扩大检索」。\n"
            "更新设置在「帮助 → 关于」；Ctrl+L 固定列表，悬停素材按钮可临时展开。")

    def toggle_events(self):
        if self._algorithm_restore is not None:
            self.exit_algorithm()
        visible = not self.event_panel.isVisible()
        self.event_panel.setVisible(visible)
        self.event_toggle.setChecked(visible)

    def set_presentation(self, mode, *, persist=True):
        if getattr(self, "_algorithm_restore", None) is not None:
            mode = "A"
        self.stage.set_mode(mode)
        self.layout_buttons.button("ABC".index(mode)).setChecked(True)
        if persist and self.catalog:
            self.dirty = True

    def presentation_settings(self):
        self.options.exec()

    def focus_video(self, camera):
        self.set_presentation("A")
        self.board.expanded = None
        self.board.enlarge(camera)

    def set_two_view_ratio(self):
        ratio, ok = QInputDialog.getInt(self, "双画面主视角宽度", "A 观察布局中，两路画面的主视角宽度百分比",
                                        self.board.observation_ratio, 50, 85, 5)
        if ok:
            self.board.observation_ratio = ratio
            self.board.relayout()
            self.dirty = True
            self.save_current()

    def resize_pip(self, i):
        self.stage.pip_scale = [.26, .34, .46][i]
        self.stage.arrange()
        if self.catalog:
            self.dirty = True

    def resize_wave(self, i):
        self.stage.wave_ratio = [.25, .32, .43][i]
        self.stage.arrange()
        if self.catalog:
            self.dirty = True

    def reset_pip(self):
        self.stage.pip_position = (1.0, 0.0)
        self.stage.arrange()

    def open_project(self, root, *, preferred_json=None):
        self.exit_algorithm()
        self.banner.hide()
        super().open_project(root, preferred_json=preferred_json)
        if not self.catalog:
            return
        self.restore_presentation(self.settings.get("presentation", {}))

    def restore_presentation(self, prefs):
        prefs = prefs if isinstance(prefs, dict) else {}
        def index(key, default, maximum):
            value = prefs.get(key, default)
            return value if isinstance(value, int) and 0 <= value <= maximum else default
        mode = prefs.get("mode", "A")
        self.set_presentation(mode if isinstance(mode, str) and mode in {"A", "B", "C"} else "A", persist=False)
        self.pip_size.setCurrentIndex(index("pip_size", 1, 2))
        self.wave_size.setCurrentIndex(index("wave_size", 1, 2))
        self.plot.group.setCurrentIndex(index("signal_group", 0, 4))
        self.playback_policy.setCurrentIndex(index("playback_policy", 2, 2))
        self.glass.setChecked(prefs.get("glass", True) is not False)
        ratio = prefs.get("observation_ratio", 75)
        self.board.observation_ratio = ratio if isinstance(ratio, int) and 50 <= ratio <= 85 else 75
        self.stage.pip_position = (1.0, 0.0)
        pos = prefs.get("pip_position", [1.0, 0.0])
        if isinstance(pos, (list, tuple)) and len(pos) == 2 and all(isinstance(v, (int, float)) for v in pos):
            self.stage.pip_position = tuple(max(0, min(1, v)) for v in pos)
        self.source_panel.setVisible(prefs.get("sources_open") is True)
        self.source_toggle.setChecked(self.source_panel.isVisible())
        self.event_panel.setVisible(prefs.get("events_open") is True)
        self.event_toggle.setChecked(self.event_panel.isVisible())
        self.stage.arrange()

    def save_current(self, *_, background=False):
        if hasattr(self, "stage") and self.catalog and not self.catalog.readonly:
            self.settings["presentation"] = {
                "mode": self.stage.mode, "pip_size": self.pip_size.currentIndex(),
                "wave_size": self.wave_size.currentIndex(), "pip_position": self.stage.pip_position,
                "signal_group": self.plot.group.currentIndex(),
                "observation_ratio": self.board.observation_ratio,
                "playback_policy": self.playback_policy.currentIndex(), "glass": self.glass.isChecked(),
                "sources_open": self.source_toggle.isChecked(), "events_open": not self.event_panel.isHidden(),
            }
            if self._algorithm_restore is not None:
                mode, policy, _, events = self._algorithm_restore
                self.settings["presentation"].update(mode=mode, playback_policy=policy, events_open=events)
        super().save_current(background=background)

    def closeEvent(self, event):
        organize = getattr(self, "_organization_window", None)
        if organize is not None and (organize.running or organize.pause_pending):
            organize.cancel()
            event.ignore()
            QTimer.singleShot(150, self.close)
            return
        panel = getattr(self, "algorithm_panel", None)
        if panel is not None and panel.running:
            panel.cancel()
            event.ignore()
            QTimer.singleShot(300, self.close)
            return
        super().closeEvent(event)
        if event.isAccepted() and panel is not None:
            panel.timer.stop()
        if event.isAccepted() and organize is not None:
            organize.close()

    def playback_changed(self, playing):
        super().playback_changed(playing)
        self.play_button.setToolTip("空格播放 / 暂停；输入文字时不会触发标注快捷键")
