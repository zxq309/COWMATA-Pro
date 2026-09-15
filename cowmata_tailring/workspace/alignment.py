"""One observed match completes alignment; drift calibration stays optional."""
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class AlignmentMixin:
    def create_alignment_controls(self):
        self._alignment_session = None
        panel = QWidget()
        panel.setObjectName("alignmentControls")
        panel.setStyleSheet("#alignmentControls { background:#e8f3de; border:1px solid #a9c582; border-radius:6px; }")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 6, 9, 6)
        self.alignment_tip = QLabel()
        self.alignment_tip.setWordWrap(True)
        layout.addWidget(self.alignment_tip)
        row = QHBoxLayout()
        self.alignment_mode = QComboBox()
        self.alignment_mode.addItems(["一次对齐（推荐）", "精细校准：增加对应点"])
        self.alignment_mode.currentIndexChanged.connect(self._alignment_help)
        row.addWidget(self.alignment_mode)
        row.addStretch()
        self.alignment_apply = self._button("完成对齐", self.complete_alignment, row)
        self.alignment_apply.setObjectName("primary")
        self._button("取消", self.cancel_alignment, row)
        layout.addLayout(row)
        panel.hide()
        return panel

    def _alignment_help(self, *_):
        if self.alignment_mode.currentIndex() == 0:
            text = "已暂停同步跟随。分别拖动录像和九轴，把同一动作的同一时刻放在播放线上，再点“完成对齐”。只需对应一次。"
            if self.work and len(self.work.clock.anchors) > 1 and self.work.clock.basis == "manual":
                text += " 本次将改用固定时间差，旧校准可通过撤销恢复。"
            self.alignment_apply.setText("完成对齐")
        else:
            text = "可选：仅在越播偏差越大时使用。在离原对应点较远的位置找同一动作，再保存这个点校正时间漂移。"
            self.alignment_apply.setText("保存精细校准点")
        self.alignment_tip.setText(text)

    def pin(self):
        if not self.writable_work():
            return
        if self._alignment_session is not None:
            return
        self._alignment_session = (self.work, self.work.clock.revision, self.link.isChecked())
        self.board.play(False)
        self.link.setChecked(False)
        self.link.setEnabled(False)
        self.alignment_mode.setCurrentIndex(0)
        self._alignment_help()
        self.alignment_controls.show()
        self.tell("分别拖动录像和九轴，找到同一时刻后点击“完成对齐”。")

    def cancel_alignment(self, *_):
        session = self._alignment_session
        self._alignment_session = None
        self.alignment_controls.hide()
        self.link.setEnabled(True)
        if session is not None and self.work is session[0]:
            self.link.setChecked(session[2])
            if self.linked and self.work.clock.anchors:
                self.board.seek(self.work.clock.map(self.imu_ms))

    def complete_alignment(self):
        session = self._alignment_session
        if session is None:
            return
        if self.work is not session[0] or self.work.clock.revision != session[1]:
            self.cancel_alignment()
            self.tell("当前记录或校准已变化，请重新点击“一次对齐”。")
            return
        if not self.writable_work():
            return
        self.board.play(False)
        current = next((e for e in self.board.evidence()
                        if e["camera"] == self.board.main_camera and e["frame_ready"]), None)
        if not current:
            self.alignment_tip.setText("主视角画面尚未到位。请等待画面加载完成，再点“完成对齐”。")
            return
        try:
            if self.alignment_mode.currentIndex() == 0:
                self.work.align_once(self.imu_ms, current["reference_ms"], current, self.motion.duration_ms)
                message = "一次对齐已完成，已恢复同步跟随。当前记录按固定时间差同步，可继续标注；无需第二次对齐。"
            else:
                self.work.calibrate(self.imu_ms, current["reference_ms"], current)
                message = ("精细校准已更新，已恢复同步跟随。" if len(self.work.clock.anchors) >= 2 else
                           "已保存第一个精细校准点；再增加一个相隔较远的点即可校正漂移。普通标注可直接使用一次对齐。")
            self._alignment_session = None
            self.alignment_controls.hide()
            self.link.setEnabled(True)
            self.link.setChecked(True)
            self.update_alignment_text()
            self.refresh_events()
            self.dirty = True
            self.save_current()
            self.request_record_videos()
            self.tell(message)
        except ValueError as exc:
            self.alignment_tip.setText(str(exc))
