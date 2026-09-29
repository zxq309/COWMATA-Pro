import json,os,subprocess,time
from pathlib import Path
from PySide6.QtCore import QObject,QThread,QTimer,Signal
from PySide6.QtWidgets import QApplication,QAbstractItemView
from .ledger_update import check_update,download
from .ledger_version import VERSION
class UpdateWorker(QThread):
    status=Signal(str);ready=Signal(dict,str);done=Signal(str)
    def __init__(self,root,cache,parent=None):super().__init__(parent);self.root=root;self.cache=cache
    def run(self):
        try:
            update=check_update(self.root,VERSION,self.cache)
            if not update:self.done.emit("已是最新版本 "+VERSION);return
            self.status.emit("发现 "+update["version"]+"，正在自动下载")
            installer=download(update,self.cache,lambda n,total:self.status.emit(f"新版自动下载 {n*100//total}%"),self.isInterruptionRequested)
            if not self.isInterruptionRequested():self.ready.emit(update,str(installer))
        except Exception as error:self.done.emit("更新检查稍后重试："+str(error))
class UpdateController(QObject):
    def __init__(self,window,root):
        super().__init__(window);self.window=window;self.root=Path(root);self.worker=None;self.pending=None
        self.install_root=os.environ.get("COWMATA_LEDGER_INSTALL_ROOT")
        self.cache=Path(window.store.root)/"updates";self.next_check=0
        window.update_button.clicked.connect(self.check_now)
        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(1000)
        window.update_status.setText("v"+VERSION+" · 自动更新")
        if not self.install_root:window.update_status.setToolTip("安装版支持自动升级；当前为便携目录")
    def check_now(self):
        """Run an explicit update check from the visible button."""
        if not self.install_root:
            self.window.update_status.setText("便携版请手动下载")
            self.window.update_status.setToolTip("当前为便携目录，请下载新版便携包后整体替换；不会自动覆盖便携目录")
            return
        if self.pending:
            if QApplication.activeModalWidget() or self.window.table.state()==QAbstractItemView.EditingState or getattr(self.window,"_local_busy",False) or getattr(getattr(self.window,"review_panel",None),"drafts",{}):
                self.window.update_status.setText("请先完成当前编辑")
                self.window.update_status.setToolTip("保存当前编辑或核对后再安装新版；本地台账不会被覆盖")
                return
            self.install_pending()
            return
        if self.worker and self.worker.isRunning():
            self.window.update_status.setToolTip("正在检查或下载更新，请稍候")
            return
        self.next_check=0
        self.tick()

    def tick(self):
        if not self.install_root:return
        if self.pending:
            if QApplication.activeModalWidget() or self.window.table.state()==QAbstractItemView.EditingState:return
            if getattr(self.window,"_local_busy",False) or getattr(getattr(self.window,"review_panel",None),"drafts",{}):return
            if any(w and w.isRunning() for w in (self.window.worker,self.window.importer,getattr(self.window,"local_worker",None),self.worker,getattr(self.window,"account_worker",None))):return
            self.install_pending();return
        if time.monotonic()<self.next_check or (self.worker and self.worker.isRunning()):return
        self.next_check=time.monotonic()+60
        self.worker=UpdateWorker(self.root,self.cache,self)
        self.worker.status.connect(self.window.update_status.setText)
        self.worker.done.connect(self.finished)
        self.worker.ready.connect(self.ready)
        self.worker.finished.connect(self.maybe_close)
        self.worker.start()
    def maybe_close(self):
        if getattr(self.window,"_close_after_update",False):self.window.exit_app()
    def finished(self,text):
        self.window.update_button.setText("检查更新")
        self.window.update_status.setText("v"+VERSION+" · 自动更新")
        self.window.update_status.setToolTip(text)
    def ready(self,update,setup):
        self.pending=(update,setup);self.window.update_button.setText("安装新版");self.window.update_status.setText("新版已就绪，点击安装新版")
    def install_pending(self):
        update,setup=self.pending
        try:
            for store in getattr(self.window,"stores",{"samples":self.window.store}).values():store.export()
            job={"root":self.install_root,"setup":setup,"version":update["version"],"sha256":update["sha256"],"parent_pid":os.getpid()}
            path=self.cache/"install-job.json";path.write_text(json.dumps(job,ensure_ascii=False),encoding="utf8")
            subprocess.Popen([str(self.root/"LedgerUpdateRunner.exe"),str(path)],cwd=self.cache,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            self.pending=None;self.window.update_button.setText("检查更新");self.timer.stop();self.window.exit_app()
        except Exception as error:
            self.pending=None;self.next_check=time.monotonic()+300;self.finished("安装稍后重试："+str(error))
