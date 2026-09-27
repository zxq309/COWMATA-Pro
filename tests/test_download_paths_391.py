import json
import os
from pathlib import Path

import pytest

from cowmata_tailring.edge_download.pro_settings import ProSettings
from cowmata_tailring.edge_download.settings import defaults


def test_upgrade_keeps_explicit_existing_farm(tmp_path):
    folder=tmp_path/'preferences'
    folder.mkdir()
    farm=tmp_path/'existing-farm'
    farm.mkdir()
    values=defaults(farm)
    (folder/'automatic-download.json').write_text(json.dumps(values),encoding='utf-8')
    store=ProSettings(folder)
    assert Path(store.value['data_root']) == farm


@pytest.mark.skipif(os.name != "nt", reason="Migration of native Windows drive paths")
def test_390_wrong_default_migrates_without_creating_data(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    # This host really has an F: drive with live datasets; the migration rule
    # keeps an existing valid root, so the test must judge the F: default as
    # if that drive were absent.
    real_is_dir=Path.is_dir
    def without_f_drive(self,*args,**kwargs):
        if str(self).upper().rstrip('\\')=='F:':
            return False
        return real_is_dir(self,*args,**kwargs)
    monkeypatch.setattr('cowmata_tailring.edge_download.pro_settings.Path.is_dir',without_f_drive)
    folder=tmp_path/'preferences'
    folder.mkdir()
    values=defaults(Path(r'F:\牛舍'))
    values.update(schema_390=1,kinds=['motion','pulse','temp'])
    (folder/'automatic-download.json').write_text(json.dumps(values),encoding='utf-8')
    store=ProSettings(folder)
    assert not str(store.value['data_root']).startswith('F:\\')
    assert not str(store.value['ledger_directory']).startswith('F:\\')
    assert not (tmp_path/'local').exists()
