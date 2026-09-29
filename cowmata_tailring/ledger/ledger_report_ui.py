from pathlib import Path
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QDateEdit,QLabel,QTextBrowser,QPushButton,QFileDialog
from .ledger_core import atomic_write
from .ledger_report import build_report,date_bounds,latest_date
class ReportDialog(QDialog):
    def __init__(self,store,parent=None):
        super().__init__(parent);self.store=store;self.report=None
        self.setWindowTitle("台账统计报告");self.resize(1100,760);self.setMinimumSize(820,560)
        layout=QVBoxLayout(self);layout.setContentsMargins(18,16,18,14);layout.setSpacing(12)
        controls=QHBoxLayout();self.scope=QComboBox();self.scope.addItem("全部日期","all");self.scope.addItem("单日","single");self.scope.addItem("日期范围","range")
        controls.addWidget(QLabel("统计范围"));controls.addWidget(self.scope)
        rows=store.all();first,last=date_bounds(rows);last=last or latest_date(rows);first=first or last
        self.start=QDateEdit();self.end=QDateEdit()
        for widget in (self.start,self.end):
            widget.setCalendarPopup(True);widget.setDisplayFormat("yyyy-MM-dd");widget.setMinimumDate(QDate.fromString(first,"yyyy-MM-dd"));widget.setMaximumDate(QDate.fromString(last,"yyyy-MM-dd"));widget.setFixedWidth(128)
        self.start.setDate(QDate.fromString(last,"yyyy-MM-dd"));self.end.setDate(QDate.fromString(last,"yyyy-MM-dd"))
        self.first=first;self.last=last
        self.to_label=QLabel("至")
        controls.addWidget(self.start);controls.addWidget(self.to_label);controls.addWidget(self.end);controls.addStretch()
        note=QLabel("按原表字段统计" if store.schema else "有效：九轴、脉搏、温度三项均有效");note.setStyleSheet("color:#6b7f68");controls.addWidget(note);layout.addLayout(controls)
        self.browser=QTextBrowser();self.browser.setOpenExternalLinks(False);self.browser.setStyleSheet("QTextBrowser{background:white;border:1px solid #d8e3d6;border-radius:7px;}");layout.addWidget(self.browser,1)
        bottom=QHBoxLayout();self.info=QLabel();self.info.setStyleSheet("color:#6b7f68");bottom.addWidget(self.info,1)
        copy=QPushButton("复制摘要");copy.clicked.connect(self.copy_summary)
        save=QPushButton("保存报告");save.setObjectName("primary");save.clicked.connect(self.save_report)
        close=QPushButton("关闭");close.clicked.connect(self.accept)
        for w in (copy,save,close):bottom.addWidget(w)
        layout.addLayout(bottom)
        self.scope.currentIndexChanged.connect(self.scope_changed);self.start.dateChanged.connect(self.render);self.end.dateChanged.connect(self.render);self.scope_changed()
    def scope_changed(self,*_):
        mode=self.scope.currentData();self.start.setVisible(mode!="all");self.end.setVisible(mode=="range");self.to_label.setVisible(mode=="range")
        if mode=="range":self.start.setDate(QDate.fromString(self.first,"yyyy-MM-dd"));self.end.setDate(QDate.fromString(self.last,"yyyy-MM-dd"))
        elif mode=="single":self.start.setDate(QDate.fromString(self.last,"yyyy-MM-dd"))
        self.render()
    def render(self,*_):
        if self.scope.currentData()=="range" and self.start.date()>self.end.date():
            self.report=None;self.browser.setPlainText("开始日期不能晚于结束日期。");self.info.setText("请调整日期范围");return
        if self.store.schema:
            from .ledger_sheet_ui import build_sheet_report
            self.report=build_sheet_report(self.store.all(),self.store.schema,self.scope.currentData(),self.start.date().toString("yyyy-MM-dd"),self.end.date().toString("yyyy-MM-dd"))
        else:self.report=build_report(self.store.all(),self.scope.currentData(),self.start.date().toString("yyyy-MM-dd"),self.end.date().toString("yyyy-MM-dd"))
        self.browser.setHtml(self.report["html"])
        self.info.setText(f"{self.report['date_count']} 个记录日期 · {self.report['total']} 条台账")
        self.info.setToolTip("按记录日期统计，与上传时间无关。")
    def copy_summary(self):
        if self.report:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self.report["summary"])
            self.info.setText("已复制摘要，可直接粘贴")
    def save_report(self):
        if not self.report:return
        tag=self.report["period"].replace("（","_").replace("）","").replace(" ","")
        path,selected=QFileDialog.getSaveFileName(self,"保存完整报告","台账统计-"+tag+".html","网页报告 (*.html);;文本报告 (*.txt)")
        if path:
            suffix=Path(path).suffix.lower()
            if suffix not in (".html",".txt"):path+=".txt" if "*.txt" in selected else ".html"
            atomic_write(path,(self.report["text"] if path.lower().endswith(".txt") else self.report["html"]).encode("utf-8-sig"))
            self.info.setText("报告已保存，包含分类与每日明细")
