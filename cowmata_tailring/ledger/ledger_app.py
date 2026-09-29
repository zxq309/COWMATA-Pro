from __future__ import annotations
import csv,io,json,os,sys,traceback
from pathlib import Path
from datetime import datetime
from PySide6.QtCore import Qt,QTimer,QThread,Signal,QDate,QDateTime,QLockFile,QRect
from PySide6.QtGui import QColor,QFont,QIcon,QKeySequence,QShortcut,QTextCharFormat,QPainter,QPen
from PySide6.QtWidgets import *
from .ledger_core import *
from .ledger_import import import_excel
from .ledger_files import read_document,import_document,writeback,display_value,file_conflicts,resolve_file_conflict,extra_values,field_value,edit_extra,reconcile_sources,source_conflict_count,conflict_sources
from .ledger_sync import synchronize
from .ledger_review import review_items,save_review,source_issue
from .ledger_files import SourceFileError,relocate_source
from .ledger_review_ui import ReviewPanel
from .ledger_auth import authorized
from . import ledger_session
from .ledger_access_ui import LoginDialog,AccountsDialog,AccountWorker
from .ledger_version import VERSION
from .ledger_sheets import get_schema,SCHEMAS,import_workbook,BIRTH,WEARING,MANAGEMENT
from .ledger_sheet_ui import SheetRecordDialog,visible_fields

ROOT=Path(__file__).resolve().parents[1]
ENTRY_ROOT=Path(os.environ.get("COWMATA_LEDGER_INSTALL_ROOT",str(ROOT)))
# 待产犊 is a live view of the samples sheet (calving start and end both blank), not a fourth store.
SHEET_TITLES=("样本试验台账","产犊登记","设备台账","待产犊")
# Set by cowmata_tailring.ledger.host: no tray icon, no autostart, closing never quits the host app.
EMBEDDED=False
STYLE="""
QWidget {background:#f3f7f0;color:#20332a;font-family:'Microsoft YaHei UI';font-size:12px;}
QMainWindow {background:#f3f7f0;}
QFrame#card,QGroupBox {background:white;border:1px solid #d7e4d0;border-radius:10px;}
QGroupBox {margin-top:10px;padding:20px 12px 12px;font-weight:600;}
QGroupBox::title {subcontrol-origin:margin;left:16px;padding:0 6px;color:#315225;}
QLabel {background:transparent;border:0;}
QLabel#brand {font-size:18px;font-weight:800;color:#345c23;}
QLabel#heading {font-size:20px;font-weight:700;}
QLabel#section {font-size:14px;font-weight:700;padding:5px 3px;}
QToolButton {background:transparent;border:0;border-radius:4px;font-size:16px;}
QToolButton:hover {background:#e1efd9;}
QLabel#muted {color:#6b7f68;}
QLabel#number {font-size:28px;font-weight:700;color:#315225;}
QPushButton {background:white;border:1px solid #d1dfca;border-radius:7px;padding:4px 11px;min-height:21px;font-weight:600;}
QPushButton:hover {background:#e6f6dc;border-color:#8ebd73;}
QPushButton#primary {background:#8add66;border-color:#69b24e;color:#20351c;}
QPushButton#primary:hover {background:#73cd4c;}
QPushButton#primary[mainAction="true"] {font-size:15px;font-weight:700;min-height:32px;padding:5px 16px;}
QPushButton#importOption {background:#eef9e7;border:1px solid #b6d8a4;border-radius:10px;text-align:left;padding:16px;font-size:15px;min-height:64px;}
QPushButton#importOption:hover,QPushButton#importOption:focus {background:#dbf2ca;border:2px solid #7bbd59;}
QPushButton:disabled {background:#edf0e9;color:#96a28f;}
QLineEdit,QComboBox,QDateEdit,QDateTimeEdit,QSpinBox,QPlainTextEdit {background:white;border:1px solid #d1dfca;border-radius:5px;padding:5px;selection-background-color:#cceab8;}
QLineEdit:focus,QComboBox:focus,QDateEdit:focus,QDateTimeEdit:focus,QPlainTextEdit:focus {border-color:#35afc8;}
QComboBox::drop-down {border:0;width:20px;}
QTableWidget,QTreeWidget,QListWidget {background:white;border:1px solid #d7e4d0;border-radius:7px;outline:0;selection-background-color:#dff3d1;selection-color:#20351c;}
QHeaderView::section {background:#edf5e7;border:0;border-right:1px solid #dbe6d4;border-bottom:1px solid #dbe6d4;padding:5px 2px;font-weight:600;color:#3f5737;}
QTableWidget::item {padding:1px 3px;border-bottom:1px solid #edf1ea;}
QTreeWidget::item {padding:5px 1px;}
QTreeWidget::item:selected {background:#dff3d1;color:#244f16;}
QTabWidget::pane {border:1px solid #d7e4d0;background:white;}
QTabBar::tab {padding:10px 24px;background:#eaf3e4;}
QTabBar::tab:selected {background:#8add66;font-weight:700;}
QCalendarWidget {background:white;}
QCalendarWidget QToolButton {color:#315225;background:#edf5e7;padding:5px;border:0;}
QCalendarWidget QAbstractItemView {background:white;selection-background-color:#35afc8;selection-color:white;}
QCalendarWidget QSpinBox {color:#20332a;}
QStatusBar {background:#eaf3e4;color:#3f5737;border-top:1px solid #d7e4d0;}
QSplitter::handle {background:#dfe8d8;}
QScrollBar:vertical {background:#f0f5ed;width:11px;}
QScrollBar::handle:vertical {background:#bbcdb0;min-height:30px;border-radius:5px;}
QScrollBar:horizontal {background:#f0f5ed;height:12px;}
QScrollBar::handle:horizontal {background:#bbcdb0;min-width:30px;border-radius:5px;}
QScrollBar::add-line,QScrollBar::sub-line {width:0;height:0;}
QToolTip {background:#fcfff8;color:#20332a;border:1px solid #a5c689;padding:8px;}
"""
def label(text,name=None):
    w=QLabel(text)
    if name:w.setObjectName(name)
    return w
def button(text,callback,primary=False):
    w=QPushButton(text);w.clicked.connect(callback)
    if primary:w.setObjectName("primary")
    return w
def hbox(*items):
    w=QWidget();l=QHBoxLayout(w);l.setContentsMargins(0,0,0,0)
    for item in items:l.addWidget(item)
    return w
class ElidedLabel(QLabel):
    def __init__(self,text="",parent=None):
        super().__init__(parent);self.full_text="";self.setMinimumWidth(0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);self.setText(text)
    def setText(self,text):
        self.full_text=str(text);self.setToolTip(self.full_text);self.update_text()
    def update_text(self):
        super().setText(self.fontMetrics().elidedText(self.full_text,Qt.ElideRight,max(30,self.width())))
    def resizeEvent(self,event):
        super().resizeEvent(event);self.update_text()
class SourceHeader(QHeaderView):
    def __init__(self,table):super().__init__(Qt.Horizontal,table);self.table=table;self.original_sample=False
    def paintEvent(self,event):
        if not self.original_sample:return super().paintEvent(event)
        painter=QPainter(self.viewport());painter.setPen(QPen(QColor("#dbe6d4")))
        groups={"设备号":("佩戴记录",["设备号","佩戴开始","佩戴结束"]),"佩戴开始":("佩戴记录",["设备号","佩戴开始","佩戴结束"]),"佩戴结束":("佩戴记录",["设备号","佩戴开始","佩戴结束"]),"产犊开始":("产犊时间段",["产犊开始","产犊结束"]),"产犊结束":("产犊时间段",["产犊开始","产犊结束"]),"九轴":("样本有效性预判",["九轴","脉搏","温度"]),"脉搏":("样本有效性预判",["九轴","脉搏","温度"]),"温度":("样本有效性预判",["九轴","脉搏","温度"])}
        keys=[self.table.horizontalHeaderItem(c).data(Qt.UserRole) for c in range(self.count())];drawn=set();h=self.height();half=h//2
        def cell(rect,text):
            painter.fillRect(rect,QColor("#edf5e7"));painter.setPen(QColor("#dbe6d4"));painter.drawRect(rect.adjusted(0,0,-1,-1));painter.setPen(QColor("#3f5737"));painter.drawText(rect.adjusted(3,1,-3,-1),Qt.AlignCenter|Qt.TextWordWrap,text)
        for c,key in enumerate(keys):
            if self.isSectionHidden(c):continue
            x=self.sectionViewportPosition(c);width=self.sectionSize(c)
            if key in groups:
                name,members=groups[key]
                if name not in drawn:
                    columns=[keys.index(k) for k in members if k in keys and not self.isSectionHidden(keys.index(k))];left=min(self.sectionViewportPosition(i) for i in columns);right=max(self.sectionViewportPosition(i)+self.sectionSize(i) for i in columns)
                    cell(QRect(left,0,right-left,half),name);drawn.add(name)
                cell(QRect(x,half,width,h-half),{"产犊开始":"开始","产犊结束":"结束"}.get(key,key))
            else:cell(QRect(x,0,width,h),"状态" if key=="同步状态" else key)
        painter.end()

class FitTable(QTableWidget):
    resized=Signal()
    def resizeEvent(self,event):
        super().resizeEvent(event);self.resized.emit()
def tool_button(text,tip,callback):
    w=QToolButton();w.setText(text);w.setToolTip(tip);w.setAccessibleName(tip);w.setFixedSize(28,28);w.clicked.connect(callback);return w

class OptionalDate(QWidget):
    def __init__(self,value="",date_only=False):
        super().__init__();self.date_only=date_only;self.original=value;self.touched=False
        l=QHBoxLayout(self);l.setContentsMargins(0,0,0,0)
        self.state=QComboBox();self.state.addItems(["已填写","待补充","不适用 /"])
        self.date=QDateEdit() if date_only else QDateTimeEdit()
        self.date.setDisplayFormat("yyyy-MM-dd" if date_only else "yyyy-MM-dd HH:mm")
        self.date.setCalendarPopup(True)
        self.date.setDateTime(QDateTime.currentDateTime())
        self.date.setMinimumDate(QDate(1990,1,1));self.date.setMaximumDate(QDate(2100,12,31))
        self.raw=QLabel();self.raw.setStyleSheet("color:#a85c17;font-size:11px")
        if value in ("","/"):self.state.setCurrentIndex(2 if value=="/" else 1)
        else:
            try:
                datetime.fromisoformat(value)
                if date_only:self.date.setDate(QDate.fromString(value,"yyyy-MM-dd"))
                else:self.date.setDateTime(QDateTime.fromString(value,"yyyy-MM-dd HH:mm:ss" if len(value)>16 else "yyyy-MM-dd HH:mm"))
            except ValueError:self.raw.setText("原值："+value+"（请选择正确日期）")
        l.addWidget(self.state);l.addWidget(self.date,1)
        if self.raw.text():l.addWidget(self.raw)
        self.date.setEnabled(self.state.currentIndex()==0)
        self.state.currentIndexChanged.connect(self.changed);self.date.dateTimeChanged.connect(self.changed)
    def changed(self,*_):
        self.touched=True;self.date.setEnabled(self.state.currentIndex()==0)
        if self.raw.text():self.raw.setText("")
    def value(self):
        if self.raw.text() and not self.touched:return self.original
        return ["date","","/"][self.state.currentIndex()].replace("date",(self.date.date().toString("yyyy-MM-dd") if self.date_only else self.date.dateTime().toString("yyyy-MM-dd HH:mm")))
