import csv
import json

import pytest


def table(tmp_path, fields, rows, name='2026-08-07 10_33_59.events.csv'):
    path = tmp_path/'546C50CA01F1-23381-w1'/name
    path.parent.mkdir(exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_behavior_retains_relative_time_and_flags_identity_conflict(tmp_path):
    from cowmata_tailring.workspace.legacy_labels import read_legacy_table
    row = dict(session_id='2026-08-07 10_33_59',cow_id='24178-11',code='STANDING_UP',label='起立过程',
               type='interval',t_start_rel_ms='1259206',t_end_rel_ms='1269950',
               t_start_wall_bj='2026-08-07 10:54:58.367',t_end_wall_bj='2026-08-07 10:55:09.111')
    path = table(tmp_path, list(row), [row])
    result = read_legacy_table(path)
    event = result['events'][0]
    assert event['start_ms'] == 1259206
    assert event['end_ms'] == 1269950
    assert event['identity_status'] == 'conflict'
    assert event['cow_id'] == ''
    assert event['original_row'] == row
    assert event['source_file_sha256']


def test_calving_date_and_unknown_are_not_invented(tmp_path):
    from cowmata_tailring.workspace.legacy_labels import read_legacy_table
    path = table(tmp_path, ['session_id','cow_id','label','time','date'], [
        dict(session_id='缺失',cow_id='23381w1',label='犊牛完全娩出',time='21:22:40',date='2026年8月16日'),
        dict(session_id='',cow_id='23381w1',label='无',time='未知',date='')], name='产犊.csv')
    result = read_legacy_table(path)
    assert len(result['events']) == 1
    event = result['events'][0]
    assert event['code'] == 'CALF_FULLY_EXPELLED'
    assert event['coordinates'] == 'unix_epoch_ms'
    assert event['end_ms'] is None
    assert event['cow_id'] == '23381'
    assert len(result['unresolved']) == 1


def test_boris_times_convert_seconds_once(tmp_path):
    from cowmata_tailring.workspace.legacy_labels import read_legacy_table
    path = table(tmp_path, ['Observation id','Behavior','Start (s)','Stop (s)'], [
        {'Observation id':'session','Behavior':'排尿','Start (s)':'1.125','Stop (s)':'5.625'}], name='session.boris.csv')
    event = read_legacy_table(path)['events'][0]
    assert (event['start_ms'], event['end_ms'], event['code']) == (1125, 5625, 'URINATION')


def test_zero_interval_is_preserved_as_unresolved(tmp_path):
    from cowmata_tailring.workspace.legacy_labels import read_legacy_table
    row = dict(session_id='s', cow_id='23381',code='STANDING_UP',label='起立过程',type='interval',
               t_start_rel_ms='42',t_end_rel_ms='42')
    result = read_legacy_table(table(tmp_path,list(row),[row]))
    assert not result['events']
    assert result['unresolved'][0]['original_row'] == row


def test_annotation_only_absolute_labels_open_in_history(tmp_path):
    from cowmata_tailring.workspace.label_file import load_history, read_label_file
    from cowmata_tailring.workspace.legacy_labels import build_absolute_annotation
    row = dict(session_id='2026-08-16',cow_id='23381',label='犊牛完全娩出',time='21:22:40')
    source = table(tmp_path,list(row),[row])
    doc = build_absolute_annotation(source, category='calving')
    target = tmp_path/'产犊.标注.json'
    target.write_text(json.dumps(doc), encoding='utf-8')
    restored = read_label_file(target)
    assert restored['coordinates'] == 'unix_epoch_ms'
    history = load_history(target)
    assert history.motion is None
    assert len(history.work.project.events) == 1
    assert history.work.project.events[0].t0 == doc['view']['start_ms']


@pytest.mark.parametrize('value', ['nan', 'inf', '-1'])
def test_nonfinite_and_negative_relative_time_rejected(tmp_path, value):
    from cowmata_tailring.workspace.legacy_labels import read_legacy_table
    row = dict(session_id='s', cow_id='23381',code='URINATION',label='排尿',type='interval',
               t_start_rel_ms=value,t_end_rel_ms='10')
    result = read_legacy_table(table(tmp_path,list(row),[row]))
    assert not result['events']
    assert result['unresolved']
