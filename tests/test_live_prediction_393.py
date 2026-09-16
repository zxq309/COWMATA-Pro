import base64, hashlib, json, struct
from pathlib import Path
import numpy as np
import pytest
from cowmata_tailring.algorithms.decision import FEATURES, SCHEMA
from cowmata_tailring.algorithms.models import export_forest
from cowmata_tailring.temperature import CONTRACT


def model(root):
    from sklearn.ensemble import RandomForestClassifier
    root.mkdir()
    fitted=RandomForestClassifier(n_estimators=2,max_depth=2,random_state=3).fit(np.zeros((4,len(FEATURES)*2)),[0,1,0,1])
    payload=export_forest(fitted,list(FEATURES)+[f+'_missing' for f in FEATURES],np.zeros(len(FEATURES)*2))
    blob=json.dumps(payload).encode();(root/'forest.json').write_bytes(blob)
    doc=dict(schema=SCHEMA,complete=True,version='single-json-test',algorithm='random_forest',feature_names=list(FEATURES),temperature_contract=CONTRACT,median=[0]*len(FEATURES),model_file='forest.json',sha256=hashlib.sha256(blob).hexdigest(),feature_coverage=[1]*len(FEATURES),behavior_models=[],horizon_hours=24,threshold=.5,feature_importance={},metrics={})
    (root/'decision.json').write_text(json.dumps(doc))
    return root


def raw(path,cow='90001A',temperature=True):
    frames=b''.join(struct.pack('<I9h',1010+i*20,0,0,4096,0,0,0,1,2,3) for i in range(6001))
    doc=dict(device='ABCDEF000001',cow_id=cow,version=2,imu=base64.b64encode(frames).decode(),create_time=1788228000000,update_time=1788228121010)
    if temperature:doc['temperature']=base64.b64encode(struct.pack('<2h',3850,3860)).decode()
    path.write_text(json.dumps(doc));return path



def test_folder_needs_no_ledger_and_accepts_flat_json_cow_id(tmp_path):
    from cowmata_tailring.algorithms.live_prediction import predict_folder
    folder=tmp_path/'input';folder.mkdir();p=raw(folder/'one.json');before=p.read_bytes()
    result=predict_folder(folder,model(tmp_path/'model'),tmp_path/'out',tmp_path/'cache',tmp_path/'models')
    assert result['rows'][-1]['cow_id']=='90001' and result['rows'][-1]['risk_score'] is not None
    assert result['rows'][-1]['temperature_c']==pytest.approx(38.55)
    assert result['input']['mode']=='folder' and result['input']['sensor_records']==1
    assert p.read_bytes()==before and result['coverage'][0]['span_hours']<1
    assert result['rows'][0]['history_status']=='参考历史不足24小时'
    assert (tmp_path/'out/数据覆盖.csv').is_file() and (tmp_path/'out/连续预警时段.csv').is_file()


def test_folder_does_not_mix_unidentified_cow_baselines(tmp_path):
    from cowmata_tailring.algorithms.evidence import add_baselines
    rows=[dict(cow_id='',asset_id=str(i),start_epoch_ms=i*600000,end_epoch_ms=(i+1)*600000,activity_index=float(i),temperature_c=38.) for i in range(8)]
    add_baselines(rows)
    assert all(r['baseline_windows']==0 and r['temperature_c_change'] is None for r in rows)


def test_folder_conflicting_cow_and_missing_ppg_timing_are_reported(tmp_path):
    from cowmata_tailring.algorithms.live_prediction import scan_folder
    folder=tmp_path/'ABCDEF000001-90002-A';folder.mkdir();raw(folder/'one.json')
    index,temps,count=scan_folder(folder)
    assert count==1 and not index['records'] and len(index['issues'])==1
    assert '不一致' in index['issues'][0]['reason']


def test_folder_temperature_only_does_not_invent_risk(tmp_path):
    from cowmata_tailring.algorithms.live_prediction import predict_folder
    folder=tmp_path/'input';folder.mkdir();(folder/'temp.json').write_text(json.dumps(dict(device='ABCDEF000001',cow_id='90001A',data=38.5,create_time=1788228000000)))
    with pytest.raises(ValueError,match='只有温度'):
        predict_folder(folder,model(tmp_path/'model'),tmp_path/'out',tmp_path/'cache',tmp_path/'models')


def test_forecast_windows_use_actual_model_horizon_and_keep_gap_visible():
    from cowmata_tailring.algorithms.live_prediction import temporal_summary
    rows=[]
    for stamp,score in [(0,.9),(600000,.8),(3600000,None),(86400000,.7)]:
        rows.append(dict(cow_id='90001',device_id='ABCDEF000001',field_mark='A',asset_id=str(stamp),start_epoch_ms=stamp,end_epoch_ms=stamp+600000,decision_epoch_ms=stamp+620000,horizon_hours=6,motion_coverage=1,ppg_coverage=None,risk_score=score,warning_level='关注并复核' if score else '数据不足'))
    coverage,alerts=temporal_summary(rows)
    assert rows[0]['forecast_end_ms']==rows[0]['decision_epoch_ms']+6*3600000
    assert len(alerts)==2 and alerts[0]['windows']==2
    assert coverage[0]['largest_gap_hours']>20
    assert coverage[0]['effective_signal_hours']==pytest.approx(2/3,abs=.001)


def test_missing_behavior_dependency_never_falls_back(tmp_path):
    from cowmata_tailring.algorithms.live_prediction import predict_folder
    directory=model(tmp_path/'model');manifest=directory/'decision.json';doc=json.loads(manifest.read_text());doc['behavior_models']=[dict(version='expected',sha256='0'*64,codes=['STANDING_UP'])];manifest.write_text(json.dumps(doc))
    folder=tmp_path/'input';folder.mkdir();raw(folder/'one.json')
    with pytest.raises(ValueError,match='缺少'):
        predict_folder(folder,directory,tmp_path/'out',tmp_path/'cache',tmp_path/'empty-models')


def test_folder_entry_requires_manual_enable_and_no_training_inputs(qt_application,tmp_path,monkeypatch):
    from cowmata_tailring.algorithms.decision_ui import DecisionWindow
    monkeypatch.setenv('COWMATA_ALGORITHM_HOME',str(tmp_path/'models/行为识别'))
    window=DecisionWindow();calls=[];monkeypatch.setattr(window,'launch',lambda request:calls.append(request))
    window.model.setText(str(model(tmp_path/'model')))
    monkeypatch.setattr('cowmata_tailring.algorithms.decision_ui.QFileDialog.getExistingDirectory',lambda *a:str(tmp_path/'input'))
    window.receive_folder();assert not calls
    window.folder_enabled.setChecked(True);assert not calls
    window.receive_folder();assert len(calls)==1 and calls[0]['action']=='folder_predict393'
    assert 'ledger' not in calls[0] and 'evidence' not in calls[0] and 'dataset' not in calls[0]
    window.folder_enabled.setChecked(False);window.receive_folder();assert len(calls)==1
    window.close();window.deleteLater()