class RecordDialog(QDialog):
    def __init__(self,row,settings,parent=None):
        super().__init__(parent);self.row=dict(row);self.fields={};self.result_row=None
        self.setWindowTitle("填写试验台账");self.resize(1040,820)
        outer=QVBoxLayout(self);outer.addWidget(label("填写试验台账","heading"))
        outer.addWidget(label("对应 Excel A—O 列  ·  日期使用日历选择  ·  未发生留空，不适用填 /","muted"))
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();layout=QVBoxLayout(body)
        top=QHBoxLayout()
        g=QGroupBox("01  牛只与归类");form=QFormLayout(g)
        for key,hint in [("序号","例如 236"),("牛号","例如 24311A3，保留前导零")]:
            w=QLineEdit(row.get(key,""));w.setPlaceholderText(hint);self.fields[key]=w;form.addRow(key,w)
        self.fields["预产期"]=OptionalDate(row.get("预产期",""),True);form.addRow("预产期",self.fields["预产期"])
        self.fields["记录日期"]=QDateEdit(QDate.fromString(row.get("记录日期") or today(),"yyyy-MM-dd"));self.fields["记录日期"].setCalendarPopup(True);self.fields["记录日期"].setDisplayFormat("yyyy-MM-dd")
        form.addRow("记录日期",self.fields["记录日期"])
        for key in ["牧场","现场标记"]:
            self.fields[key]=QLineEdit(row.get(key,""));form.addRow(key,self.fields[key])
        cat=QComboBox()
        for code,name in CATEGORIES.items():cat.addItem(name,code)
        cat.setCurrentIndex(max(0,cat.findData(row.get("数据分类"))));self.fields["数据分类"]=cat;cat.setEnabled(False);cat.setToolTip("按完整区间和原监测目的自动派生，不改原表监测目的");form.addRow("下载目录分类",cat)
        top.addWidget(g)
        g=QGroupBox("02  佩戴记录与产犊时间");form=QFormLayout(g)
        self.fields["设备号"]=QLineEdit(row.get("设备号",""));self.fields["设备号"].setPlaceholderText("12位完整编号，例如 0C3D5EA22DF1");form.addRow("设备号",self.fields["设备号"])
        for key in ["佩戴开始","佩戴结束","产犊开始","产犊结束"]:
            w=OptionalDate(row.get(key,""));self.fields[key]=w;form.addRow(key,w)
        purpose=QComboBox();purpose.addItems(PURPOSES);purpose.setCurrentText(row.get("监测目的","产犊监测"))
        purpose.setEditable(True);self.fields["监测目的"]=purpose;form.addRow("监测目的",purpose)
        purpose.textActivated.connect(lambda text:cat.setCurrentIndex(cat.findData(category_for(text))))
        top.addWidget(g);layout.addLayout(top)
        g=QGroupBox("03  样本有效性与人员");form=QGridLayout(g)
        for i,key in enumerate(["九轴","脉搏","温度","尾环佩戴人","事件标注人"]):
            w=QComboBox()
            if key in ["九轴","脉搏","温度"]:w.addItems(VALIDITY);w.setCurrentText(row.get(key,""))
            else:w.setEditable(True);w.addItems(list(dict.fromkeys([row.get(key,""),settings.get("wearer" if key=="尾环佩戴人" else "annotator","")])));w.setCurrentText(row.get(key,""))
            self.fields[key]=w;form.addWidget(label(key),0,i);form.addWidget(w,1,i)
        layout.addWidget(g)
        g=QGroupBox("04  样本评价");gl=QVBoxLayout(g);w=QPlainTextEdit(row.get("样本评价",""));w.setPlaceholderText("记录数据覆盖情况、异常和需要说明的信息");w.setMaximumHeight(100);self.fields["样本评价"]=w;gl.addWidget(w);self.fields["时间说明"]=QLineEdit(row.get("时间说明",""));self.fields["时间说明"].setPlaceholderText("时间精度说明，例如：产犊开始为估计时间");gl.addWidget(self.fields["时间说明"]);layout.addWidget(g)
        if row.get("核对提示"):
            warning=label("历史记录待核对："+row["核对提示"]);warning.setWordWrap(True);warning.setStyleSheet("color:#a85c17");layout.addWidget(warning)
        self.preview=label("目录："+directory(row),"muted");self.preview.setWordWrap(True);layout.addWidget(self.preview)
        scroll.setWidget(body);outer.addWidget(scroll)
        self.error=label("");self.error.setWordWrap(True);self.error.setStyleSheet("color:#ad3c29");outer.addWidget(self.error)
        outer.addWidget(hbox(label("保存到本地，点击“保存修改并同步”后上传","muted"),button("取消",self.reject),button("保存记录  Ctrl+S",self.save,True)))
        QShortcut(QKeySequence.Save,self,activated=self.save)
    def save(self):
        row=dict(self.row)
        for key,w in self.fields.items():
            if isinstance(w,OptionalDate):row[key]=w.value()
            elif isinstance(w,QDateEdit):row[key]=w.date().toString("yyyy-MM-dd")
            elif isinstance(w,QComboBox):row[key]=w.currentData() if key=="数据分类" else w.currentText()
            elif isinstance(w,QPlainTextEdit):row[key]=w.toPlainText()
            else:row[key]=w.text()
        try:
            row=normalize(row);row["数据分类"]=classify_record(row)[0];errors=issues(row)
            if errors:raise ValueError("；".join(errors))
        except ValueError as e:self.error.setText(str(e));return
        row["核对提示"]="";self.result_row=row;self.accept()
