import json
from pathlib import Path

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


def test_390_wrong_default_migrates_without_creating_data(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    # The legacy-default branch is independent of CSV files on the host.
    monkeypatch.setattr('cowmata_tailring.edge_download.pro_settings.SERVER_DIRECTORY', str(tmp_path/'no-existing-records'))
    folder=tmp_path/'preferences'
    folder.mkdir()
    values=defaults(Path(r'F:\牛舍'))
    values.update(schema_390=1,kinds=['motion','pulse','temp'])
    (folder/'automatic-download.json').write_text(json.dumps(values),encoding='utf-8')
    store=ProSettings(folder)
    assert store.value['data_root'] == str(Path(r'F:\扬大_高邮牧场'))
    assert store.value['ledger_directory'] == str(Path(r'F:\牛舍\_现场记录'))
    assert not (tmp_path/'local').exists()
