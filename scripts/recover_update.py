"""User-launched bridge for clients whose old updater cannot start."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget
from cowmata_tailring.app.portable_update import local_update
from cowmata_tailring.app.update_ui import prepare_job

class Result(QObject):
    ready=Signal(object)
    failed=Signal(str)

class Recovery(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("COWMATA Pro · 修复旧版更新")
        self.resize(760,300)
        self.running=False
        box=QVBoxLayout(self)
        note=QLabel("用于旧版更新报 403、目录拒绝或无法安装的情况。\n先保存并关闭旧版，再选择旧版 COWMATA.exe 和完整便携 ZIP。\n核验后在原位置更新，已有快捷方式保留。安装版将转为便携版；外部原始数据、标注和模型保留。")
        note.setWordWrap(True);box.addWidget(note)
        self.target=QLineEdit();self.archive=QLineEdit()
        candidate=ROOT.parent/(ROOT.name+".zip")
        if candidate.is_file():self.archive.setText(str(candidate))
        for title,field,filter in (("选择旧版 COWMATA.exe",self.target,"COWMATA.exe (COWMATA.exe)"),("选择完整便携 ZIP",self.archive,"Portable package (*.zip)")):
            row=QHBoxLayout();row.addWidget(field,1);button=QPushButton(title)
            button.clicked.connect(lambda checked=False,f=field,ft=filter:self.choose(f,ft));row.addWidget(button);box.addLayout(row)
        self.status=QLabel("只处理所选旧版程序目录；包校验失败或发现目录内混有用户文件时会停止。")
        self.status.setWordWrap(True);box.addWidget(self.status)
        self.start=QPushButton("核验并更新旧版");self.start.clicked.connect(self.begin);box.addWidget(self.start)
        self.signals=Result(self);self.signals.ready.connect(self.ready);self.signals.failed.connect(self.failed)

    def choose(self,field,filter):
        if self.running:return
        name,_=QFileDialog.getOpenFileName(self,"选择文件",field.text(),filter)
        if name:field.setText(name)

    def begin(self):
        if self.running:return
        target=Path(self.target.text()).resolve()
        archive=Path(self.archive.text()).resolve()
        if target.name != "COWMATA.exe" or not target.is_file() or not archive.is_file():
            self.failed("请先选择旧版 COWMATA.exe 和已下载的完整便携 ZIP。");return
        root=target.parent
        if root==ROOT:
            self.failed("此工具从新版便携目录启动，请选择另一处待升级的旧版。");return
        self.running=True;self.start.setEnabled(False);self.status.setText("正在核验完整更新包和旧版文件清单，请稍候…")
        def work():
            try:
                update=local_update(archive)
                cache=Path(os.environ.get("LOCALAPPDATA",Path.home()))/"COWMATA Annotator/updates"
                cache.mkdir(parents=True,exist_ok=True)
                job=prepare_job(root,archive,update,cache)
                self.signals.ready.emit(job)
            except Exception as exc:self.signals.failed.emit(str(exc))
        threading.Thread(target=work,daemon=True).start()

    def ready(self,job):
        folder=Path(job).parent
        try:
            subprocess.Popen([str(folder/"runtime/pythonw.exe"),"-I","-B",str(folder/"update_worker.py"),str(job)],
                             cwd=folder,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except OSError as exc:self.failed(str(exc));return
        self.running=False;self.start.setEnabled(True)
        self.status.setText("独立更新程序已启动，将显示进度并在成功后打开新版。")
        QMessageBox.information(self,"已启动更新","请保持旧版关闭，等待更新进度完成。\n若旧版仍开着，请正常保存并关闭；更新程序不会强制结束标注进程。")

    def failed(self,message):
        self.running=False;self.start.setEnabled(True);self.status.setText("未启动更新："+message)

    def closeEvent(self,event):
        if self.running:
            self.status.setText("正在校验更新包，请等待校验完成后关闭。");event.ignore()
        else:event.accept()

def main():
    app=QApplication(sys.argv);window=Recovery();window.show();return app.exec()

if __name__=="__main__":
    raise SystemExit(main())