class ImportPreview(QDialog):
    def __init__(self,rows,parent=None):
        super().__init__(parent);self.rows=rows;self.mode=None
        self.setWindowTitle("导入台账 · 先看结果");self.resize(1180,740)
        layout=QVBoxLayout(self);layout.addWidget(label("导入台账","heading"))
        bad=sum(bool(r.get("核对提示")) for r in rows)
        layout.addWidget(label(f"共 {len(rows)} 条 · 正常 {len(rows)-bad} 条 · 需要核对 {bad} 条","muted"))
        info=label("已按 Excel 原表头读取，合并单元格已补齐。异常标红并保留原文，重复记录自动跳过。","muted");info.setWordWrap(True);layout.addWidget(info)
        self.only=QCheckBox("只看异常");self.only.toggled.connect(self.render);layout.addWidget(self.only)
        self.table=QTableWidget(0,len(BUSINESS)+2);self.table.setHorizontalHeaderLabels(["Excel行号"]+BUSINESS+["核对提示"]);self.table.setEditTriggers(QAbstractItemView.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setWordWrap(False)
        self.table.verticalHeader().hide();self.table.verticalHeader().setDefaultSectionSize(36)
        for i in range(self.table.columnCount()):self.table.setColumnWidth(i,145 if i>3 else 100)
        self.table.setColumnWidth(len(BUSINESS),220);self.table.setColumnWidth(len(BUSINESS)+1,330);layout.addWidget(self.table,1)
        self.hint=label("直接同步：正常记录立即上传，异常记录留待核对。\n先核对：全部记录先保存在本地，点击“保存修改并同步”确认后才上传。","muted");self.hint.setWordWrap(True);layout.addWidget(self.hint)
        layout.addWidget(hbox(button("取消",self.reject),button("先核对再同步",lambda:self.choose("review")),button("导入并同步",lambda:self.choose("direct"),True)));self.render()
    def render(self):
        rows=[r for r in self.rows if not self.only.isChecked() or r.get("核对提示")]
        self.table.setRowCount(len(rows))
        for n,row in enumerate(rows):
            source=(row.get("来源","").split(" / ")[-1] or "CSV")
            for c,text in enumerate([source]+[row.get(f,"") for f in BUSINESS]+[row.get("核对提示","")]):
                item=QTableWidgetItem(text);item.setToolTip(text)
                key=BUSINESS[c-1] if 1<=c<=len(BUSINESS) else ""
                if row.get("核对提示") and (c==0 or c==len(BUSINESS)+1 or key and key in row["核对提示"]):
                    item.setBackground(QColor("#fff0e5"));item.setForeground(QColor("#a74721"))
                self.table.setItem(n,c,item)
    def choose(self,mode):self.mode=mode;self.accept()

class OptionsDelegate(QStyledItemDelegate):
    def createEditor(self,parent,option,index):
        key=self.parent().horizontalHeaderItem(index.column()).data(Qt.UserRole) or self.parent().horizontalHeaderItem(index.column()).text()
        values=VALIDITY if key in ["九轴","脉搏","温度"] else PURPOSES if key=="监测目的" else list(CATEGORIES.values()) if key=="数据分类" else None
        if values is not None:
            editor=QComboBox(parent);editor.setEditable(True);editor.addItems(values);return editor
        return super().createEditor(parent,option,index)
    def setEditorData(self,editor,index):
        if isinstance(editor,QComboBox):editor.setCurrentText(index.data(Qt.UserRole+1) or index.data() or "")
        elif isinstance(editor,QLineEdit):editor.setText(index.data(Qt.UserRole+1) or "")
        else:super().setEditorData(editor,index)
    def setModelData(self,editor,model,index):
        if isinstance(editor,QComboBox):model.setData(index,editor.currentText())
        else:super().setModelData(editor,model,index)
class SyncWorker(QThread):
    success=Signal(dict,list);failed=Signal(str,str)
    def __init__(self,settings,pending,action="sync",known_ids=None):
        super().__init__();self.settings=settings;self.pending=pending;self.action=action;self.known_ids=known_ids
    def run(self):
        try:
            reply=synchronize(ROOT,self.settings,self.pending,self.action,self.known_ids);reply["_sheet_id"]=self.settings.get("sheet_id","samples");reply["_action"]=self.action;self.success.emit(reply,self.pending)
        except Exception as e:self.failed.emit(str(e),self.settings.get("sheet_id","samples"))
class ImportWorker(QThread):
    success=Signal(dict);failed=Signal(str)
    def __init__(self,path,settings):
        super().__init__();self.path=path;self.settings=settings
    def run(self):
        try:self.success.emit(read_document(self.path,self.settings))
        except Exception as e:self.failed.emit(str(e))

class LocalWorkCancelled(Exception):pass

class LocalWorker(QThread):
    progress=Signal(str)
    def __init__(self,root,schema,kind,options=None):
        super().__init__();self.root=root;self.schema=schema;self.kind=kind;self.options=options or {};self.result=None;self.error=None
    def checkpoint(self,message=''):
        if self.isInterruptionRequested() or not ledger_session.active():raise LocalWorkCancelled('已取消；本地记录已保留，未继续上传。')
        if message:self.progress.emit(message)
    def run(self):
        target=None
        try:
            self.checkpoint('正在后台核对…')
            if self.kind=='import':
                payload=read_document(self.options['path'],self.options['settings']);self.checkpoint('正在合并文件中的单元格…')
                id=payload['sheet_id'];target=Store(self.root if id=='samples' else self.root/id,SCHEMAS.get(id));ids=import_document(target,payload)
                self.result={'payload':payload,'ids':ids,'summary':target.last_import_summary}
            else:
                target=Store(self.root,self.schema)
                if self.kind=='review':
                    save_review(target,self.options['item'],self.options['values'],self.options['admin'],self.options.get('target_key'));self.result={}
                elif self.kind=='relocate':
                    self.checkpoint('正在核对原文件的新位置…');relocate_source(target,self.options['old_path'],self.options['new_path']);self.result={}
                else:
                    items=review_items(target)
                    if self.kind=='recheck' or not items or any(item['kind']=='legacy' for item in items):
                        reconcile_sources(target,checkpoint=self.checkpoint)
                    self.checkpoint('正在整理核对结果…')
                    blocked=bool(review_items(target));self.result={'blocked':blocked}
                    if self.kind=='prepare' and not blocked:
                        self.checkpoint('正在回写已确认的单元格…');writeback(target,record_ids=self.options.get('source_ids'))
                        self.checkpoint('文件核对完成，准备同步…')
                        self.result.update(pending=[p for p in target.pending() if self.options.get('source_ids') is None or p['record']['记录ID'] in self.options['source_ids']])
            self.checkpoint()
        except LocalWorkCancelled as e:self.error=('cancel',str(e))
        except SourceFileError as e:self.error=('source',str(e),e.path)
        except Exception as e:self.error=('error',str(e))
        finally:
            if target:target.db.close()

class MainWindow(QMainWindow):
    columns=["记录日期"]+BUSINESS+["数据分类","同步状态","核对提示","详情"]
    overview={"记录日期","牛号","设备号","佩戴开始","佩戴结束","产犊开始","产犊结束","监测目的","九轴","脉搏","温度","样本评价","同步状态","详情"}
    def __init__(self,store,network=True,account=None):
        super().__init__();self.store=store;self.network=network;self.worker=None;self.importer=None;self.local_worker=None;self._local_busy=False;self._after_review_sync=False;self.refreshing=False;self.tree_filter=None;self.manual_sync=False;self.quitting=False;self.next_retry=0;self.failures=0;self.last_connection="尚未连接服务器";self._refresh_pending=False;self._tree_signature=None
        self.account=dict(account or {"username":"local","display_name":"本地","role":"admin"});self.account_worker=None;self._auth_locked=False;self.logout_requested=False
        self.primary_store=store;self.stores={"samples":store,**{id:Store(store.root/id,schema) for id,schema in SCHEMAS.items()}}
        self.sheet_id="samples";self.pending_view=False;self.schema=None;self.cow_key="牛号";self.group_field="数据分类";self._refresh_batch=None;self._sync_context={};self._notices=[]
        self.sample_columns=list(self.columns);self.sample_overview=set(self.overview)
        for id,child in self.stores.items():
            if id!="samples":child.configure({k:v for k,v in store.settings().items() if k in ("host","port","user","farm","wearer","annotator","auto_sync","sync_seconds","minimize_to_tray")})
        self.setAcceptDrops(True);self.setWindowTitle("COWMATA · 现场试验台账 "+VERSION);self.resize(1500,900);self.setMinimumSize(1160,640)
        icon=QIcon(str(ROOT/"assets"/"ledger.ico"));self.setWindowIcon(icon)
        central=QWidget();root=QVBoxLayout(central);root.setContentsMargins(10,8,10,6);root.setSpacing(8);self.setCentralWidget(central)
        heading=QHBoxLayout();heading.setSpacing(8)
        self.directory_button=tool_button("☰","显示或隐藏数据目录",self.toggle_directory);heading.addWidget(self.directory_button)
        identity=QWidget();identity_layout=QHBoxLayout(identity);identity_layout.setContentsMargins(0,0,0,0);identity_layout.setSpacing(8)
        self.brand_icon=QLabel();self.brand_icon.setPixmap(icon.pixmap(72,72));self.brand_icon.setScaledContents(True);self.brand_icon.setFixedSize(36,36);identity_layout.addWidget(self.brand_icon)
        wordmark=QVBoxLayout();wordmark.setSpacing(0);wordmark.addWidget(label("COWMATA","brand"));wordmark.addWidget(label("现场台账 · LEDGER","muted"));identity_layout.addLayout(wordmark);heading.addWidget(identity)
        heading.addStretch(1)
        self.header_actions=QWidget();actions=QHBoxLayout(self.header_actions);actions.setContentsMargins(0,0,0,0);actions.setSpacing(8);self.header_actions.setMaximumWidth(700)
        self.import_button=button("导入表格并同步",self.import_dialog,True);self.import_button.setProperty("mainAction",True);self.import_button.setMinimumWidth(166);self.import_button.setFixedHeight(44)
        self.import_button.setToolTip("选择 Excel 或 CSV 后，扫描新增与修改并立即同步到服务器");actions.addWidget(self.import_button)
        self.new_button=button("＋ 新建",self.new_record);self.new_button.setFixedHeight(44);actions.addWidget(self.new_button)
        self.report_button=button("统计报告",self.report_dialog);self.report_button.setFixedHeight(44);actions.addWidget(self.report_button)
        self.search=QLineEdit();self.search.setPlaceholderText("搜索牛号、设备号、人员…");self.search.setClearButtonEnabled(True);self.search.setMinimumWidth(140);self.search.setFixedHeight(44);self.search.textChanged.connect(self.refresh_table);actions.addWidget(self.search,1);heading.addWidget(self.header_actions,3)
        self.refresh_button=button("刷新服务器",lambda:self.refresh_server());self.refresh_button.setFixedHeight(44);self.refresh_button.setToolTip("读取服务器最新的三张台账，保留本机未提交修改，不上传任何记录");heading.addWidget(self.refresh_button)
        self.summary=ElidedLabel("");self.summary.setObjectName("muted");self.summary.setMinimumWidth(55);self.summary.setMaximumWidth(210);self.summary.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred);heading.addWidget(self.summary,1)
        self.account_button=button(self.account.get("display_name") or self.account["username"],self.account_menu);self.account_button.setFixedHeight(44)
        self.account_button.setToolTip(self.account["username"]+" · "+("管理员" if self.is_admin() else "操作员"));heading.addWidget(self.account_button)
        heading.addWidget(tool_button("⚙","设置",self.settings_dialog));root.addLayout(heading)
        self.sync_button=button("保存修改并同步",lambda:self.sync(True),True);self.sync_button.setProperty("mainAction",True);self.sync_button.setMinimumWidth(166);self.sync_button.setFixedHeight(44);self.sync_button.setToolTip("上传当前 Sheet 中新增或修改的记录；有原文件的修改先同名回写。仅点击后上传。")
        self.update_button=button("检查更新",lambda:getattr(self,"updater",None) and self.updater.check_now());self.update_button.setFixedHeight(32);self.update_button.setToolTip("立即检查并下载签名更新；安装前会保留本地台账")
        self.update_status=ElidedLabel("v"+VERSION+" · 自动更新");self.update_status.setObjectName("muted");self.update_status.setFixedWidth(180);self.update_status.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Preferred)
        self.split=QSplitter();self.split.setHandleWidth(5);root.addWidget(self.split,1)
        self.sidebar=QWidget();side=QVBoxLayout(self.sidebar);side.setContentsMargins(0,0,6,0);side.setSpacing(6)
        side.addWidget(label("数据目录","section"));self.tree=QTreeWidget();self.tree.setHeaderHidden(True);self.tree.setIndentation(14);self.tree.setUniformRowHeights(True);self.tree.setMinimumWidth(150);self.tree.itemClicked.connect(self.filter_tree);side.addWidget(self.tree,1);self.split.addWidget(self.sidebar)
        content=QWidget();body=QVBoxLayout(content);body.setContentsMargins(2,0,0,0);body.setSpacing(6)
        filters=QHBoxLayout();filters.setSpacing(7)
        self.date_scope=QComboBox();self.date_scope.addItems(["全部日期","单日","日期范围"]);self.date_scope.setFixedWidth(104);filters.addWidget(self.date_scope)
        self.range_check=QCheckBox(self);self.range_check.hide()
        from .ledger_report import latest_date
        date=QDate.fromString(latest_date(self.store.all()),"yyyy-MM-dd")
        self.date_from=QDateEdit(date);self.date_to=QDateEdit(date);self.to_label=label("至")
        for w in (self.date_from,self.date_to):w.setCalendarPopup(True);w.setDisplayFormat("yyyy-MM-dd");w.setFixedWidth(120);w.dateChanged.connect(self.date_changed)
        filters.addWidget(self.date_from);filters.addWidget(self.to_label);filters.addWidget(self.date_to)
        self.status_filter=QComboBox();self.status_filter.addItems(["全部状态","待确认","待同步","已同步","待核对","冲突"]);self.status_filter.setFixedWidth(100);self.status_filter.currentIndexChanged.connect(self.refresh_table);filters.addWidget(self.status_filter)
        self.view_mode=QComboBox();self.view_mode.addItems(["概览","原表全部列"]);self.view_mode.setCurrentIndex(1);self.view_mode.setFixedWidth(116);self.view_mode.currentIndexChanged.connect(self.apply_view);filters.addWidget(self.view_mode)
        self.section_filter=QComboBox();self.section_filter.addItems(["佩戴台账","设备管理"]);self.section_filter.setFixedWidth(112);self.section_filter.hide();self.section_filter.currentIndexChanged.connect(self.apply_view);filters.addWidget(self.section_filter)
        filters.addWidget(tool_button("↺","清除筛选",self.clear_filters))
        self.conflict_button=button("核对冲突",self.review_pending_conflicts);self.conflict_button.hide();filters.addWidget(self.conflict_button);filters.addStretch()
        body.addLayout(filters)
        self.table=FitTable(0,len(self.columns));self.table.setHorizontalHeader(SourceHeader(self.table));self.table.setHorizontalHeaderLabels(self.columns)
        for c,key in enumerate(self.columns):
            self.table.horizontalHeaderItem(c).setData(Qt.UserRole,key)
            if key in ("九轴","脉搏","温度"):self.table.horizontalHeaderItem(c).setToolTip(key+"：✓ 有效，× 无效，? 待判定，— 不适用")
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setSelectionMode(QAbstractItemView.ExtendedSelection);self.table.setEditTriggers(QAbstractItemView.DoubleClicked|QAbstractItemView.EditKeyPressed);self.table.setItemDelegate(OptionsDelegate(self.table));self.table.setShowGrid(True);self.table.setSortingEnabled(False);self.table.horizontalHeader().setSectionsClickable(True);self.table.horizontalHeader().sectionClicked.connect(lambda c:(self.table.setSortingEnabled(True),self.table.sortItems(c,Qt.AscendingOrder)))
        self.table.setWordWrap(True);self.table.verticalHeader().setDefaultSectionSize(38);self.table.verticalHeader().setMinimumSectionSize(32);self.table.verticalHeader().hide();self.table.horizontalHeader().setMinimumSectionSize(24);self.table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter);self.table.horizontalHeader().setFixedHeight(34)
        self.table.itemChanged.connect(self.cell_changed);self.table.itemSelectionChanged.connect(self.selection_changed);self.table.cellClicked.connect(self.table_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu);self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.resized.connect(self.fit_columns);self.split.splitterMoved.connect(self.fit_columns);body.addWidget(self.table,1)
        self.count=ElidedLabel("");self.count.setObjectName("muted");self.count.setMaximumHeight(18);body.addWidget(self.count)
        self.review_panel=ReviewPanel(self);body.addWidget(self.review_panel);self.review_panel.saveRequested.connect(self.save_review_item);self.review_panel.locateRequested.connect(self.locate_review_row);self.review_panel.recheckRequested.connect(self.recheck_conflicts);self.review_panel.reconnectRequested.connect(self.reconnect_source);self.review_panel.attentionRequested.connect(lambda message:self.show_feedback("warning","尚未上传",message))
        self.local_progress=QWidget();progress_layout=QHBoxLayout(self.local_progress);progress_layout.setContentsMargins(0,0,0,0);self.local_status=QLabel();progress_layout.addWidget(self.local_status,1);self.cancel_work_button=button("取消核对",self.cancel_local_work);progress_layout.addWidget(self.cancel_work_button);body.addWidget(self.local_progress);self.local_progress.hide()
        self.split.addWidget(content);self.split.setSizes([235,1245]);self.split.setStretchFactor(1,1)
        self.sheet_tabs=QTabBar();self.sheet_tabs.setExpanding(False);self.sheet_tabs.setDrawBase(True);self.sheet_tabs.setStyleSheet("QTabBar::tab{padding:7px 24px;background:#e9f0e5;border:1px solid #d1dfca;}QTabBar::tab:selected{background:white;color:#315c35;font-weight:700;border-bottom:3px solid #76bd57;}")
        for name in SHEET_TITLES:self.sheet_tabs.addTab(name)
        self.operation_card=QFrame();self.operation_card.setObjectName('card');operation_layout=QHBoxLayout(self.operation_card);operation_layout.setContentsMargins(10,6,10,6)
        self.operation_label=QLabel('就绪 · 修改先保存在本地，点击右下角同步后才上传。');self.operation_label.setTextFormat(Qt.PlainText);self.operation_label.setWordWrap(True);self.operation_label.setMinimumHeight(28);operation_layout.addWidget(self.operation_label,1)
        self.operation_detail='尚无本次操作结果。';self.operation_result_button=button('查看结果',self.show_operation_result);operation_layout.addWidget(self.operation_result_button);root.addWidget(self.operation_card)
        footer=QHBoxLayout();footer.setSpacing(12);footer.addWidget(self.sheet_tabs);footer.addStretch();footer.addWidget(self.update_button);footer.addWidget(self.update_status);footer.addWidget(self.sync_button);root.addLayout(footer)
        self.sheet_tabs.currentChanged.connect(self.switch_sheet)
        self.details=label("");self.details.setParent(self);self.details.hide()
        bar=QStatusBar();bar.setSizeGripEnabled(False);bar.setFixedHeight(28);self.setStatusBar(bar)
        self.banner=ElidedLabel("本地台账已就绪");bar.addWidget(self.banner,1)
        self.remote_summary=ElidedLabel("");self.remote_summary.setObjectName("muted");bar.addPermanentWidget(self.remote_summary,2);bar.addPermanentWidget(tool_button("ⓘ","查看同步详情及服务器位置",self.remote_dialog))
        for name in ("remote_address","remote_path","remote_receipt","remote_network"):
            w=label("");w.setParent(self);w.hide();setattr(self,name,w)
        self.range_check.toggled.connect(self.refresh_table);self.date_scope.currentIndexChanged.connect(self.date_scope_changed)
        self.date_scope_changed();self.apply_view();self.update_remote_footer()
        QShortcut(QKeySequence.New,self,activated=self.new_record);QShortcut(QKeySequence.Copy,self.table,activated=self.copy_selected);QShortcut(QKeySequence("Ctrl+D"),self,activated=self.duplicate);QShortcut(QKeySequence.Undo,self,activated=self.undo)
        self.tray=QSystemTrayIcon(icon,self);menu=QMenu();menu.addAction("打开现场台账",self.restore);menu.addAction("同步当前台账修改",lambda:self.sync(True));menu.addAction("退出",self.exit_app);self.tray.setContextMenu(menu);self.tray.activated.connect(lambda reason:self.restore() if reason==QSystemTrayIcon.DoubleClick else None)
        if QSystemTrayIcon.isSystemTrayAvailable() and not EMBEDDED:self.tray.show()
        self.timer=QTimer(self);self.timer.timeout.connect(self.auto_tick);self.timer.start(1000)
        self.refresh()
        self.auth_timer=QTimer(self);self.auth_timer.timeout.connect(self.check_account)
        if network:
            QTimer.singleShot(250,lambda:self.refresh_server(automatic=True))
            # Offline windows (network=False) have no server session to re-verify.
            self.auth_timer.start(1000)
    def is_admin(self):return self.account.get("role")=="admin"
    def account_menu(self):
        menu=QMenu(self)
        identity=menu.addAction(self.account["username"]+" · "+("管理员" if self.is_admin() else "操作员"));identity.setEnabled(False)
        if self.is_admin():menu.addAction("账号管理",self.manage_accounts)
        menu.addSeparator();menu.addAction("退出登录",self.logout)
        menu.exec(self.account_button.mapToGlobal(self.account_button.rect().bottomLeft()))
    def manage_accounts(self):
        if not self.is_admin():return self.error("只有管理员可以管理账号")
        AccountsDialog(ROOT,self.primary_store.settings(),self).exec()
        self.check_account()
    def check_account(self):
        if self._auth_locked:return
        if not ledger_session.active():self.lock_access("登录已过期，请重新登录");return
        from cowmata_security.client import current_session
        s=current_session()
        if s.clock()-s.last_refresh<60:return
        if self.account_worker and self.account_worker.isRunning():return
        self.account_worker=AccountWorker(ROOT,self.primary_store.settings(),"whoami",parent=self)
        self.account_worker.success.connect(self.account_checked);self.account_worker.failed.connect(self.account_check_failed);self.account_worker.start()
    def account_checked(self,reply):
        if reply.get("user",{}).get("username")!=self.account["username"]:
            self.lock_access("登录身份不一致，请重新登录");return
        self.account=dict(reply["user"]);self.account_button.setText(self.account.get("display_name") or self.account["username"])
        self.account_button.setToolTip(self.account["username"]+" · "+("管理员" if self.is_admin() else "操作员"))
    def account_check_failed(self,message):
        if message.startswith("AUTH_REQUIRED"):self.lock_access(message)
    def lock_access(self,message):
        if self._auth_locked:return
        self.cancel_local_work();self.review_panel.hide();self._auth_locked=True;self._refresh_batch=None;self.timer.stop();self.auth_timer.stop();self.hide();ledger_session.clear()
        QTimer.singleShot(0,lambda:self.relogin(message))
    def relogin(self,message):
        if self._local_busy:
            QTimer.singleShot(100,lambda:self.relogin(message));return
        dialog=LoginDialog(ROOT,self.primary_store.settings(),self,username=self.account["username"]);dialog.hint.setText(message)
        if dialog.exec()==QDialog.Accepted:
            self.account=dict(dialog.reply["user"]);self._auth_locked=False;self.account_checked(dialog.reply);self.show()
            self.timer.start(1000);self.auth_timer.start(1000);self.next_retry=0
            QTimer.singleShot(250,lambda:self.refresh_server(automatic=True))
        else:self.logout_requested=True;self.close_when_idle()
    def logout(self):
        self.cancel_local_work()
        from cowmata_security.login_preferences import LoginPreferences
        LoginPreferences("ledger").disable_auto_login()
        self._auth_locked=True;self._refresh_batch=None;self.timer.stop();self.auth_timer.stop();self.hide()
        if any(worker and worker.isRunning() for worker in (self.worker,self.importer,self.local_worker,self.account_worker)):
            QTimer.singleShot(100,self.logout);return
        self.account_worker=AccountWorker(ROOT,self.primary_store.settings(),"logout",parent=self)
        self.account_worker.finished.connect(self.logged_out);self.account_worker.start()
    def logged_out(self):
        ledger_session.clear();self.logout_requested=True;QTimer.singleShot(0,self.close_when_idle)
    def close_when_idle(self):
        if any(worker and worker.isRunning() for worker in (self.worker,self.importer,self.local_worker,self.account_worker)):
            QTimer.singleShot(100,self.close_when_idle);return
        self.exit_app()
    def update_sheet_tabs(self):
        for i,(id,name) in enumerate([("samples","样本试验台账"),("calving","产犊登记"),("equipment","设备台账")]):
            self.sheet_tabs.setTabText(i,f"{name}  {len(self.stores[id].all())}")
        pending=sum(classify_record(r)[0]=="pending_calving" for r in self.stores["samples"].all())
        self.sheet_tabs.setTabText(3,f"{SHEET_TITLES[3]}  {pending}")
    def row_group(self,row):
        # Samples are grouped by the shared rule, so a stale stored category never misfiles a row.
        return row.get(self.group_field,"") if self.schema else classify_record(row)[0]
    def visible_rows(self):
        rows=self.store.all()
        return [r for r in rows if self.row_group(r)=="pending_calving"] if self.pending_view and not self.schema else rows
    def switch_sheet(self,index):
        self.review_panel.hide()
        id=["samples","calving","equipment","samples"][index];pending=index==3
        if id==self.sheet_id:
            if pending!=self.pending_view:
                self.pending_view=pending;self._tree_signature=None;self.tree_filter=None;self.refresh()
                self.banner.setText(SHEET_TITLES[index]+f" · {len(self.visible_rows())} 条")
            return
        self.pending_view=pending
        focus=QApplication.focusWidget()
        if focus and self.table.isAncestorOf(focus):focus.clearFocus()
        self.sheet_id=id;self.store=self.stores[id];self.schema=get_schema(id)
        self.cow_key=self.schema.cow_field if self.schema else "牛号";self.group_field="工作表" if self.schema else "数据分类"
        self.columns=["记录日期"]+self.schema.business+["同步状态","核对提示","详情"] if self.schema else list(self.sample_columns)
        self.table.blockSignals(True);self.table.setSortingEnabled(False);self.table.clear();self.table.setRowCount(0);self.table.setColumnCount(len(self.columns));self.table.setHorizontalHeaderLabels(self.columns)
        for c,key in enumerate(self.columns):self.table.horizontalHeaderItem(c).setData(Qt.UserRole,key);self.table.horizontalHeaderItem(c).setToolTip(key)
        self.table.blockSignals(False);self.table.setSortingEnabled(False)
        self._tree_signature=None;self.tree_filter=None;self.section_filter.setVisible(id=="equipment");self.view_mode.setVisible(id=="samples")
        self.search.setText("");self.date_scope.setCurrentIndex(0);self.status_filter.setCurrentIndex(0)
        from .ledger_report import latest_date
        self.date_from.setDate(QDate.fromString(latest_date(self.store.all()),"yyyy-MM-dd"));self.date_to.setDate(self.date_from.date())
        self.apply_view();self.refresh()
        self.banner.setText(SHEET_TITLES[index]+f" · {len(self.visible_rows())} 条")

    def toggle_directory(self):
        self.sidebar.setVisible(not self.sidebar.isVisible());QTimer.singleShot(0,self.fit_columns)
    def date_scope_changed(self,*_):
        mode=self.date_scope.currentIndex();self.date_from.setVisible(mode>0);self.date_to.setVisible(mode==2);self.to_label.setVisible(mode==2)
        self.range_check.setChecked(mode>0);self.date_changed()
    def date_changed(self,*_):
        if self.date_scope.currentIndex()==1:
            self.date_to.blockSignals(True);self.date_to.setDate(self.date_from.date());self.date_to.blockSignals(False)
        if hasattr(self,"table"):self.refresh_table()
    def apply_view(self,*_):
        if not hasattr(self,"table"):return
        overview=self.view_mode.currentIndex()==0
        selected=set(visible_fields(self.schema,self.section_filter.currentText()))|{"同步状态","详情"} if self.schema else self.sample_overview if overview else set(BUSINESS+[k for k in self.columns if k in ("配种开始","配种结束")]+["同步状态","详情"])
        wrapped={"牛场登记生产时间":"牛场登记\n生产时间","提前预产期天数":"提前预产期\n天数","是否佩戴过尾环":"是否佩戴\n过尾环","是否处于有效监测范围":"是否处于有效\n监测范围","备注(最后一次佩戴时间）":"备注（最后一次佩戴时间）","拆除时间(掉落）":"拆除时间\n（掉落）","佩戴时长（天）":"佩戴时长\n（天）"}
        header=self.table.horizontalHeader()
        ordered=(visible_fields(self.schema,self.section_filter.currentText())+["同步状态","详情"]) if self.schema else list(self.columns)
        for visual,key in enumerate(ordered):header.moveSection(header.visualIndex(self.columns.index(key)),visual)
        for c,key in enumerate(self.columns):
            self.table.setColumnHidden(c,key not in selected)
            self.table.horizontalHeaderItem(c).setText("状态" if key=="同步状态" else key)
            self.table.horizontalHeaderItem(c).setToolTip(key)
        self.table.horizontalHeader().original_sample=not self.schema and not overview
        self.table.horizontalHeader().setFixedHeight(58 if not self.schema and not overview else 42)
        self.table.horizontalHeader().viewport().update()
        self.refresh_table();self.fit_columns()
    def fit_columns(self,*_):
        if not hasattr(self,"table"):return
        widths={"记录日期":96,"序号":54,"牛号":88,"预产期":98,"设备号":126,"佩戴开始":100,"佩戴结束":100,"产犊开始":100,"产犊结束":100,"监测目的":100,"九轴":52,"脉搏":52,"温度":52,"样本评价":110,"尾环佩戴人":100,"事件标注人":100,"同步状态":36,"详情":34}
        widths.update({"生产日期":100,"牛场登记生产时间":90,"提前预产期天数":88,"是否佩戴过尾环":90,"是否处于有效监测范围":106,"备注(最后一次佩戴时间）":185,"日期":100,"新佩戴牛号":100,"设备编码":126,"拆除时间(掉落）":122,"佩戴时长（天）":90,"设备去向":98,"库存数量":70,"备注":180,"设备来源":145,"设备数量":70,"设备号":175,"故障数量":70,"故障设备编码":150,"故障率(%)":88,"遗失设备":120})
        if not self.schema:widths["设备号"]=126
        visible=[self.table.horizontalHeader().logicalIndex(v) for v in range(len(self.columns)) if not self.table.isColumnHidden(self.table.horizontalHeader().logicalIndex(v))]
        minimum=sum(widths.get(self.columns[c],90) for c in visible);extra=max(0,self.table.viewport().width()-minimum)
        weights={c:(4 if self.columns[c] in ("样本评价","备注","备注(最后一次佩戴时间）") else 1 if self.columns[c] not in ("九轴","脉搏","温度","同步状态","详情") else 0) for c in visible};total=sum(weights.values()) or 1;used=0
        for c in visible:
            width=widths.get(self.columns[c],90)+int(extra*weights[c]/total);self.table.setColumnWidth(c,width);used+=width
        if visible and extra:self.table.setColumnWidth(visible[-1],self.table.columnWidth(visible[-1])+self.table.viewport().width()-used)
    def table_clicked(self,row,column):
        if self.columns[column]=="详情":self.record_details()
    def remote_dialog(self):
        dialog=QDialog(self);dialog.setWindowTitle("同步详情");dialog.resize(660,380);layout=QVBoxLayout(dialog)
        for w in (self.remote_address,self.remote_path,self.remote_receipt,self.remote_network):
            text=label(w.text());text.setWordWrap(True);text.setTextInteractionFlags(Qt.TextSelectableByMouse);layout.addWidget(text)
        layout.addStretch();row=QHBoxLayout();row.addWidget(button("复制服务器信息",self.copy_remote));row.addWidget(button("处理同步冲突",lambda:(dialog.accept(),self.conflicts_dialog())));row.addStretch();row.addWidget(button("关闭",dialog.accept));layout.addLayout(row);dialog.exec()
    def record_details(self):
        row=self.selected()
        if not row:return
        dialog=QDialog(self);dialog.setWindowTitle("台账详情 · "+(row.get(self.cow_key) or row.get("工作表","")));dialog.resize(900,630)
        layout=QVBoxLayout(dialog);heading=label((row.get(self.cow_key) or row.get("工作表",""))+"  ·  "+row["记录日期"],"heading");layout.addWidget(heading)
        if row.get("核对提示"):
            issue=label("需核对："+row["核对提示"]);issue.setWordWrap(True);issue.setStyleSheet("color:#a74721");layout.addWidget(issue)
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();grid=QGridLayout(body)
        for i,key in enumerate((visible_fields(self.schema,row.get("工作表"))+["记录日期","牧场","记录类型"]) if self.schema else ["记录日期"]+BUSINESS+["牧场","数据分类"]):
            value=CATEGORIES.get(row.get(key),row.get(key,"")) if key=="数据分类" else row.get(key,"")
            text=label(value or "—");text.setTextFormat(Qt.PlainText);text.setTextInteractionFlags(Qt.TextSelectableByMouse);text.setWordWrap(True)
            grid.addWidget(label(key,"muted"),i//2,(i%2)*2);grid.addWidget(text,i//2,(i%2)*2+1)
        grid.setColumnStretch(1,1);grid.setColumnStretch(3,1);scroll.setWidget(body);layout.addWidget(scroll,1)
        more=QToolButton();more.setText("▸ 来源与归类信息");more.setCheckable(True);layout.addWidget(more)
        source=QPlainTextEdit("来源："+(row.get("来源") or "软件录入")+"\n归类目录："+row.get("归类目录","")+"\n时间说明："+row.get("时间说明",""));source.setReadOnly(True);source.setMaximumHeight(110);source.hide();layout.addWidget(source);more.toggled.connect(source.setVisible)
        footer=QHBoxLayout();footer.addWidget(button("编辑记录",lambda:(dialog.accept(),self.edit_record()),True));footer.addWidget(button("续填同一头牛",lambda:(dialog.accept(),self.duplicate())));footer.addStretch();footer.addWidget(button("关闭",dialog.accept));layout.addLayout(footer);dialog.exec()
    def update_remote_footer(self,confirmed=None,error=None):
        s=self.store.settings()
        self.remote_address.setText(f"公网服务器：{s['host']}:{s['port']}    ·    增量同步 · 公网直连 · SSH 加密上传    ·    账户：{s['user']}")
        connection=(confirmed or {}).get("connection") or s.get("last_direct_connection")
        line="上传线路：仅使用真实网卡，不走系统代理或VPN虚拟网卡"
        if connection and connection.get("server")==s["host"] and connection.get("port")==s["port"]:
            line=f"最近同步线路：本机 {connection['local_address']} → 本地网关 {connection['gateway']} → 服务器公网 · 直连"
        self.remote_network.setText(line);self.remote_network.setWordWrap(True)
        path=(confirmed or {}).get("target") or s.get("last_confirmed_target") or s["server_file"]
        self.remote_path.setText("服务器端保存位置："+path)
        self.remote_summary.setText(f"公网 {s['host']}:{s['port']} → {path}")
        held=sum(r["_held"] for r in self.store.all(True));pending=len(self.store.pending())
        if error:
            status="本次未同步成功："+error
        elif confirmed:
            status="服务器已确认保存并通过SHA256校验  ·  "+s.get("last_confirmed_at",now())
        elif s.get("last_confirmed_at"):
            status="最近服务器确认："+s["last_confirmed_at"]
        else:status="以上为配置的远端目标，等待服务器首次确认"
        self.remote_receipt.setText(status+f"  ·  待上传 {pending} 条  ·  暂缓上传 {held} 条")
        self.remote_receipt.setWordWrap(True)
    def copy_remote(self):
        s=self.store.settings();QApplication.clipboard().setText(f"公网服务器：{s['host']}:{s['port']}\n上传线路：真实网卡公网直连，不使用代理\n服务器端保存位置：{s.get('last_confirmed_target') or s['server_file']}\n最近服务器确认：{s.get('last_confirmed_at','尚未确认')}")
        self.statusBar().showMessage("已复制公网服务器和远端CSV完整位置",5000)
    def restore(self):self.showNormal();self.raise_();self.activateWindow()

    def error(self,error):QMessageBox.warning(self,"需要处理",str(error))
    def refresh(self):
        if self.sheet_id=='samples':
            extras=list(dict.fromkeys(k for row in self.store.all() for k in extra_values(row)))
            expected=[k for k in self.sample_columns if k not in ('同步状态','核对提示','详情')]+extras+['同步状态','核对提示','详情']
            if self.columns!=expected:
                self.columns=expected;self.refreshing=True;self.table.setRowCount(0);self.table.setColumnCount(len(expected));self.table.setHorizontalHeaderLabels(expected)
                for c,key in enumerate(expected):self.table.horizontalHeaderItem(c).setData(Qt.UserRole,key)
                self.refreshing=False;self.apply_view()
        self.refresh_tree();self.refresh_table();self.update_sheet_tabs()
        conflicts=source_conflict_count(self.store)+sum(bool(r["_conflict"]) for r in self.store.all(True))
        self.conflict_button.setVisible(bool(conflicts));self.conflict_button.setText(f"核对冲突 ({conflicts})")
        if hasattr(self,"remote_receipt"):self.update_remote_footer()
    def refresh_tree(self):
        rows=self.visible_rows()
        signature=tuple(sorted((r["记录ID"],r["牧场"],self.row_group(r),r["记录日期"]) for r in rows))+(self.pending_view,)
        if signature==self._tree_signature:return
        initial=not self._tree_signature;self._tree_signature=signature
        expanded=set();iterator=QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item=iterator.value()
            if item.isExpanded():expanded.add(tuple(item.data(0,Qt.UserRole) or ()))
            iterator+=1
        scroll=self.tree.verticalScrollBar().value()
        self.tree.blockSignals(True);self.tree.clear()
        folder=self.style().standardIcon(QStyle.SP_DirIcon);file=self.style().standardIcon(QStyle.SP_FileIcon)
        def add(parent,text,key,leaf=False):
            item=QTreeWidgetItem(parent,[text]);item.setData(0,Qt.UserRole,key);item.setIcon(0,file if leaf else folder);item.setToolTip(0,text)
            if tuple(key or ()) in expanded or initial and not leaf:item.setExpanded(True)
            if key==self.tree_filter:self.tree.setCurrentItem(item)
            return item
        root=add(self.tree,f"全部台账  {len(rows)} 条",None)
        farms={}
        for row in rows:farms.setdefault(row["牧场"],[]).append(row)
        for farm,items in sorted(farms.items()):
            parent=add(root,f"{farm or '未填写牧场'}  {len(items)} 条",("farm",farm));cats={}
            for row in items:cats.setdefault(self.row_group(row),[]).append(row)
            for code,records in sorted(cats.items()):
                category=add(parent,f"{CATEGORIES.get(code,'待分类') if not self.schema else code}  {len(records)} 条",("category",farm,code));dates={}
                for row in records:dates[row["记录日期"]]=dates.get(row["记录日期"],0)+1
                for date,count in sorted(dates.items(),reverse=True):
                    add(category,f"{date or '日期待补充'}  ·  {count} 条",("date",farm,code,date),True)
        root.setExpanded(True);self.tree.blockSignals(False);self.tree.verticalScrollBar().setValue(scroll)
    def refresh_table(self,*_):
        if self.refreshing:return
        if self.table.state()==QAbstractItemView.EditingState:self._refresh_pending=True;return
        self._refresh_pending=False;self.refreshing=True
        try:
            rows=self.visible_rows();s=self.search.text().strip().lower();filtered=[]
            scroll=self.table.verticalScrollBar().value()
            for row in rows:
                if self.sheet_id=="equipment" and row.get("工作表")!=self.section_filter.currentText():continue
                if self.tree_filter:
                    f=self.tree_filter
                    if row["牧场"]!=f[1]:continue
                    if len(f)>2 and self.row_group(row)!=f[2]:continue
                    if len(f)>3 and row["记录日期"]!=f[3]:continue
                if s and s not in " ".join(str(row.get(k,"")) for k in (self.schema.fields if self.schema else BUSINESS+EXTRA)).lower():continue
                if self.range_check.isChecked() and not self.date_from.date().toString("yyyy-MM-dd")<=row["记录日期"]<=self.date_to.date().toString("yyyy-MM-dd"):continue
                status=self.status_filter.currentText()
                if status=="待确认" and not row["_held"]:continue
                if status=="待同步" and (not row["_dirty"] or row["_held"]):continue
                if status=="已同步" and row["_dirty"]:continue
                if status=="待核对" and not row["核对提示"]:continue
                if status=="冲突" and not row["_conflict"]:continue
                filtered.append(row)
            from .ledger_files import slot
            filtered.sort(key=lambda r:(r.get("来源","").split(" / ")[0],slot(r),r["记录ID"]))
            selected=set(self.selected_ids());sort_col=self.table.horizontalHeader().sortIndicatorSection();sort_order=self.table.horizontalHeader().sortIndicatorOrder()
            sorting=self.table.isSortingEnabled();self.table.setSortingEnabled(False)
            wanted={r["记录ID"] for r in filtered}
            for old_n in range(self.table.rowCount()-1,-1,-1):
                cell=self.table.item(old_n,0)
                if cell is None or cell.data(Qt.UserRole) not in wanted:self.table.removeRow(old_n)
            positions={self.table.item(n,0).data(Qt.UserRole):n for n in range(self.table.rowCount())}
            for desired,row in enumerate(filtered):
                key=row['记录ID'];current=positions.get(key)
                if current is None:
                    destination=self.table.rowCount() if sorting else desired
                    self.table.insertRow(destination)
                    positions={k:(n+1 if n>=destination else n) for k,n in positions.items()};positions[key]=destination
                elif not sorting and current!=desired:
                    # Move existing cell objects only when the source row order actually changed.
                    height=self.table.rowHeight(current)
                    cells=[self.table.takeItem(current,c) for c in range(self.table.columnCount())]
                    self.table.removeRow(current);self.table.insertRow(desired)
                    for c,item in enumerate(cells):
                        if item is not None:self.table.setItem(desired,c,item)
                    self.table.setRowHeight(desired,height)
                    shifted={k:(n-1 if n>current else n) for k,n in positions.items() if k!=key}
                    positions={k:(n+1 if n>=desired else n) for k,n in shifted.items()};positions[key]=desired
            dates=sorted({r["记录日期"] for r in filtered});bands={d:i%2 for i,d in enumerate(dates)}
            for row in filtered:
                n=positions[row["记录ID"]]
                marks={}
                if self.schema:
                    try:marks={v.get("field"):v for v in json.loads(row.get("原始单元格") or "{}").values()}
                    except (ValueError,AttributeError):pass
                for c,key in enumerate(self.columns):
                    raw=CATEGORIES.get(self.row_group(row),"待分类") if key=="数据分类" else ("冲突" if row["_conflict"] else "待确认" if row["_held"] else "待同步" if row["_dirty"] else "已同步") if key=="同步状态" else row.get(key,"")
                    text=display_value(row,key) if key in (self.schema.business if self.schema else BUSINESS+["配种开始","配种结束"]) else raw
                    if not self.schema and self.view_mode.currentIndex()==0:
                        if key in ("九轴","脉搏","温度"):text={"有效":"✓","无效":"×","/":"—","":"?"}.get(raw,"?")
                        if key in ("佩戴开始","佩戴结束","产犊开始","产犊结束"):text=raw.replace(" ","\n",1)
                        if key=="同步状态":text="!" if row["_conflict"] or row.get("核对提示") else "◷" if row["_dirty"] else "✓"
                    if key=="同步状态":text="!" if row["_conflict"] else "◷" if row["_dirty"] else "✓"
                    if key=="详情":text="⋯"
                    signature=(text,raw,row.get("核对提示"),row.get("_conflict"),row.get("_dirty"),bands.get(row["记录日期"],0),str(marks.get(key,{})))
                    item=self.table.item(n,c)
                    if item is not None and item.data(Qt.UserRole+2)==signature:continue
                    if item is None:item=QTableWidgetItem();self.table.setItem(n,c,item)
                    item.setText(text);item.setData(Qt.UserRole,row["记录ID"]);item.setData(Qt.UserRole+1,raw);item.setData(Qt.UserRole+2,signature)
                    item.setToolTip("点击查看完整记录" if key=="详情" else (raw+"\n"+row["核对提示"]).strip() if key=="同步状态" else raw)
                    if key in ("同步状态","核对提示","详情"):item.setFlags(item.flags()&~Qt.ItemIsEditable)
                    item.setForeground(QColor("#26392d"))
                    item.setTextAlignment((Qt.AlignLeft if key=="样本评价" else Qt.AlignCenter)|Qt.AlignVCenter)
                    item.setBackground(QColor("#ffffff" if bands.get(row["记录日期"],0) else "#f5faf1"))
                    if key in ["记录日期","同步状态"]+([self.schema.date_field] if self.schema else []):
                        item.setForeground(QColor("#177f95" if key=="记录日期" else "#a85c17" if row["_dirty"] else "#478327"))
                    if row["_conflict"] or (key=="核对提示" and text):item.setForeground(QColor("#ad3c29"))
                    if row.get("核对提示") and key in row["核对提示"]:
                        item.setBackground(QColor("#fff0e5"));item.setForeground(QColor("#a74721"))
                    if key in ["九轴","脉搏","温度"] and raw=="无效":item.setForeground(QColor("#b84935"))
                    if key in ("九轴","脉搏","温度") and raw=="有效":item.setForeground(QColor("#388341"))
                    rgb=marks.get(key,{}).get("font_rgb","")
                    if len(rgb)==8 and int(rgb[-6:-4],16)>150 and int(rgb[-4:-2],16)<100:
                        item.setForeground(QColor("#"+rgb[-6:]));item.setToolTip(item.toolTip()+"\n原表标红，已保留")
                if row["记录ID"] in selected:
                    for c in range(len(self.columns)):self.table.item(n,c).setSelected(True)
            self.table.setSortingEnabled(sorting)
            if sorting and sort_col>=0:self.table.sortItems(sort_col,sort_order)
            from .ledger_report import validity
            valid=sum(validity(r)=="有效" for r in filtered);review=sum(bool(r["核对提示"]) for r in filtered)
            self.summary.setText(f"{len(filtered)} 条 · 有效 {valid} · 待核对 {review}" if not self.schema else f"{len(filtered)} 条 · 需核对 {review}")
            if self.sheet_id=="calving":self.summary.setText(f"{len(filtered)} 条 · 有效监测 {sum(r.get('是否处于有效监测范围','').strip()=='是' for r in filtered)} · 需核对 {review}")
            self.summary.setToolTip("按当前筛选统计；有效为九轴、脉搏、温度三项均有效。" if not self.schema else "按原表字段统计，可疑值保留原文并提示核对。")
            conflicts=sum(bool(r["_conflict"]) for r in self.store.all(True))
            self.count.setText(f"显示 {len(filtered)} / {len(rows)} 条 · {len(dates)} 个日期"+(f" · {conflicts} 条冲突" if conflicts else "")+"    双击单元格编辑 · ... 查看详情 · 右键操作")
            self.table.verticalScrollBar().setValue(scroll);QTimer.singleShot(0,self.fit_columns)
        finally:self.refreshing=False
    def selected_ids(self):
        return list(dict.fromkeys(self.table.item(i.row(),0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows() if self.table.item(i.row(),0)))
    def selected(self):
        ids=self.selected_ids();return self.store.get(ids[0]) if ids else None
    def selection_changed(self):
        row=self.selected()
        if row:self.details.setText("归类目录："+row["归类目录"]+"\n"+(row["来源"] or "软件录入")+"  ·  "+("待核对："+row["核对提示"] if row["核对提示"] else "时间采用北京时间"))
    def filter_tree(self,item,*_):
        self.tree_filter=item.data(0,Qt.UserRole)
        if self.sheet_id=="equipment" and self.tree_filter and len(self.tree_filter)>2:self.section_filter.setCurrentText(self.tree_filter[2])
        if self.tree_filter and len(self.tree_filter)>3:
            date=QDate.fromString(self.tree_filter[3],"yyyy-MM-dd")
            if date.isValid():self.date_from.setDate(date);self.date_scope.setCurrentIndex(1);self.date_changed()
        else:self.date_scope.setCurrentIndex(0)
        self.refresh_table()
    def calendar_filter(self,date):
        self.tree_filter=None;self.date_from.setDate(date);self.date_scope.setCurrentIndex(1);self.date_changed()
    def clear_filters(self):
        self.tree_filter=None;self.search.clear();self.date_scope.setCurrentIndex(0);self.range_check.setChecked(False);self.status_filter.setCurrentIndex(0)
        if self.tree.topLevelItem(0):self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.refresh_table()
    def new_record(self):
        if self._local_busy:return
        if self.schema:
            row=self.schema.empty(self.store.settings(),self.section_filter.currentText() if self.sheet_id=="equipment" else "产犊登记")
            row["记录类型"]="产犊" if self.sheet_id=="calving" else "设备批次" if row["工作表"]=="设备管理" else "佩戴"
            self.record_dialog(row);return
        row=empty_record(self.store.settings());row["序号"]=str(max([int(r["序号"]) for r in self.store.all() if r["序号"].isdigit()]+[0])+1)
        if self.range_check.isChecked() and self.date_from.date()==self.date_to.date():row["记录日期"]=self.date_from.date().toString("yyyy-MM-dd")
        self.record_dialog(row)
    def edit_record(self):
        if self._local_busy:return
        row=self.selected()
        if not row:return self.error("请先选择一条记录")
        if row["_conflict"]:return self.conflicts_dialog()
        self.record_dialog(row)
    def record_dialog(self,row):
        if self._local_busy:return
        dialog=SheetRecordDialog(row,self.schema,self) if self.schema else RecordDialog(row,self.store.settings(),self)
        if dialog.exec()==QDialog.Accepted:
            try:self.store.save(dialog.result_row,not bool(self.schema));self.saved()
            except Exception as e:self.error(e)
    def duplicate(self):
        if self._local_busy:return
        old=self.selected()
        if not old:return self.error("请先选择一条记录")
        if self.schema:
            row=self.schema.empty(self.store.settings(),old.get("工作表"));row["记录类型"]=old.get("记录类型","数据")
            for key in (self.cow_key,"设备编码","设备来源"):row[key]=old.get(key,"")
            self.record_dialog(row);return
        row=empty_record(self.store.settings())
        for key in ["序号","牛号","预产期","设备号","牧场","现场标记","数据分类","监测目的","尾环佩戴人","事件标注人"]:row[key]=old[key]
        self.record_dialog(row)
    def saved(self):
        self.show_feedback('warning','已保存本地 · 未上传','请点击右下角保存修改并同步，等待服务器确认。',False)
        self.refresh();self.banner.setText(self.store.last_export_error or ("记录已保存到本地 · 核对后点击“保存修改并同步”" if any(r["_held"] for r in self.store.all(True)) else "修改已保存在本地 · 点击“保存修改并同步”后回写原文件并上传"));self.next_retry=0
    def cell_changed(self,item):
        if self.refreshing or self._local_busy:return
        key=self.columns[item.column()];row=self.store.get(item.data(Qt.UserRole))
        if not row:return
        value=item.text()
        if key in extra_values(row):
            try:
                edit_extra(row,key,value);row['数据分类']=classify_record(row)[0];self.store.save(row,False);self.saved()
            except Exception as e:self.error(e);self.refresh_table()
            return
        if self.schema:
            if key not in self.schema.business and key!="记录日期":return
            if row.get(key)==value:return
            row[key]=value;row=self.schema.normalize(row);row["核对提示"]="；".join(self.schema.issues(row))
            try:self.store.save(row,False);self.saved()
            except Exception as e:self.error(e);self.refresh_table()
            return
        if key=="数据分类":value=next((code for code,name in CATEGORIES.items() if name==value),"")
        if row.get(key)==value:return
        row[key]=value
        try:
            if key in ["记录日期","预产期","佩戴开始","佩戴结束","产犊开始","产犊结束"]:row[key]=normalize_time(value,key in ["记录日期","预产期"])
            if key=="设备号":
                row[key]=value.upper().replace(":","").replace("-","")
                if not re.fullmatch("[0-9A-F]{12}",row[key]):raise ValueError("设备号应为12位十六进制")
            if key=="牛号" and not value.strip():raise ValueError("牛号不能为空")
            row["数据分类"]=classify_record(row)[0]
            new_issues=issues(row);old_issues=issues(self.store.get(row["记录ID"]))
            extra=[x for x in new_issues if x not in old_issues]
            if extra:raise ValueError("；".join(extra))
            row["核对提示"]="；".join(new_issues);self.store.save(row,False);self.saved()
        except Exception as e:self.error(e);self.refresh_table()
    def copy_selected(self):
        ids=self.selected_ids()
        if not ids:return
        out=io.StringIO(newline="");writer=csv.writer(out,delimiter="\t",lineterminator="\r\n")
        fields=visible_fields(self.schema,self.section_filter.currentText()) if self.schema else BUSINESS
        writer.writerow(fields)
        for key in ids:writer.writerow([display_value(self.store.get(key),f) for f in fields])
        QApplication.clipboard().setText(out.getvalue());self.statusBar().showMessage(f"已复制 {len(ids)} 行，可直接粘贴到 Excel",6000)
    def context_menu(self,point):
        menu=QMenu(self);menu.addAction("查看完整记录",self.record_details);menu.addAction("编辑整条记录",self.edit_record);menu.addAction("续填同一头牛",self.duplicate);menu.addAction("复制选中行（含Excel表头）",self.copy_selected);menu.addAction("复制归类目录",lambda:QApplication.clipboard().setText(self.selected()["归类目录"]) if self.selected() else None);menu.addSeparator();menu.addAction("撤销上次修改",self.undo);menu.addAction("导出CSV总表",self.export_dialog);action=menu.addAction("删除选中",self.delete_selected);action.setEnabled(self.is_admin());menu.exec(self.table.viewport().mapToGlobal(point))
    def delete_selected(self):
        if self._local_busy:return
        if not self.is_admin():return self.error("只有管理员可以删除台账")
        ids=self.selected_ids()
        if ids and QMessageBox.question(self,"删除记录",f"删除所选 {len(ids)} 条？可以撤销；同步后总表将标记为已删除。")==QMessageBox.Yes:
            try:self.store.delete(ids);self.saved()
            except Exception as e:self.error(e)
    def undo(self):
        if self._local_busy:return
        try:self.store.undo();self.saved()
        except Exception as e:self.error(e)
    def report_dialog(self):
        from .ledger_report_ui import ReportDialog
        ReportDialog(self.store,self).exec()

    def import_dialog(self):
        if self._local_busy or self.importer and self.importer.isRunning():return
        if self._refresh_batch or self.worker and self.worker.isRunning():return
        path,_=QFileDialog.getOpenFileName(self,"导入表格并同步 · 选择 Excel 或 CSV","","试验台账 (*.xlsx *.csv)")
        if path:self.start_import(path)
    def dragEnterEvent(self,event):
        urls=event.mimeData().urls()
        if len(urls)==1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).suffix.lower() in (".xlsx",".csv"):event.acceptProposedAction()
    def dropEvent(self,event):
        if self.importer and self.importer.isRunning():return
        urls=event.mimeData().urls()
        if len(urls)==1 and urls[0].isLocalFile():
            self.start_import(urls[0].toLocalFile());event.acceptProposedAction()
    def start_import(self,path,direct=True):
        if self._local_busy or self.worker and self.worker.isRunning():return
        self.import_direct=direct
        self.start_local_work('import',self.primary_store,{'path':path,'settings':self.store.settings()})
    def import_failed(self,message):self.banner.setText('导入未完成：'+message);self.error(message)
    def import_preview(self,result):
        payload=result['payload'];id=payload['sheet_id'];target=self.stores[id];ids=result['ids'];summary=result['summary'];target.last_import_summary=summary
        self.sheet_tabs.setCurrentIndex(['samples','calving','equipment'].index(id));self.clear_filters();self.refresh()
        self.banner.setText(f"已导入：新增 {summary['added']}，更新 {summary['updated']}，重复跳过 {summary['skipped']}")
        notices=payload.get('notices') or []
        if notices:
            self.banner.setText(self.banner.text()+" · 已兼容原表格式："+notices[0]+(f"（另有 {len(notices)-1} 项，悬停查看）" if len(notices)>1 else ""))
            self.banner.setToolTip("\n".join(notices))
        else:self.banner.setToolTip("")
        if review_items(target):self.review_pending_conflicts();return
        if self.import_direct:QTimer.singleShot(0,lambda:self.sync(True,sheet_id=id,source_ids=set(ids),source_name=Path(payload['path']).name))
    def review_file_conflicts(self,target,path):
        self.review_pending_conflicts();return []
    def approve_review(self):
        held=[r for r in self.store.all(True) if r["_held"]]
        if not held:return True
        good=[r for r in held if not r["核对提示"]];bad=[r for r in held if r["核对提示"]]
        dialog=QDialog(self);dialog.setWindowTitle("确认后同步");dialog.resize(850,560);layout=QVBoxLayout(dialog)
        layout.addWidget(label(f"待确认 {len(held)} 条 · 正常 {len(good)} 条 · 异常 {len(bad)} 条","heading"))
        description=label("请核对下面的记录。确认后才会上传到服务器。","muted");layout.addWidget(description)
        table=QTableWidget(len(held),4);table.setHorizontalHeaderLabels(["牛号","记录日期","Excel来源","核对提示"]);table.setEditTriggers(QAbstractItemView.NoEditTriggers);table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for n,row in enumerate(held):
            for c,key in enumerate(["牛号","记录日期","来源","核对提示"]):
                item=QTableWidgetItem(row.get(key,""));item.setToolTip(row.get(key,""))
                if row["核对提示"]:item.setForeground(QColor("#a74721"))
                table.setItem(n,c,item)
        layout.addWidget(table,1)
        preserve=QCheckBox("异常记录已人工核实，确认保留原值一并同步");preserve.setVisible(bool(bad));layout.addWidget(preserve)
        confirm=button("确认正常记录并同步",dialog.accept,True);confirm.setEnabled(bool(good))
        preserve.toggled.connect(lambda checked:(confirm.setEnabled(checked or bool(good)),confirm.setText("确认全部并同步" if checked else "确认正常记录并同步")))
        layout.addWidget(hbox(button("返回核对",dialog.reject),confirm))
        if dialog.exec()!=QDialog.Accepted:return False
        self.store.release_review([r["记录ID"] for r in (held if preserve.isChecked() else good)]);self.refresh();return True
    def export_dialog(self):
        path,_=QFileDialog.getSaveFileName(self,"导出完整CSV总表",str(self.store.csv_path),"CSV (*.csv)")
        if path:
            try:atomic_write(path,self.store.encode_csv(self.store.all(True)));self.statusBar().showMessage("已导出完整总表："+path,8000)
            except Exception as e:self.error(e)
    def auto_tick(self):
        import time
        if self._auth_locked or self._local_busy:return
        if self.network and not ledger_session.active():self.lock_access("请登录后继续使用");return
        if self._refresh_pending and self.table.state()!=QAbstractItemView.EditingState:self.refresh_table()
        for store in self.stores.values():
            if store.last_export_error:store.export()
        # Uploads are manual. Login triggers one read-only refresh of all sheets.
    def show_operation_result(self):
        self.notify_result('success','最近操作结果',self.operation_detail)
    def show_feedback(self,level,title,message,popup=True):
        self.operation_detail=title+'\n'+message
        self.operation_label.setText(title+' · '+message.split('\n')[0]);self.operation_label.setStyleSheet('font-weight:600;color:'+({'success':'#286c32','warning':'#994700','error':'#af3025','working':'#18667c'}.get(level,'#20332a')))
        self.operation_card.setToolTip(self.operation_detail);self.banner.setText(title+' · '+message.split('\n')[0])
        if popup:self.notify_result(level,title,message)
    def blocked_upload_feedback(self):
        count=len(self.review_panel.items);first=self.review_panel.current()
        message=f'本次未上传：还有 {count} 条待核对。已保存的修改仍在本机；服务器暂时看不到这些修改。'
        if first:message+='\n下一条：'+first['source']+'\n'+(self.review_panel.validation_message() or '请核对最终内容并保存本条。')
        self.show_feedback('warning','尚未上传',message)
    def notify_result(self,level,title,message):
        icons={"success":QMessageBox.Information,"warning":QMessageBox.Warning,"error":QMessageBox.Critical}
        dialog=QMessageBox(icons[level],title,message,parent=self);dialog.setWindowModality(Qt.WindowModal)
        dialog.setTextFormat(Qt.PlainText);dialog.addButton("知道了",QMessageBox.AcceptRole);self._notices.append(dialog)
        def closed():
            if dialog in self._notices:self._notices.remove(dialog)
            dialog.deleteLater()
        dialog.finished.connect(closed);dialog.open()
    def review_pending_conflicts(self):
        if self._local_busy or self.worker and self.worker.isRunning():return
        self.clear_filters();items=review_items(self.store);self.review_panel.present(items)
        self.banner.setText(f'{len(items)} 条待核对：调整最终内容后保存本条，再点击右下角同步。' if items else '没有待核对的冲突；可直接同步。')
    def locate_review_row(self,key,fields):
        if not key:return
        for n in range(self.table.rowCount()):
            first=self.table.item(n,0)
            if first and first.data(Qt.UserRole)==key:
                self.table.selectRow(n)
                for field in fields:
                    if field in self.columns:
                        c=self.columns.index(field);self.table.setColumnHidden(c,False);cell=self.table.item(n,c)
                        if cell:cell.setBackground(QColor('#ffdeb0'));cell.setToolTip('待核对字段：'+field+'；请在下方比较并修改最终内容。')
                self.table.scrollToItem(first,QAbstractItemView.PositionAtCenter);break
    def recheck_conflicts(self):
        if self._local_busy or self.worker and self.worker.isRunning():return
        self.start_local_work('recheck',self.store)
    def reconnect_source(self,old_path):
        if self._local_busy or self.worker and self.worker.isRunning():return
        selected,_=QFileDialog.getOpenFileName(self,"重新选择原文件 · 保留已修改台账","","原台账 (*.xlsx *.csv)")
        if selected:self.start_local_work('relocate',self.store,{'old_path':old_path,'new_path':selected})
    def save_review_item(self,item,values,target_key):
        if self._local_busy:return
        self.start_local_work('review',self.store,{'item':item,'values':values,'target_key':target_key,'admin':self.is_admin()})
    def start_local_work(self,kind,target,options=None):
        if self._local_busy or self._auth_locked:return
        self._local_busy=True;self._local_cancelled=False;self._local_target=target;self.local_progress.show();self.local_status.setText('正在后台核对…');self.show_feedback('working','处理中','正在保存本地核对结果…' if kind=='review' else '正在后台检查文件，尚未上传，请等待结果。',False);self.cancel_work_button.setEnabled(True)
        self._set_transfer_busy(True);self.sync_button.setText('正在核对…');self.review_panel.setEnabled(False);self.new_button.setEnabled(False);self.sheet_tabs.setEnabled(False);self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.local_worker=LocalWorker(target.root,target.schema,kind,options);self.local_worker.progress.connect(self.local_status.setText);self.local_worker.finished.connect(self.local_work_finished);self.local_worker.start()
    def cancel_local_work(self):
        if self._local_busy and self.local_worker:
            self._local_cancelled=True;self.local_worker.requestInterruption();self.cancel_work_button.setEnabled(False);self.local_status.setText('正在取消；当前文件读取结束后停止，不再上传。')
    def local_work_finished(self):
        worker=self.local_worker
        if self._local_cancelled:worker.error=('cancel','已取消；本地记录已保留，未继续上传。')
        self._local_busy=False;self.local_progress.hide();self.review_panel.setEnabled(True);self.new_button.setEnabled(True);self.sheet_tabs.setEnabled(True);self.table.setEditTriggers(QAbstractItemView.DoubleClicked|QAbstractItemView.EditKeyPressed);self._set_transfer_busy(False)
        if self._auth_locked:self._after_review_sync=False;return
        self.refresh()
        if worker.error:
            self._after_review_sync=False;self.banner.setText(worker.error[1]);self.review_panel.hint.setText(worker.error[1])
            if worker.error[0]=='source':
                items=review_items(self.store);issue=source_issue(worker.error[2],worker.error[1]);self.review_panel.present([issue]+[item for item in items if item.get('path')!=worker.error[2]])
            self.show_feedback('warning','已取消 · 未上传' if worker.error[0]=='cancel' else '尚未上传',worker.error[1]+'\n本地修改已保留。',worker.error[0]!='cancel')
            return
        if worker.kind=='import':self.import_preview(worker.result)
        elif worker.kind=='review':
            self.review_panel.saved(worker.options['item']);self.review_pending_conflicts()
            again=self._after_review_sync;self._after_review_sync=False;remaining=len(self.review_panel.items)
            message=f'本条已保存到本地，未上传。还有 {remaining} 条待核对。'
            message+='\n问题位置：'+worker.options['item']['source']
            message+=('\n请继续核对其他记录；全部完成后点击右下角“保存修改并同步”。' if remaining else '\n请点击右下角“保存修改并同步”，看到“服务器已确认保存”才表示上传完成。')
            if again and not remaining:
                self.show_feedback('working','本地核对已保存','正在继续同步到服务器，请等待确认回执。',False);QTimer.singleShot(0,lambda:self.sync(True))
            else:self.show_feedback('warning','已保存本地 · 未上传',message)
        elif worker.kind in ('recheck','relocate'):
            self.review_pending_conflicts()
            if self.review_panel.items:self.blocked_upload_feedback()
            else:self.show_feedback('warning','核对完成 · 未上传','原文件已重新核对，本地修改已保留。请点击右下角“保存修改并同步”，等待服务器确认。')
        elif worker.kind=='prepare':
            if worker.result['blocked']:self.review_pending_conflicts();self.blocked_upload_feedback();return
            if not ledger_session.active():self.lock_access('请登录后同步');return
            self.start_transfer(self._local_target,worker.result['pending'],**worker.options['transfer'])
    def refresh_server(self,automatic=False):
        if not self.network or self._auth_locked or self.quitting or self._local_busy:return
        if not ledger_session.active():self.lock_access("请登录后刷新服务器台账");return
        if self._refresh_batch:return
        if any(w and w.isRunning() for w in (self.worker,self.importer)):
            if automatic:QTimer.singleShot(500,lambda:self.refresh_server(True))
            else:self.banner.setText("当前任务完成后可刷新服务器")
            return
        if not authorized(ROOT):return self.error("连接组件不完整，请重新安装最新版")
        self.show_feedback("working","正在刷新服务器","正在读取三张台账的最新内容，请等待刷新结果。",False)
        self._refresh_batch={"added":0,"updated":0,"latest":"","queue":[self.sheet_id]+[id for id in self.stores if id!=self.sheet_id],"results":[],"errors":[],"automatic":automatic}
        self._next_refresh()
    def _next_refresh(self):
        batch=self._refresh_batch
        if not batch:return
        if self._auth_locked or self.quitting:self._refresh_batch=None;return
        if batch['queue']:
            self.sync(True,sheet_id=batch['queue'].pop(0),pull=True);return
        self._refresh_batch=None
        message="服务器已刷新 · "+" · ".join(batch['results'])
        if batch['errors']:
            message="服务器刷新未全部完成，本地数据已保留。\n"+"\n".join(batch['errors'])
            self.show_feedback("warning","刷新未完成",message)
        else:
            drafts=sum(bool(r['_dirty']) for store in self.stores.values() for r in store.all(True))
            if drafts:message+=f" · 本机 {drafts} 条未提交修改已保留"
            summary=f"本次读取新增 {batch['added']} 条、更新 {batch['updated']} 条。" if batch['added'] or batch['updated'] else '本次刷新没有新上传或修改，服务器内容与上次一致。'
            details=summary+'\n'+'\n'.join(batch['results'])+'\n刷新时间：'+now()
            if batch['latest']:details+='\n服务器最新记录修改时间：'+batch['latest']
            if drafts:details+=f'\n本机另有 {drafts} 条未上传修改，刷新不会上传。'
            details+='\n可清除日期、目录或搜索筛选查看全部记录；上传端须显示服务器确认后，这里才能读到。'
            self.show_feedback('success','刷新完成',details,not batch['automatic'])
        self.banner.setText(message);self._set_transfer_busy(False)
    def _set_transfer_busy(self,busy,pull=False):
        self.sync_button.setEnabled(not busy);self.import_button.setEnabled(not busy);self.refresh_button.setEnabled(not busy)
        self.sync_button.setText("正在同步…" if busy and not pull else "保存修改并同步")
        self.refresh_button.setText("正在刷新…" if busy and pull else "刷新服务器")
    def transfer_finished(self):
        if self._local_busy:return
        if self._refresh_batch:QTimer.singleShot(0,self._next_refresh)
        else:self._set_transfer_busy(False)
    def sync(self,manual=False,probe=False,sheet_id=None,source_ids=None,source_name=None,pull=False):
        if not manual or not self.network or self._auth_locked:return
        if not ledger_session.active():self.lock_access("请登录后同步");return
        id=sheet_id or self.sheet_id;target=self.stores[id]
        if not authorized(ROOT):return self.error("连接组件不完整，请重新安装最新版")
        if self._refresh_batch and not pull:return
        if self.worker and self.worker.isRunning():return
        if self.importer and self.importer.isRunning():
            QTimer.singleShot(100,lambda:self.sync(manual,probe,id,source_ids,source_name,pull));return
        name="样本试验台账" if id=="samples" else SCHEMAS[id].title
        if self._local_busy:return
        if not probe and not pull:
            if self.review_panel.isVisible() and self.review_panel.current():
                self._after_review_sync=True
                if not self.review_panel.save_current():self._after_review_sync=False
                return
            if source_ids is None and not self.approve_review():return
            options={'source_ids':source_ids,'transfer':{'probe':probe,'pull':pull,'source_ids':source_ids,'source_name':source_name}}
            self.start_local_work('prepare',target,options);return
        self.start_transfer(target,[],probe,pull,source_ids,source_name)
    def start_transfer(self,target,pending,probe=False,pull=False,source_ids=None,source_name=None):
        if self._auth_locked or not ledger_session.active():return
        id=target.schema.id if target.schema else 'samples';name='样本试验台账' if id=='samples' else SCHEMAS[id].title
        self.manual_sync=True;self._set_transfer_busy(True,pull)
        origin='读取服务器：'+name if pull else '连接检测' if probe else '文件：'+source_name if source_name else '台账修改：'+name
        action='probe' if probe else 'pull' if pull else 'sync'
        self._sync_context={'action':action,'origin':origin,'sheet_id':id,'source_skipped':getattr(target,'last_import_summary',{}).get('skipped',0) if source_ids is not None else 0}
        self.show_feedback('working','正在读取服务器' if pull else '正在同步',origin+'；请等待服务器确认，当前还不能视为上传成功。',False)
        self.worker=SyncWorker(target.settings(),pending,action,target.known_ids())
        self.worker.success.connect(self.sync_success);self.worker.failed.connect(self.sync_failed);self.worker.finished.connect(self.transfer_finished);self.worker.start()
    def sync_success(self,reply,sent):
        if self._auth_locked:return
        if reply.get("user"):self.account_checked(reply)
        if self._auth_locked:return
        id=reply.get("_sheet_id","samples");target=self.stores[id];name="样本台账" if id=="samples" else SCHEMAS[id].title
        action=reply.get('_action',self._sync_context.get('action','sync'))
        if "rows" in reply:
            known={entry['id']:json.loads(entry['base_body'] or entry['body']) for entry in target.db.execute("SELECT id,body,base_body FROM records WHERE base!=''")}
            received_added=sum(row['记录ID'] not in known for row in reply['rows'])
            received_updated=sum(row['记录ID'] in known and target.fingerprint(row)!=target.fingerprint(known[row['记录ID']]) for row in reply['rows'])
            try:target.apply_sync(reply["rows"],sent,reply.get("aliases"),accepted=reply.get("accepted"))
            except Exception as e:self.sync_failed(str(e),id);return
            confirmed=now()
            target.configure({"last_confirmed_target":reply["target"],"last_confirmed_at":confirmed,"last_direct_connection":reply.get("connection"),"last_delta":reply.get("delta",{})})
            delta=reply.get("delta",{});message=f"{name}已{'刷新' if action=='pull' else '同步'} · {reply['count']} 条"
            conflicts=sum(bool(r['_conflict']) for r in target.all(True))+source_conflict_count(target)
            if delta.get("inserted") or delta.get("updated"):message+=f" · 新增{delta.get('inserted',0)} 更新{delta.get('updated',0)}"
            if conflicts:message+=f" · {conflicts} 条冲突待处理"
            if id==self.sheet_id:self.refresh();self.update_remote_footer(confirmed=reply)
            else:self.update_sheet_tabs()
            if self._refresh_batch:
                batch=self._refresh_batch;batch['added']+=received_added;batch['updated']+=received_updated;batch['latest']=max([batch['latest']]+[row.get('修改时间','') for row in reply['rows']])
                batch['results'].append(f"{name}：新增 {received_added} 条，更新 {received_updated} 条，共 {reply['count']} 条")
            elif action=='sync':
                remaining=sum(bool(r['_dirty']) for r in target.all(True))
                details=f"服务器已确认保存，CSV 完整性校验通过。\n同步来源：{self._sync_context.get('origin',name)}\n新增 {delta.get('inserted',0)} 条，更新 {delta.get('updated',0)} 条，服务器共 {reply['count']} 条。\n服务器：{target.settings()['host']}:{target.settings()['port']}\n远端文件：{reply['target']}\n确认时间：{confirmed}"
                confirmed_count=delta.get('confirmed',sum(not bool((target.get(reply.get('aliases',{}).get(p['record']['记录ID'],p['record']['记录ID'])) or {'_dirty':True}).get('_dirty')) for p in sent))
                details+=f"\n本次已确认 {confirmed_count} 条；重复跳过 {delta.get('preflight_skipped',0)+delta.get('unchanged',0)+self._sync_context.get('source_skipped',0)} 条；待核对 {delta.get('unconfirmed',0)} 条。"
                if remaining:details+=f"\n当前台账还有 {remaining} 条本机修改未上传（包括其他来源或待核对内容）。"
                if conflicts:details+=f"\n其中 {conflicts} 条冲突需要点击“核对冲突”处理。"
                self.show_feedback("warning" if conflicts or remaining else "success","部分同步完成" if conflicts or remaining else "同步成功",details)
        else:
            message=name+"服务器连接正常"
            self.notify_result("success","连接成功",message+"\n远端文件："+reply.get('target',target.settings()['server_file']))
        self.failures=0;self.last_connection=now()+" "+message;self.banner.setText(message);target.log(message)
    def sync_failed(self,message,sheet_id=None):
        id=sheet_id or self.sheet_id;self.failures+=1
        self.last_connection=message;self.banner.setText(message);self.stores[id].log(message)
        if id==self.sheet_id:self.refresh();self.update_remote_footer(error=message)
        else:self.update_sheet_tabs()
        if message.startswith("AUTH_REQUIRED"):
            self._refresh_batch=None;self.lock_access(message);return
        if self._refresh_batch:self._refresh_batch['errors'].append(("样本台账" if id=='samples' else SCHEMAS[id].title)+"："+message)
        else:
            s=self.stores[id].settings();action=self._sync_context.get('action','sync')
            self.show_feedback("error","同步未完成" if action=='sync' else "连接未完成",message+f"\n服务器：{s['host']}:{s['port']}\n远端文件：{s['server_file']}\n未取得完整成功回执，本地数据已保留。请刷新核对或重试，同步会自动去重。")
    def conflicts_dialog(self):self.review_pending_conflicts()
    def settings_dialog(self):
        if self._local_busy:return
        s=self.store.settings();dialog=QDialog(self);dialog.setWindowTitle("软件设置");dialog.resize(780,680);layout=QVBoxLayout(dialog);tabs=QTabWidget();layout.addWidget(tabs)
        general=QWidget();form=QFormLayout(general);fields={}
        for key,title in [("farm","默认牧场"),("wearer","默认尾环佩戴人"),("annotator","默认事件标注人")]:
            fields[key]=QLineEdit(s[key]);form.addRow(title,fields[key])
        form.addRow(label("点击窗口右上角关闭会直接退出软件；本地台账已保存。登录后自动读取三张表；“刷新服务器”只读取。上传仅在导入后直接同步或点击“保存修改并同步”时进行。"))
        startup=QCheckBox("Windows 登录后自动启动");startup.setChecked(self.autostart_enabled())
        if not EMBEDDED:form.addRow(startup)
        form.addRow("本地保存位置",label(str(self.store.root)));form.addRow(hbox(button("打开本地目录",lambda:os.startfile(str(self.store.root))),button("导出CSV总表",self.export_dialog)));tabs.addTab(general,"录入与保存")
        server=QWidget();form2=QFormLayout(server)
        for key,title in [("host","服务器"),("user","上传账户"),("server_file","服务器固定CSV")]:
            fields[key]=QLineEdit(s[key]);form2.addRow(title,fields[key])
        fields["port"]=QSpinBox();fields["port"].setRange(1,65535);fields["port"].setValue(s["port"]);form2.addRow("SSH端口",fields["port"])
        info=label("使用随软件提供的专用上传密钥与固定主机指纹。\n服务器固定CSV位置必须与接收端配置一致。更换服务器时，管理员需要同步更新 known_hosts。","muted");info.setWordWrap(True);form2.addRow(info)
        status=label(self.last_connection);status.setWordWrap(True);form2.addRow("最近连接",status);form2.addRow(button("检测已保存的服务器配置",lambda:self.sync(True,True)));tabs.addTab(server,"服务器")
        logs=QPlainTextEdit();logs.setReadOnly(True);logs.setPlainText("\n".join(r["at"]+" "+r["message"] for r in self.store.db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 80")));tabs.addTab(logs,"同步记录")
        about=QPlainTextEdit();about.setReadOnly(True);about.setPlainText("COWMATA 现场试验台账 "+VERSION+"\n\nExcel A—O 的15个业务字段保持原有顺序。每次佩戴占一行；牛号按文本保存。\n\n记录日期用于导航；佩戴开始/结束、产犊开始/结束分别保留。界面中的归类目录与 COWMATA Pro 3.9 端侧下载对齐，CSV中保存目录字段。\n\n本地SQLite事务保存，自动生成UTF-8 BOM CSV；手动按记录版本合并到服务器固定CSV。网络失败后请点击重新同步。\n\nCSV包含记录ID、版本、修改时间及已删除标记。查询有效记录时筛选 已删除=0；待核对记录请检查核对提示。原始单元格保留Excel原文。\n\n快捷键：Ctrl+N 新建；Ctrl+D 续填同牛；Ctrl+C 复制选中行；Ctrl+Z 撤销。\n\n开发源码见 source；服务器升级见 server-upgrade。");tabs.addTab(about,"说明")
        error=label("");error.setWordWrap(True);error.setStyleSheet("color:#ad3c29");layout.addWidget(error)
        def save():
            try:
                values={}
                for key,w in fields.items():values[key]=w.isChecked() if isinstance(w,QCheckBox) else w.value() if isinstance(w,QSpinBox) else w.text().strip()
                if not re.fullmatch(r"[a-zA-Z0-9_.-]+",values["host"]) or values["host"].startswith("-"):raise ValueError("服务器填写IP或主机名")
                if not re.fullmatch(r"[a-zA-Z0-9_.-]+",values["user"]) or values["user"].startswith("-"):raise ValueError("账户格式无效")
                from pathlib import PureWindowsPath
                if not PureWindowsPath(values["server_file"]).is_absolute() or not values["server_file"].lower().endswith(".csv"):raise ValueError("请输入服务器CSV绝对路径")
                if any(values[k]!=s[k] for k in ["host","port","user","server_file"]):
                    if self.store.all(True):raise ValueError("此台账已绑定服务器；更换服务器需先完成迁移，避免混入其他总表。")
                if not EMBEDDED:self.set_autostart(startup.isChecked())
                self.store.configure(values)
                shared={k:v for k,v in values.items() if k not in ("server_file",)}
                for other in self.stores.values():other.configure(shared)
                self.next_retry=0;dialog.accept()
            except Exception as e:error.setText(str(e))
        layout.addWidget(hbox(button("取消",dialog.reject),button("保存设置",save,True)));dialog.exec()
    def autostart_enabled(self):
        if os.name!="nt":return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                return bool(winreg.QueryValueEx(key,"COWMATA_LedgerUploader")[0])
        except OSError:return False
    def set_autostart(self,enabled):
        if os.name!="nt":return
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enabled:winreg.SetValueEx(key,"COWMATA_LedgerUploader",0,winreg.REG_SZ,'"'+str(ENTRY_ROOT/"现场台账上传器.exe")+'" --background')
            else:
                try:winreg.DeleteValue(key,"COWMATA_LedgerUploader")
                except FileNotFoundError:pass
    def exit_app(self):self.quitting=True;self.close()
    def closeEvent(self,event):
        if self._local_busy:
            self.cancel_local_work();self.banner.setText("正在取消后台核对，窗口仍可操作；完成后可退出。");event.ignore();self.quitting=False;return
        if (self.worker and self.worker.isRunning()) or (self.importer and self.importer.isRunning()):
            self.banner.setText("正在完成当前任务，完成后可退出；本地记录已保留。");event.ignore();self.quitting=False;return
        if self.account_worker and self.account_worker.isRunning():
            self.account_worker.finished.connect(self.close_when_idle);event.ignore();return
        updater=getattr(self,"updater",None)
        if updater and updater.worker and updater.worker.isRunning():
            self._close_after_update=True;updater.worker.requestInterruption();event.ignore();return
        self.timer.stop();self.auth_timer.stop();self.tray.hide()
        for store in self.stores.values():store.export();store.db.close()
        event.accept()
        if not EMBEDDED:QApplication.quit()

def connection_settings(data):
    # Read connection settings only; no ledger is opened before successful login.
    settings=dict(DEFAULTS);path=Path(data)/"台账缓存.sqlite"
    if path.exists():
        import sqlite3
        db=None
        try:
            db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
            for key,value in db.execute("SELECT key,value FROM settings WHERE key IN ('host','port','user')"):settings[key]=json.loads(value)
        except (sqlite3.Error,ValueError):pass
        finally:
            if db:db.close()
    return settings
def main():
    from .ledger_branding import identify_process,configure_application
    identify_process()
    app=QApplication(sys.argv);configure_application(app,ROOT);app.setQuitOnLastWindowClosed(False);app.setStyle("Fusion");app.setStyleSheet(STYLE)
    data=Path(os.environ.get("COWMATA_LEDGER_HOME",str(ROOT/"data")))
    data.mkdir(parents=True,exist_ok=True)
    lock=QLockFile(str(data/"session.lock"));lock.setStaleLockTime(0)
    if not lock.tryLock(0):QMessageBox.information(None,"现场试验台账","软件已在运行，请从右下角托盘打开。");return 0
    settings=connection_settings(data)
    while True:
        ledger_session.clear();login=LoginDialog(ROOT,settings)
        if login.exec()!=QDialog.Accepted:return 0
        account=dict(login.reply["user"]);username=account["username"]
        # Preserve existing administrator data. Operators have independent drafts/settings.
        profile=data if username=="admin" else data/"accounts"/username
        try:store=Store(profile)
        except Exception as e:QMessageBox.critical(None,"无法打开本地台账",str(e));return 1
        store.configure({key:settings[key] for key in ("host","port","user")})
        window=MainWindow(store,network="--offline" not in sys.argv,account=account)
        if "--no-updates" not in sys.argv:
            from .ledger_update_ui import UpdateController
            window.updater=UpdateController(window,ROOT)
        window.setWindowTitle(window.windowTitle()+" · "+username+"（"+("管理员" if account["role"]=="admin" else "操作员")+"）")
        window.show()
        def failure(kind,error,tb):
            store.log("未处理异常："+str(error));QMessageBox.critical(window,"操作未完成",str(error)+"\\n本地已提交记录保留，请检查后重试。")
        sys.excepthook=failure
        result=app.exec();ledger_session.clear()
        if not window.logout_requested:return result
if __name__=="__main__":sys.exit(main())
