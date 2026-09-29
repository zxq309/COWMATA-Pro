"""4.4.0 待产犊: one classification rule shared by uploader, server receiver and downloader."""
import csv
import io
import sys
import uuid
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server-upgrade"


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.syspath_prepend(str(SERVER))
    for name in ("receiver", "ledger_core", "ledger_pending", "ledger_merge", "ledger_sheets"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import receiver
    return receiver


def record(**values):
    from cowmata_tailring.ledger.ledger_core import FIELDS
    row = dict.fromkeys(FIELDS, "")
    row.update(记录ID=str(uuid.uuid4()), 版本="", 已删除="0", 牧场="扬大_高邮牧场", 牛号="24242R6", 设备号="0C3D5EA22F07",
               佩戴开始="2026-09-27 17:00", 监测目的="产犊监测", 记录日期="2026-09-27")
    row.update(values)
    return row


@pytest.mark.parametrize("values, expected", [
    (dict(), "pending_calving"),
    (dict(监测目的="孕后期监测"), "pending_calving"),
    (dict(产犊开始="/", 产犊结束="/"), "pregnancy_late"),
    (dict(产犊开始="2026-09-28 10:00", 产犊结束="2026-09-28 10:40"), "calving"),
    (dict(佩戴结束="2026-09-28 08:00", 产犊开始="2026-09-29 10:00", 产犊结束="2026-09-29 10:40"), "pregnancy_late"),
    (dict(产犊开始="2026-09-28 10:00"), "review"),
    (dict(产犊开始="昨天", 产犊结束="2026-09-28 10:40"), "review"),
    (dict(监测目的="发情监测"), "estrus"),
    (dict(监测目的="死胎"), "review"),  # an outcome means calved: blank times are an error
    (dict(监测目的="产后监测"), "review"),
])
def test_calving_columns_decide_the_category(values, expected):
    from cowmata_tailring.ledger.ledger_core import classify_record
    assert classify_record(record(**values))[0] == expected


def test_client_and_server_rules_are_identical(server):
    import ledger_core as server_core

    from cowmata_tailring.ledger import ledger_core as client_core
    assert server_core.CATEGORIES == client_core.CATEGORIES
    for values in ({}, {"产犊开始": "/", "产犊结束": "/"}, {"产犊开始": "2026-09-28 10:00", "产犊结束": "2026-09-28 11:00"}):
        assert server_core.classify_record(record(**values)) == client_core.classify_record(record(**values))


def rows_of(path):
    return list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"))))


def test_server_derives_category_and_pending_ledger(server, tmp_path):
    target = tmp_path / "样本试验台账.csv"
    stale = record(数据分类="review")  # an older client still sends 待核对
    reply = server.handle(dict(version=2, action="sync", target=str(target), changes=[dict(base="", record=stale)]), str(target))
    assert reply["ok"]
    saved = rows_of(target)[0]
    assert saved["数据分类"] == "pending_calving" and "/待产犊/" in saved["归类目录"]
    pending = rows_of(tmp_path / "待产犊台账.csv")
    assert [r["记录ID"] for r in pending] == [stale["记录ID"]]
    assert list(pending[0]) == list(saved)  # same columns as the samples sheet

    filled = dict(saved, 产犊开始="2026-09-28 10:00", 产犊结束="2026-09-28 10:40")
    server.handle(dict(version=2, action="sync", target=str(target), changes=[dict(base=saved["版本"], record=filled)]), str(target))
    assert rows_of(target)[0]["数据分类"] == "calving"
    assert rows_of(tmp_path / "待产犊台账.csv") == []
    log = rows_of(tmp_path / "待产犊变更记录.csv")
    assert [(r["变更"], r["新分类"]) for r in log] == [("新增", "待产犊"), ("移出", "产犊")]


def test_pull_creates_missing_pending_ledger(server, tmp_path):
    target = tmp_path / "样本试验台账.csv"
    server.handle(dict(version=2, action="sync", target=str(target), changes=[dict(base="", record=record())]), str(target))
    (tmp_path / "待产犊台账.csv").unlink()
    server.handle(dict(version=2, action="pull", target=str(target)), str(target))
    assert len(rows_of(tmp_path / "待产犊台账.csv")) == 1


def test_downloader_uses_the_same_rule():
    from cowmata_tailring.edge_download.csv_targets import sample_eligibility
    from cowmata_tailring.edge_download.ledger import csv_category
    pending = record(数据分类="review", 九轴="", 脉搏="", 温度="")
    assert csv_category(pending) == "待产犊"
    assert sample_eligibility(pending)[0] == "eligible"
    assert sample_eligibility(dict(pending, 九轴="无效"))[0] == "excluded"
    assert csv_category(dict(pending, 产犊开始="/", 产犊结束="/")) == "怀孕/孕晚期"
    calved = dict(pending, 产犊开始="2026-09-28 10:00", 产犊结束="2026-09-28 10:40", 九轴="有效", 脉搏="有效", 温度="有效")
    assert csv_category(calved) == "产犊" and sample_eligibility(calved)[0] == "eligible"


def test_locked_change_log_keeps_changes_for_next_refresh(server, tmp_path, monkeypatch):
    import ledger_pending
    samples = tmp_path / "样本试验台账.csv"
    from cowmata_tailring.ledger.ledger_core import FIELDS
    with samples.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(record())
    real = ledger_pending._atomic

    def locked(path, data):
        if path.name == ledger_pending.CHANGES_FILE:
            raise PermissionError("open in Excel")
        real(path, data)
    monkeypatch.setattr(ledger_pending, "_atomic", locked)
    with pytest.raises(PermissionError):
        ledger_pending.refresh(samples)
    assert not (tmp_path / ledger_pending.PENDING_FILE).exists()
    monkeypatch.setattr(ledger_pending, "_atomic", real)
    assert ledger_pending.refresh(samples)["新增"] == 1
    assert (tmp_path / ledger_pending.CHANGES_FILE).exists()
