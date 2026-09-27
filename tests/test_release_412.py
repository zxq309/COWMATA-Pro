import json,time,zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication,QPushButton
from cowmata_tailring.workspace import collaboration_packages as packages
from cowmata_tailring.workspace.farm_layout import initialize_farm
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.catalog import Catalog


def test_selected_date_package_preserves_exact_tree_and_bytes(tmp_path,monkeypatch):
    root=tmp_path/'牧场';initialize_farm(root)
    paths={}
    for day in ['2026-08-17','2026-08-18']:
        for kind in ['Motion','PPG','Temp']:
            rel=f'产犊/{kind}/{day}/546C50CA07FA-23077-E/{day}_17-01-33.json'
            payload=json.dumps({'device':'old-device','cow_id':99999,'example':'原样保留'}).encode()
            p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload);paths[rel]=payload
        p=root/f'录像/{day}/视角01/{day}_00-00-00.mp4';p.parent.mkdir(parents=True);p.write_bytes(b'unchanged video');paths[p.relative_to(root).as_posix()]=p.read_bytes()
    units=[u for u in packages.inventory(root,'产犊') if u['day']=='2026-08-17']
    plans=packages.plan_dispatch_groups(root,[units])
    out=packages.dispatch(root,plans)[0]
    with zipfile.ZipFile(out) as z:
        chosen={r:body for r,body in paths.items() if '/2026-08-17/' in r}
        for rel,body in chosen.items():assert z.read(root.name+'/'+rel)==body
        assert not any('/2026-08-18/' in n for n in z.namelist())
        assert all(n.endswith(('.json','.mp4')) for n in z.namelist())


def test_compact_controls_and_close_annotation_releases_lease(tmp_path):
    app=QApplication.instance() or QApplication([])
    window=MainWindow();window.show();app.processEvents()
    catalog=Catalog(tmp_path);window.catalog=catalog;window.board.catalog=catalog
    buttons=window.centralWidget().findChildren(QPushButton)
    assert not any(b.text()=='打开工程' and b.isVisible() for b in buttons)
    assert any(b.text()=='关闭标注' for b in buttons)
    assert window.plot.layout().indexOf(window.plot.sheets)>window.plot.layout().indexOf(window.plot.wave_scroll)
    assert window.stage.wave_ratio>=.5
    window.close_annotation_session()
    deadline=time.monotonic()+10
    while window.catalog is not None and time.monotonic()<deadline:
        app.processEvents();time.sleep(.01)
    assert window.catalog is None
    assert window.board.catalog is None
    second=Catalog(tmp_path);assert not second.readonly;second.close()
    window.close()


def test_classified_dahua_uses_content_clock_not_adjacent_mtime(tmp_path,monkeypatch):
    from cowmata_tailring.media import classified_dahua as module
    from cowmata_tailring.media.dahua_duration import DahuaProgramScan
    path=tmp_path/'2026-09-13_14-34-13.mp4';path.write_bytes(b'fixture')
    monkeypatch.setattr(module,'is_dahua_program_stream',lambda _:True)
    monkeypatch.setattr(module,'scan_dahua_program_stream',lambda _:DahuaProgramScan(32161,((0,0),(100,3)),((1,0,False,True),(4,100,False,True)),66.6686996))
    result=module.classified_dahua_timeline(path)
    assert 2144100<result.duration_ms<2144200
    assert result.native['patch_timestamps']


def test_old_filename_timing_is_queued_for_recheck(tmp_path):
    from cowmata_tailring.workspace.storage import atomic_json
    cat=Catalog(tmp_path,stability_seconds=0)
    try:
        video=tmp_path/'2026-09-13_14-34-13.mp4';video.write_bytes(b'fixture')
        cat.scan(fast=True)
        cat.index_one(video.name,lambda *_:dict(time_engine='cowmata-classified-filename-1',duration_ms=1000,needs_review=True,intervals=[]))
        # Emulate the persisted 4.1.1 timing record.
        row=cat.rows()[0];meta={**row['metadata'],'time_engine':'cowmata-classified-filename-1'}
        cat.update_metadata(row['asset_id'],meta)
        assert cat.queue_ocr_upgrade('current',time_signature='current')==1
        assert cat.rows()[0]['state']=='pending'
    finally:cat.close()
