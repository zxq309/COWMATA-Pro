"""Directory selection must stay responsive and never read video contents."""
import threading,time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from cowmata_tailring.workspace import organization as core
from cowmata_tailring.workspace.organization_ui import OrganizationWindow
from cowmata_tailring.workspace import dahua_tasks

def test_normal_directory_selection_returns_before_slow_enumeration(tmp_path,monkeypatch):
 app=QApplication.instance() or QApplication([])
 root=tmp_path/'videos';view=root/'视角01';view.mkdir(parents=True)
 for i in range(4):(view/(str(i)+'.mp4')).write_bytes(b'untouched')
 original=core.walk_files
 def slow(*args,**kwargs):
  for path in original(*args,**kwargs):time.sleep(.1);yield path
 monkeypatch.setattr(core,'walk_files',slow)
 window=OrganizationWindow(None);window.show()
 try:
  begin=time.monotonic();window.set_video_root(root);elapsed=time.monotonic()-begin
  assert elapsed<.15,'Directory selection blocked the GUI for '+str(elapsed)
  ticks=0;deadline=time.monotonic()+3
  while getattr(window,'_discovering',False) and time.monotonic()<deadline:
   app.processEvents();time.sleep(.01);ticks+=1
  assert ticks>=2 and not window._discovering
  assert window.sources.rowCount()==1
  assert '（4）' in window.sources.item(0,0).text()
  assert all(p.read_bytes()==b'untouched' for p in view.iterdir())
 finally:window.request_shutdown();window.close();app.processEvents()

def test_dahua_scan_discovers_sources_without_hashing_payloads(tmp_path,monkeypatch):
 root=tmp_path/'raw';root.mkdir();source=root/'one.dav';source.write_bytes(b'raw-video')
 monkeypatch.setattr(dahua_tasks,'digest_file',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Full video read during directory discovery')))
 result=dahua_tasks.scan(dict(mode='files',files=[str(root)]),tmp_path/'job')
 assert len(result['rows'])==1 and result['rows'][0]['source']==str(source)
 assert result['rows'][0]['status']=='discovered'
 assert source.read_bytes()==b'raw-video'


def test_raw_page_uses_disk_dropdown_without_opening_picker(monkeypatch):
 from PySide6.QtWidgets import QFileDialog
 from cowmata_tailring.workspace.dahua_ui import DahuaPanel
 app=QApplication.instance() or QApplication([])
 monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Native folder picker opened')))
 panel=DahuaPanel()
 assert panel.mode.currentIndex()==1
 assert not panel.folder_button.isVisible()
 disk=dict(number=3,model='Recorder',size=8000000000000,identity='fixture',dhfs=True,letters=['G:'])
 panel.apply_disks([disk])
 assert 'G:' in panel.disk_choice.itemText(1) and panel.disk_choice.currentIndex()==0
 calls=[];panel.start=lambda action,request,job=None:calls.append((action,request))
 panel.disk_choice.setCurrentIndex(1);panel.disk_selected(1)
 assert calls==[('scan',dict(mode='disk',disk=disk))]
 panel.active=False;panel.close();app.processEvents()


def test_switching_directory_ignores_stale_background_results(tmp_path,monkeypatch):
 from cowmata_tailring.workspace import video_discovery
 app=QApplication.instance() or QApplication([])
 old=tmp_path/'old';new=tmp_path/'new';old.mkdir();new.mkdir()
 entered=threading.Event();release=threading.Event()
 def discover(root,cancelled,publish):
  root=Path(root)
  if root==old:entered.set();release.wait(3)
  publish(dict(root=str(root),counts={str(root/'视角01'):1},done=True,error=''))
 monkeypatch.setattr(video_discovery,'discover',discover)
 window=OrganizationWindow(None)
 try:
  window.set_video_root(old);assert entered.wait(1)
  window.set_video_root(new)
  deadline=time.monotonic()+3
  while window._discovering and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
  assert not window._discovering and window.sources.rowCount()==1
  release.set();time.sleep(.05);app.processEvents()
  from PySide6.QtCore import Qt
  assert Path(window.sources.item(0,1).data(Qt.ItemDataRole.UserRole))==new/'视角01'
  window.set_video_root(old);window.cancel_directory_scan();time.sleep(.05);app.processEvents()
  assert not window._discovering and window._discovery_incomplete
  assert not window.execute_button.isEnabled()
 finally:release.set();window.request_shutdown();window.close();app.processEvents()


def test_selected_dahua_source_is_hashed_and_modified_source_rejected(tmp_path,monkeypatch):
 import hashlib,os
 source=tmp_path/'one.dav';source.write_bytes(b'original')
 job=tmp_path/'job';index=dahua_tasks.scan(dict(mode='files',files=[str(source)]),job)
 def normalize(src,dst,cancelled):
  data=Path(src).read_bytes();Path(dst).write_bytes(data)
  return dict(sha256=hashlib.sha256(data).hexdigest())
 monkeypatch.setattr(dahua_tasks,'normalize_file',normalize)
 row=index['rows'][0];prepared,_=dahua_tasks.normalized(row,index,job,lambda:False)
 assert row['sha256']==hashlib.sha256(b'original').hexdigest()
 assert prepared.read_bytes()==b'original'
 stamp=source.stat();source.write_bytes(b'changed!');os.utime(source,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
 import pytest
 with pytest.raises(ValueError,match='重新扫描'):dahua_tasks.normalized(row,index,job,lambda:False)
 assert prepared.read_bytes()==b'original'
