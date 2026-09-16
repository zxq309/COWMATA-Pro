"""Independent original-video preparation, ending in standard classified MP4."""
from __future__ import annotations
import json,os,sys,uuid
from collections import defaultdict
from pathlib import Path
from PySide6.QtCore import QProcess,QSettings,Qt,QTimer,QUrl,QSize
from PySide6.QtGui import QDesktopServices,QPixmap,QIcon,QStandardItemModel,QStandardItem
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QComboBox,
    QLineEdit,QFileDialog,QTableWidget,QTableWidgetItem,QHeaderView,QCheckBox,QProgressBar,QAbstractItemView,QDialog)
from . import dahua_tasks as tasks
from .data_category import CATEGORIES
from .storage import atomic_json

class DahuaPanel(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.settings=QSettings();self.job=None;self.index=None;self.process=None;self.active=False
        self.groups={};self.files=[];self.json_sources=[];self.disks=[];self.buffer=b'';self.error=''
        outer=QVBoxLayout(self)
        help=QLabel('原始录像准备 → 核对通道与视角 → 转为标准 MP4 并归类。主标注界面加载归类后的 MP4。')
        help.setWordWrap(True);outer.addWidget(help)
        row=QHBoxLayout();self.mode=QComboBox();self.mode.addItems(['录像文件 / 目录','录像机原盘（只读）'])
        row.addWidget(self.mode);self.source_text=QLineEdit();self.source_text.setReadOnly(True)
        self.source_text.setPlaceholderText('DAV / DHAV 文件，或含原始录像的目录');row.addWidget(self.source_text,1)
        self.disk_choice=QComboBox();self.disk_choice.setMinimumWidth(350);row.addWidget(self.disk_choice)
        self.files_button=QPushButton('选择文件…');self.files_button.clicked.connect(self.choose_files);row.addWidget(self.files_button)
        self.folder_button=QPushButton('选择目录…');self.folder_button.clicked.connect(self.choose_folder);row.addWidget(self.folder_button)
        self.disk_button=QPushButton('刷新原盘');self.disk_button.clicked.connect(lambda:self.start('disks',{}));row.addWidget(self.disk_button)
        outer.addLayout(row);self.mode.currentIndexChanged.connect(self.mode_changed);self.mode_changed()
        row=QHBoxLayout();row.addWidget(QLabel('牧场目录'));self.target=QLineEdit();self.target.setPlaceholderText('选择保存归类结果的牧场')
        self.target.setText(str(self.settings.value('dahua/target','')));row.addWidget(self.target,1)
        self.target_button=QPushButton('选择…');self.target_button.clicked.connect(self.choose_target);row.addWidget(self.target_button)
        self.category=QComboBox()
        for key,title in CATEGORIES.items():
            if key!='pregnancy':self.category.addItem(title,key)
        self.category.setCurrentIndex(self.category.findData('calving'));row.addWidget(self.category)
        self.scenario=QComboBox();self.scenario.addItem('新归类，可一并接入 JSON','mixed');self.scenario.addItem('以已有 Motion 为基准补视频','attach_video');row.addWidget(self.scenario)
        outer.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel('时间范围（北京时间）'))
        self.start_time=QLineEdit();self.end_time=QLineEdit()
        self.start_time.setPlaceholderText('开始 YYYY-MM-DD HH:MM:SS，可空');self.end_time.setPlaceholderText('结束 YYYY-MM-DD HH:MM:SS，可空')
        row.addWidget(self.start_time);row.addWidget(self.end_time)
        self.midnight=QCheckBox('跨午夜按天拆分');self.midnight.setChecked(True);row.addWidget(self.midnight)
        self.json_button=QPushButton('附加九轴 / PPG / 温度目录…');self.json_button.clicked.connect(self.choose_json);row.addWidget(self.json_button)
        outer.addLayout(row)
        row=QHBoxLayout();self.scan_button=QPushButton('扫描原始录像');self.scan_button.clicked.connect(self.scan);row.addWidget(self.scan_button)
        self.preview_button=QPushButton('核对已选通道缩略图');self.preview_button.clicked.connect(self.previews);row.addWidget(self.preview_button)
        self.resume_button=QPushButton('恢复上次视频任务');self.resume_button.clicked.connect(self.restore);row.addWidget(self.resume_button)
        row.addStretch();outer.addLayout(row)
        self.table=QTableWidget(20,4);self.table.setHorizontalHeaderLabels(['固定归档视角','原通道 / 来源（人工选择）','静态预览','数据与核对状态'])
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0,105);self.table.setColumnWidth(2,150)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.setWordWrap(False)
        self.preview_paths={}
        self.mapping=[]
        for i,view in enumerate(tasks.VIEWS):
            self.table.setItem(i,0,QTableWidgetItem(view));combo=QComboBox();combo.setMinimumContentsLength(12);combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);combo.addItem('不接入此视角','')
            self.table.setCellWidget(i,1,combo);self.mapping.append(combo)
            preview=QPushButton('尚未预览');preview.setEnabled(False)
            preview.clicked.connect(lambda checked=False,row=i:self.show_preview(row))
            self.table.setCellWidget(i,2,preview);self.table.setItem(i,3,QTableWidgetItem('未选择来源'))
        self.fit_table_rows()
        outer.addWidget(self.table,1)
        self.status=QLabel('先扫描，再人工选择原通道对应的视角。空视角保留编号，不生成占位视频。');self.status.setWordWrap(True);outer.addWidget(self.status)
        self.bar=QProgressBar();self.bar.setRange(0,1);outer.addWidget(self.bar)
        row=QHBoxLayout();self.run_button=QPushButton('开始转码并归类');self.run_button.clicked.connect(self.organize);row.addWidget(self.run_button)
        self.pause_button=QPushButton('暂停');self.pause_button.clicked.connect(self.pause);row.addWidget(self.pause_button)
        self.output_button=QPushButton('打开归类目录');self.output_button.clicked.connect(self.open_output);row.addWidget(self.output_button)
        self.report_button=QPushButton('查看视频任务记录');self.report_button.clicked.connect(self.open_report);row.addWidget(self.report_button)
        outer.addLayout(row)
        self.controls=[self.mode,self.source_text,self.disk_choice,self.files_button,self.folder_button,self.disk_button,self.target,self.target_button,self.category,self.scenario,self.start_time,self.end_time,self.midnight,self.json_button,self.scan_button,self.preview_button,self.resume_button,self.run_button,*self.mapping]
        self._disks_loaded=False
        self.disk_choice.addItem('请选择录像机原盘…',None)
        self.disk_choice.activated.connect(self.disk_selected)
        self.mode.setCurrentIndex(1)
        self.refresh()
    def fit_table_rows(self):
        # Match the controls and current font/DPI, not an empty thumbnail frame.
        height=max(34,self.fontMetrics().height()+16,max(c.sizeHint().height()+4 for c in self.mapping))
        self.table.verticalHeader().setDefaultSectionSize(height)
        for row in range(self.table.rowCount()):self.table.setRowHeight(row,height)
        self.table.setColumnWidth(0,max(105,self.fontMetrics().horizontalAdvance('固定归档视角')+20))
        self.table.setColumnWidth(2,max(150,self.fontMetrics().horizontalAdvance('查看大图')+70))
    def showEvent(self,event):
        super().showEvent(event);self.fit_table_rows()
        if self.mode.currentIndex()==1 and not self._disks_loaded and not self.running:
            self._disks_loaded=True
            QTimer.singleShot(0,lambda:self.start('disks',{}))
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'mapping'):self.fit_table_rows()
    def show_preview(self,row):
        image=QPixmap(self.preview_paths.get(row,''))
        if image.isNull():return
        box=QDialog(self);box.setWindowTitle(tasks.VIEWS[row]+' · 静态预览')
        layout=QVBoxLayout(box);label=QLabel();label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        available=self.screen().availableGeometry()
        label.setPixmap(image.scaled(min(1100,int(available.width()*.8)),min(750,int(available.height()*.75)),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(label);box.exec()
    @property
    def running(self):return self.active
    def mode_changed(self):
        disk=self.mode.currentIndex()==1
        for widget in (self.source_text,self.files_button,self.folder_button):widget.setVisible(not disk)
        self.disk_choice.setVisible(disk);self.disk_button.setVisible(disk)
        if disk and self.isVisible() and hasattr(self,'controls') and not self._disks_loaded and not self.running:
            self._disks_loaded=True
            self.start('disks',{})
    def choose_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择原始录像','','原始录像 (*.dav *.dhav *.h264 *.h265)')
        if paths:self.files=paths;self.source_text.setText('；'.join(paths));self.index=None;self.scan()
    def choose_folder(self):
        directory=QFileDialog.getExistingDirectory(self,'选择原始录像目录','',QFileDialog.Option.DontUseNativeDialog)
        if directory:self.files=[directory];self.source_text.setText(directory);self.index=None;self.scan()
    def choose_target(self):
        directory=QFileDialog.getExistingDirectory(self,'选择输出牧场',self.target.text())
        if directory:self.target.setText(directory)
    def choose_json(self):
        directory=QFileDialog.getExistingDirectory(self,'选择包含 Motion / PPG / Temp 的目录')
        if directory:
            self.json_sources=[directory];self.json_button.setToolTip(directory);self.json_button.setText('已附加 JSON 目录')
    def scan(self):
        if self.mode.currentIndex()==1:
            disk=self.disk_choice.currentData()
            if not disk:self.status.setText('请刷新并选择录像机原盘');return
            request=dict(mode='disk',disk=disk)
        else:
            if not self.files:self.status.setText('请先选择原始录像');return
            request=dict(mode='files',files=self.files)
        self.job=tasks.task_root()/uuid.uuid4().hex;self.index=None
        self.settings.setValue('dahua/last_job',str(self.job));self.start('scan',request,self.job)
    def selected_mapping(self):
        result={}
        for view,combo in zip(tasks.VIEWS,self.mapping):
            group=combo.currentData()
            if not group:continue
            if group in result:raise ValueError('同一来源不能重复分配到不同视角')
            result[group]=view
        if not result:raise ValueError('请人工选择至少一个原通道与归档视角')
        return result
    def previews(self):
        try:self.start('previews',dict(groups=list(self.selected_mapping())),self.job)
        except ValueError as exc:self.status.setText(str(exc))
    def organize(self):
        try:
            if not self.index:raise ValueError('请先扫描原始录像')
            if not self.target.text().strip():raise ValueError('请选择输出牧场')
            options=dict(target=self.target.text().strip(),category=self.category.currentData(),scenario=self.scenario.currentData(),mapping=self.selected_mapping(),
                start=self.start_time.text().strip(),end=self.end_time.text().strip(),split_midnight=self.midnight.isChecked(),json_sources=self.json_sources)
            self.settings.setValue('dahua/target',options['target']);self.start('organize',dict(options=options),self.job)
        except (OSError,ValueError) as exc:self.status.setText(str(exc))
    def restore(self):
        saved=str(self.settings.value('dahua/last_job','')).strip()
        if not saved or not Path(saved).is_absolute():
            self.status.setText('没有可恢复的视频任务');return
        job=Path(saved)
        self.job=job;self.start('restore',{},job)
    def apply_restored_options(self,options):
        if not options:return
        self.target.setText(options['target']);self.category.setCurrentIndex(self.category.findData(options['category']))
        self.scenario.setCurrentIndex(self.scenario.findData(options.get('scenario','mixed')))
        self.start_time.setText(str(options.get('start') or ''));self.end_time.setText(str(options.get('end') or ''))
        self.midnight.setChecked(options.get('split_midnight',True));self.json_sources=options.get('json_sources',[])
        for group,view in options.get('mapping',{}).items():
            combo=self.mapping[tasks.VIEWS.index(view)];combo.setCurrentIndex(combo.findData(group))
    def apply_disks(self,disks):
        self.disks=disks;self._disks_loaded=True;self.disk_choice.clear()
        self.disk_choice.addItem('请选择录像机原盘…',None)
        for disk in disks:
            letters=' / '.join(disk.get('letters',[])) or '无盘符'
            self.disk_choice.addItem(f"{letters} · 磁盘 {disk['number']} · {disk['model']} · {disk['size']/1e12:.2f} TB"+
                                    (' · 录像机原盘' if disk.get('dhfs') else ' · 非支持的录像机原盘'),disk)
            if not disk.get('dhfs'):
                item=self.disk_choice.model().item(self.disk_choice.count()-1)
                if item:item.setEnabled(False)
        self.status.setText('选择录像机原盘后自动只读扫描；无需打开盘符或格式化。')
    def disk_selected(self,index):
        disk=self.disk_choice.itemData(index)
        if disk and disk.get('dhfs') and not self.running:self.scan()
    def start(self,action,request,job=None):
        if self.running:return
        self.operation=action;self.operation_job=Path(job) if job else tasks.task_root()/('devices-'+uuid.uuid4().hex)
        self.operation_job.mkdir(parents=True,exist_ok=True);(self.operation_job/'dahua-cancel').unlink(missing_ok=True)
        atomic_json(self.operation_job/'dahua-request.json',dict(action=action,**request),backup=False)
        self.buffer=b'';self.stderr=b'';self.error='';self.result_path=None;self.active=True
        self.status.setText('后台读取与核对中…');self.bar.setRange(0,0);self.refresh()
        if self.process:self.process.deleteLater()
        self.process=QProcess(self);self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(lambda:self.read_error())
        self.process.finished.connect(self.finished);self.process.errorOccurred.connect(self.process_error)
        program=Path(sys.executable)
        if os.name=='nt' and program.with_name('pythonw.exe').exists():program=program.with_name('pythonw.exe')
        self.process.start(str(program),['-I','-B',str(Path(__file__).with_name('dahua_worker.py')),str(self.operation_job)])
    def read_error(self):self.stderr=(self.stderr+bytes(self.process.readAllStandardError()))[-12000:]
    def read_output(self):
        self.buffer+=bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            try:
                value=json.loads(line);event=value.get('event')
                if event=='progress':
                    total=value['total'];self.bar.setRange(0,total if total else 0);self.bar.setValue(value['current']);self.status.setText(value['message'])
                elif event=='error':self.error=value['error'];self.status.setText(('已暂停：' if value.get('paused') else '处理失败：')+self.error)
                elif event=='result':self.result_path=value['path']
                elif event=='preview':self.apply_preview(value['row'])
                elif event=='row':self.status.setText(value['row'].get('message',''))
            except (ValueError,KeyError,TypeError):self.error='任务输出无法解析，请查看视频任务记录'
    def process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.error='视频准备进程未能启动';self.active=False;self.status.setText(self.error);self.refresh()
    def finished(self,*_):
        self.read_output();self.read_error();self.active=False;self.bar.setRange(0,1);self.bar.setValue(1)
        try:
            if self.result_path:
                result=tasks.read_json(self.result_path)
                if self.operation=='disks':
                    self.apply_disks(result['disks'])
                elif self.operation in {'scan','restore'}:
                    self.apply_index(result)
                    if self.operation=='restore':self.apply_restored_options(result.get('options',{}))
                elif self.operation=='organize':
                    self.output=result.get('output','');self.status.setText(('归类完成' if result.get('status')=='completed' else '所选范围没有可输出录像')+f"；待核对 {len(result.get('issues',[]))} 项。原始录像保留。")
                elif self.operation=='previews':self.status.setText('缩略图核对完成；通道号只作来源说明，请人工确认视角映射。')
            elif not self.error:self.status.setText('任务未完成：'+self.stderr.decode('utf-8','replace')[-1500:])
        except (OSError,ValueError,KeyError) as exc:self.status.setText('读取任务结果失败：'+str(exc))
        self.refresh()
    def apply_index(self,index):
        if 'rows' in index:index=tasks.index_summary(index)
        self.index=index;self.groups=index['groups']
        model=QStandardItemModel(self)
        empty=QStandardItem('不接入此视角');empty.setData('',Qt.ItemDataRole.UserRole);model.appendRow(empty)
        for group,count in self.groups.items():
            item=QStandardItem(group+' · '+str(count)+' 段');item.setData(group,Qt.ItemDataRole.UserRole);model.appendRow(item)
        previous=getattr(self,'_mapping_model',None);self._mapping_model=model
        for i,combo in enumerate(self.mapping):
            combo.setModel(model);combo.setCurrentIndex(0)
            self.table.setItem(i,3,QTableWidgetItem('未选择来源'));self.preview_paths.pop(i,None)
            button=self.table.cellWidget(i,2);button.setIcon(QIcon());button.setText('尚未预览');button.setEnabled(False)
        if previous:previous.deleteLater()
        self.status.setText(f"扫描到 {index['total']} 段、{len(self.groups)} 组来源，异常索引 {index['invalid']} 段。请人工选择映射。")
    def apply_preview(self,row):
        for i,combo in enumerate(self.mapping):
            if combo.currentData()!=row['group']:continue
            preview=row.get('preview',{});label=self.table.cellWidget(i,2)
            if preview.get('image'):
                self.preview_paths[i]=preview['image'];label.setIcon(QIcon(preview['image']));label.setIconSize(QSize(48,27))
                label.setText('查看大图');label.setEnabled(True)
            self.table.setItem(i,3,QTableWidgetItem(row.get('error') or '原码流通道 '+str(preview.get('channels','未知'))+'；待核对视角'))
    def pause(self):
        if self.running:(self.operation_job/'dahua-cancel').touch();self.status.setText('正在安全暂停，已完成的 MP4 保留用于继续…')
    def request_shutdown(self):
        if self.running:self.pause()
        return not self.running
    def refresh(self):
        if not hasattr(self,'controls'):return
        for widget in self.controls:widget.setEnabled(not self.running)
        self.preview_button.setEnabled(not self.running and bool(self.index));self.run_button.setEnabled(not self.running and bool(self.index))
        self.pause_button.setEnabled(self.running);self.report_button.setEnabled(bool(self.job))
    def open_output(self):
        destination=getattr(self,'output','') or self.target.text()
        if Path(destination).is_dir():QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(destination).resolve())))
    def open_report(self):
        if self.job and self.job.is_dir():QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.job)))
