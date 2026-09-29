"""Non-modal, editable conflict panel next to the ledger."""
import json
from PySide6.QtCore import Qt,Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QComboBox,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView
from .ledger_files import field_value

class ReviewPanel(QWidget):
 saveRequested=Signal(dict,dict,object)
 locateRequested=Signal(str,list)
 recheckRequested=Signal()
 attentionRequested=Signal(str)
 reconnectRequested=Signal(str)
 def __init__(self,parent=None):
  super().__init__(parent);self.items=[];self.drafts={};self.loading=False;self.setMaximumHeight(320)
  layout=QVBoxLayout(self);layout.setContentsMargins(6,4,6,4);layout.setSpacing(4)
  top=QHBoxLayout();top.addWidget(QLabel('冲突核对'));self.records=QComboBox();self.records.setMinimumWidth(240);top.addWidget(self.records,1)
  self.recheck=QPushButton('后台重新核对');self.recheck.clicked.connect(self.recheckRequested);top.addWidget(self.recheck)
  self.reconnect=QPushButton('重新选择原文件');self.reconnect.clicked.connect(lambda:self.reconnectRequested.emit(self.current()['path']) if self.current() and self.current().get('path') else None);top.addWidget(self.reconnect)
  close=QPushButton('收起');close.clicked.connect(self.hide);top.addWidget(close);layout.addLayout(top)
  self.hint=QLabel();self.hint.setTextFormat(Qt.PlainText);self.hint.setStyleSheet('font-weight:600;color:#924411;');self.hint.setWordWrap(True);self.hint.setTextInteractionFlags(Qt.TextSelectableByMouse);layout.addWidget(self.hint)
  self.target=QComboBox();self.target.setMinimumHeight(36);self.target.setStyleSheet("QComboBox {border:2px solid #dc9b35;}");self.target.currentIndexChanged.connect(self.load_fields);layout.addWidget(self.target)
  self.cells=QTableWidget(0,4);self.cells.setHorizontalHeaderLabels(['问题字段','本机内容','文件 / 服务器内容','最终内容（双击可修改）']);self.cells.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);self.cells.setEditTriggers(QAbstractItemView.DoubleClicked|QAbstractItemView.EditKeyPressed);self.cells.verticalHeader().hide();layout.addWidget(self.cells,1)
  bottom=QHBoxLayout();self.local=QPushButton('最终内容采用本机');self.other=QPushButton('最终内容采用对方');self.apply=QPushButton('保存本条核对');self.apply.setObjectName('primary')
  self.local.clicked.connect(lambda:self.copy_values(1));self.other.clicked.connect(lambda:self.copy_values(2));self.apply.clicked.connect(self.save_current)
  for w in (self.local,self.other):bottom.addWidget(w)
  bottom.addWidget(QLabel('核对保存后，点击右下角同步'),1);bottom.addWidget(self.apply);layout.addLayout(bottom)
  self.records.currentIndexChanged.connect(self.select_item);self.cells.itemChanged.connect(self.edited);self.hide()
 def token(self,item):return (item['kind'],item.get('path',''),item['key'],item.get('expected_body',''),item.get('expected_conflict',''),item.get('expected_incoming',''),self.target.currentData() if item['kind']=='ambiguous' else None)
 def present(self,items):
  self.items=items;self.records.blockSignals(True);self.records.clear()
  for item in items:
   r=item['local'];self.records.addItem(('服务器 · ' if item['kind']=='server' else '文件 · ')+str(r.get('序号') or '')+' 牛号 '+str(r.get('牛号') or '待确认')+' · '+item['source'])
  self.records.blockSignals(False)
  if items:self.show();self.select_item(0)
  else:self.hide();self.drafts.clear()
 def current(self):
  i=self.records.currentIndex();return self.items[i] if 0<=i<len(self.items) else None
 def select_item(self,index):
  item=self.current()
  if not item:return
  self.reconnect.setVisible(bool(item.get('path')));self.hint.setText(item['reason']+'  '+item['source']);self.target.blockSignals(True);self.target.clear();self.target.setVisible(item['kind']=='ambiguous')
  if item['kind']=='ambiguous':
   self.target.addItem(f"必选：这条文件记录对应哪一行？（{len(item['candidates'])} 条候选）",None)
   for r in item['candidates']:self.target.addItem('序号 '+r.get('序号','')+' / 牛号 '+r.get('牛号','')+' / 设备 '+r.get('设备号',r.get('设备编码',''))+' / '+r.get('佩戴开始',r.get('生产日期',''))+' / '+r['记录ID'][:8],r['记录ID'])
  self.target.blockSignals(False);self.load_fields()
 def load_fields(self,*args):
  item=self.current()
  if not item:return
  self.loading=True;local=item['local'];target=self.target.currentData()
  if item['kind']=='ambiguous' and not target:
   self.cells.setRowCount(0);self.loading=False;self.apply.setEnabled(True);self.local.setEnabled(False);self.other.setEnabled(False);self.hint.setText(self.validation_message());return
  if item['kind']=='ambiguous' and target:local=next(r for r in item['candidates'] if r['记录ID']==target)
  self.hint.setText(item['reason']+'  '+item['source'])
  proposed=item.get('proposals',{}).get(target,item.get('proposed',local))
  self.cells.setRowCount(len(item['fields']));draft=self.drafts.get(self.token(item),{})
  for n,key in enumerate(item['fields']):
   values=[key,field_value(local,key),field_value(item['other'],key),draft.get(key,field_value(proposed,key))]
   for c,value in enumerate(values):
    cell=QTableWidgetItem(str(value));cell.setToolTip(str(value));self.cells.setItem(n,c,cell)
    if c==0:
     cell.setData(Qt.UserRole,key)
     if key=='已删除':cell.setText('删除状态：0 保留 / 1 删除')
    if c!=3:cell.setFlags(cell.flags()&~Qt.ItemIsEditable)
    if values[1]!=values[2]:cell.setBackground(QColor('#fff0dc'))
  self.loading=False;self.apply.setEnabled(True);self.local.setEnabled(item['kind']!='legacy');self.other.setEnabled(item['kind']!='legacy')
  self.locateRequested.emit(target or item['key'],item['fields'])
 def values(self):return {self.cells.item(n,0).data(Qt.UserRole):self.cells.item(n,3).text() for n in range(self.cells.rowCount())}
 def edited(self,*args):
  if not self.loading and self.current():self.drafts[self.token(self.current())]=self.values()
 def copy_values(self,column):
  for n in range(self.cells.rowCount()):self.cells.item(n,3).setText(self.cells.item(n,column).text())
 def validation_message(self):
  item=self.current()
  if not item:return '没有待保存的核对记录。'
  if item['kind']=='legacy':return '未上传。'+item['reason']+' 点击“后台重新核对”；如果文件已移动或改名，点击“重新选择原文件”。问题位置：'+item['source']
  if item['kind']=='ambiguous' and not self.target.currentData():
   return '未保存、未上传。请在橙色下拉框中选择对应的台账行，再保存核对。共 '+str(len(item['candidates']))+' 条候选；问题位置：'+item['source']
  return ''
 def save_current(self):
  item=self.current()
  if not item:return False
  # Finish the active cell editor before collecting values.
  self.apply.setFocus()
  message=self.validation_message()
  if message:
   self.hint.setText(message);self.target.setFocus();self.attentionRequested.emit(message);return False
  self.saveRequested.emit(item,self.values(),self.target.currentData() if item['kind']=='ambiguous' else None);return True
 def saved(self,item):
  for key in list(self.drafts):
   if key[:3]==(item['kind'],item.get('path',''),item['key']):self.drafts.pop(key,None)
