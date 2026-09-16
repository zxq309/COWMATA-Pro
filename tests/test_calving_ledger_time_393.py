import csv
from datetime import datetime
import pytest
from cowmata_tailring.edge_download.core import CHINA
from cowmata_tailring.algorithms.decision import attach_outcomes

@pytest.mark.parametrize('day,clock,cow',[
 ('2026-09-01','22:00:00','90001'),
 ('2026-09-01','2026-09-01 22:00:00','90001'),
 ('','2026-09-01T14:00:00+00:00','90001A'),
 ('2026/09/01','2026/09/01 22:00:00','90001'),
])
def test_outcome_accepts_ledger_full_or_separate_timestamp(tmp_path,day,clock,cow):
    ledger=tmp_path/'birth.csv'
    with ledger.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,['生产日期','牛场登记生产时间','牛号']);writer.writeheader();writer.writerow({'生产日期':day,'牛场登记生产时间':clock,'牛号':cow})
    now=int(datetime(2026,9,1,10,tzinfo=CHINA).timestamp()*1000)
    rows=attach_outcomes([dict(cow_id='90001',decision_epoch_ms=now)],ledger)
    assert len(rows)==1 and rows[0]['outcome']==1
    assert rows[0]['calving_epoch_ms']==now+12*3600000

def test_date_only_and_deleted_outcomes_do_not_invent_birth_time(tmp_path):
    ledger=tmp_path/'birth.csv'
    with ledger.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,['生产日期','牛场登记生产时间','牛号','已删除']);writer.writeheader()
        writer.writerows([{'生产日期':'2026-09-02','牛号':'90001'}, {'生产日期':'2026-09-01','牛场登记生产时间':'22:00','牛号':'90001','已删除':'1'}])
    assert attach_outcomes([dict(cow_id='90001',decision_epoch_ms=1788228000000)],ledger)==[]
