"""Ongoing-wear downloads and late ledger updates, using local fake servers only."""

import base64
import csv
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from cowmata_tailring.edge_download.core import CHINA, Cancelled, DownloadError, Job
from cowmata_tailring.edge_download.csv_download import run_csv_job
from cowmata_tailring.edge_download.prediction import PredictionPlan


START = datetime(2026, 9, 25, tzinfo=CHINA)
DEVICE = "0C3D5EA22E1F"
OTHER = "546C50CA07FA"
FIELDS = (
    "记录ID", "设备号", "牛号", "佩戴开始", "佩戴结束", "产犊开始", "产犊结束",
    "九轴", "脉搏", "温度", "监测目的", "数据分类", "已删除",
)


def sample(**changes):
    row = dict.fromkeys(FIELDS, "")
    row.update(记录ID="upstream-id-1", 设备号=DEVICE, 牛号="23414AD30",
               佩戴开始="2026-09-25 00:00:00", 已删除="0")
    row.update(changes)
    return row


def write_samples(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "样本试验台账.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return folder


def job_for(tmp_path, rows=None):
    ledger = write_samples(tmp_path / "ledger", [sample()] if rows is None else rows)
    return Job("http://example.test", tmp_path / "farm", "未分类", (),
               ("motion", "pulse", "temp"), START, START + timedelta(days=10), ledger)


def sensor(kind="motion", stamp=None, uid=1, device=DEVICE, cow="23414AD30"):
    stamp = stamp or START + timedelta(hours=1)
    row = dict(uid=uid, device=device, cow_id=cow, create_time=int(stamp.timestamp() * 1000))
    if kind == "motion":
        row.update(version=2, imu=base64.b64encode(bytes(22)).decode())
    elif kind == "pulse":
        row.update(data="AQACAA==", ir_data="AwAEAA==", imu_data=None)
    else:
        row.update(data=36.5)
    return kind, row


class Server:
    """Every request is bounded and inspectable; never opens a connection."""

    def __init__(self, *documents):
        self.documents = list(documents)
        self.listings = []
        self.details = []
        self.fail_listing = False
        self.cancel_listing = False
        self.fail_uid = None

    def factory(self, base_url, cancel, log):
        server = self

        class Client:
            def check(self):
                if cancel.is_set():
                    raise Cancelled()

            def listing(self, target, lo, hi, kinds):
                server.listings.append((target.device, lo, hi))
                if server.fail_listing:
                    raise DownloadError("simulated listing failure")
                if server.cancel_listing:
                    cancel.set()
                    raise Cancelled()
                for kind, data in server.documents:
                    stamp = datetime.fromtimestamp(data["create_time"] / 1000, CHINA)
                    if kind in kinds and data["device"] == target.device and lo <= stamp < hi:
                        yield kind, data["uid"], data["device"], data.get("cow_id", "")

            def record(self, kind, uid, device, cow):
                server.details.append((kind, uid))
                if uid == server.fail_uid:
                    raise DownloadError("simulated detail failure")
                return next(dict(data) for modality, data in server.documents
                            if modality == kind and data["uid"] == uid and data["device"] == device)

        return Client()


def run(job, server, now=None, **kwargs):
    return run_csv_job(job, threading.Event(), client_factory=server.factory,
                       now=now or START + timedelta(days=2, hours=10), **kwargs)


def csv_rows(root, name="预测台账.csv"):
    with (root / "待预测" / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def add_prediction(root, name, text):
    path = root / "待预测" / name
    rows = csv_rows(root, name)
    fields = [*rows[0], "算法预测时间", "算法预测结果"]
    for row in rows:
        row.update(算法预测时间="2026-09-25T09:00:00+08:00", 算法预测结果=text)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def raw_files(root):
    return sorted(path for path in root.rglob("*.json")
                  if any(part in ("Motion", "PPG", "Temp") for part in path.relative_to(root).parts))


def test_core_sheet_alone_downloads_three_modalities_with_no_labels(tmp_path):
    job = job_for(tmp_path)
    server = Server(*(sensor(kind) for kind in ("motion", "pulse", "temp")))
    result = run(job, server)
    assert (result.saved, result.failed) == (3, 0)
    assert len(raw_files(job.farm)) == 3
    assert all(path.is_relative_to(job.farm / "待预测") for path in raw_files(job.farm))
    tracked = csv_rows(job.farm)
    active = csv_rows(job.farm, "待预测清单.csv")
    assert len(tracked) == len(active) == 1
    assert tracked[0]["设备号"] == DEVICE
    assert tracked[0]["产犊开始"] == tracked[0]["佩戴结束"] == ""
    paths = json.loads(tracked[0]["原始数据路径"])
    assert {str((job.farm / value).resolve()) for value in paths} == {
        str(path.resolve()) for path in raw_files(job.farm)
    }
    assert int(tracked[0]["已下载文件数"]) == 3


@pytest.mark.parametrize("changes", [
    {"九轴": "无效", "脉搏": "无效", "温度": "无效"},
    {"监测目的": "待定", "数据分类": "review"},
    {"产犊开始": "待定", "产犊结束": "待定"},
    {"产犊开始": "2026-09-25 10:00:00", "产犊结束": "2026-09-25 11:00:00"},
])
def test_labels_and_validity_do_not_gate_ongoing_download(tmp_path, changes):
    job = job_for(tmp_path, [sample(**changes)])
    server = Server(sensor())
    assert run(job, server).saved == 1
    assert csv_rows(job.farm, "待预测清单.csv")[0]["设备号"] == DEVICE


@pytest.mark.parametrize("value", ["07FA", "0C3D5EA22E1", "0C3D5EA22E1FF", "0C3D5EA22E1G", ""])
def test_sample_device_requires_exact_twelve_hex_digits(tmp_path, value):
    job = job_for(tmp_path, [sample(设备号=value)])
    server = Server()
    assert run(job, server).saved == 0
    assert server.listings == []
    assert csv_rows(job.farm, "待预测清单.csv") == []


def test_lowercase_device_normalizes_and_keeps_leading_zero(tmp_path):
    job = job_for(tmp_path, [sample(设备号="  " + DEVICE.lower() + "  ")])
    server = Server(sensor())
    assert run(job, server).saved == 1
    assert {row[0] for row in server.listings} == {DEVICE}
    assert csv_rows(job.farm)[0]["设备号"] == DEVICE


@pytest.mark.parametrize("value", ["", "not-a-date", "########"])
def test_missing_or_invalid_start_never_downloads(tmp_path, value):
    job = job_for(tmp_path, [sample(佩戴开始=value)])
    server = Server()
    assert run(job, server).saved == 0
    assert server.listings == []


@pytest.mark.parametrize("value", ["/", "无", "待定", "########", "2026-09-24 10:00:00"])
def test_nonempty_invalid_end_is_not_treated_as_ongoing(tmp_path, value):
    job = job_for(tmp_path, [sample(佩戴结束=value)])
    server = Server()
    assert run(job, server).saved == 0
    assert server.listings == []
    assert csv_rows(job.farm, "待预测清单.csv") == []


@pytest.mark.parametrize("calved", [False, True])
def test_first_import_does_not_start_historical_closed_wear(tmp_path, calved):
    values = dict(佩戴结束="2026-09-26 12:00:00")
    if calved:
        values.update(产犊开始="2026-09-26 10:00:00", 产犊结束="2026-09-26 11:00:00",
                      九轴="有效", 脉搏="有效", 温度="有效")
    job = job_for(tmp_path, [sample(**values)])
    server = Server(sensor())
    assert run(job, server).saved == 0
    assert server.listings == []
    assert csv_rows(job.farm, "待预测清单.csv") == []


def test_preview_does_not_persist_or_enroll_closed_record(tmp_path):
    job = job_for(tmp_path)
    plan = PredictionPlan(job.ledger_directory, job.farm)
    assert plan.ready
    assert list(plan.bounds(START, START + timedelta(days=1)))
    assert not (job.farm / ".edge-download" / "prediction-tracking.json").exists()
    write_samples(job.ledger_directory, [sample(佩戴结束="2026-09-26 12:00:00")])
    server = Server(sensor())
    assert run(job, server).saved == 0
    assert server.listings == []


def test_completed_wear_fetches_tail_once_and_later_calving_edits_do_not_query(tmp_path):
    job = job_for(tmp_path)
    tail_time = START + timedelta(days=1, hours=11)
    end = START + timedelta(days=1, hours=12)
    server = Server(sensor(), sensor(stamp=tail_time, uid=2))
    assert run(job, server, now=START + timedelta(days=1, hours=10)).saved == 1
    original = {path: path.read_bytes() for path in raw_files(job.farm)}
    old_id = csv_rows(job.farm)[0]["记录ID"]
    add_prediction(job.farm, "预测台账.csv", "预计产犊")
    write_samples(job.ledger_directory, [sample(佩戴结束=end.isoformat())])
    assert run(job, server).saved == 1
    assert all(hi <= end for _, _, hi in server.listings)
    server.listings.clear()
    write_samples(job.ledger_directory, [sample(
        佩戴结束=end.isoformat(), 产犊开始="2026-09-26 10:00:00", 产犊结束="2026-09-26 11:00:00",
        九轴="有效", 脉搏="有效", 温度="有效",
    )])
    assert run(job, server, now=START + timedelta(days=5)).saved == 0
    assert server.listings == []
    tracked = csv_rows(job.farm)
    assert len(tracked) == 1 and tracked[0]["记录ID"] == old_id
    assert tracked[0]["产犊结束"] == "2026-09-26 11:00:00"
    assert tracked[0]["算法预测结果"] == "预计产犊"
    assert csv_rows(job.farm, "待预测清单.csv") == []
    assert all(path.read_bytes() == raw for path, raw in original.items())
    assert len(raw_files(job.farm)) == 2


def test_reorder_and_reissued_upstream_ids_preserve_stable_prediction_fields(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server)
    old_id = csv_rows(job.farm)[0]["记录ID"]
    add_prediction(job.farm, "预测台账.csv", "主清单预测")
    add_prediction(job.farm, "待预测清单.csv", "待预测清单预测")
    write_samples(job.ledger_directory, [
        sample(设备号=OTHER, 牛号="23077E", 记录ID="new-row", 佩戴结束="2026-09-26 01:00:00"),
        sample(设备号=DEVICE.lower(), 记录ID="reissued-id", 佩戴开始=START.isoformat(), 九轴="有效"),
    ])
    run(job, server)
    tracked, active = csv_rows(job.farm), csv_rows(job.farm, "待预测清单.csv")
    assert len(tracked) == len(active) == 1
    assert tracked[0]["记录ID"] == active[0]["记录ID"] == old_id
    assert tracked[0]["源行号"] == "3"
    assert tracked[0]["源记录ID"] == "reissued-id"
    assert tracked[0]["算法预测结果"] == "主清单预测"
    assert active[0]["算法预测结果"] == "待预测清单预测"
    assert len(raw_files(job.farm)) == 1


def test_current_day_end_waits_for_next_day_before_finishing(tmp_path):
    job = job_for(tmp_path)
    end = START + timedelta(days=1, hours=12)
    server = Server(sensor(), sensor(stamp=end - timedelta(minutes=1), uid=2))
    run(job, server, now=START + timedelta(days=1, hours=9))
    write_samples(job.ledger_directory, [sample(佩戴结束=end.isoformat())])
    run(job, server, now=START + timedelta(days=1, hours=15))
    assert all(hi <= START + timedelta(days=1) for _, _, hi in server.listings)
    assert ("motion", 2) not in server.details
    assert run(job, server, now=START + timedelta(days=2, hours=1)).saved == 1
    server.listings.clear()
    run(job, server, now=START + timedelta(days=3))
    assert server.listings == []


def test_prediction_columns_only_in_active_csv_survive_closing_in_main_ledger(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server, now=START + timedelta(days=1, hours=9))
    add_prediction(job.farm, "待预测清单.csv", "补录前的独立预测")
    write_samples(job.ledger_directory, [sample(佩戴结束="2026-09-26 12:00:00")])
    run(job, server)
    assert csv_rows(job.farm, "待预测清单.csv") == []
    assert csv_rows(job.farm)[0]["算法预测结果"] == "补录前的独立预测"
    assert csv_rows(job.farm)[0]["算法预测时间"] == "2026-09-25T09:00:00+08:00"


@pytest.mark.parametrize("failure", ["listing", "detail", "cancel"])
def test_failed_or_cancelled_tail_is_retried_after_restart(tmp_path, failure):
    job = job_for(tmp_path)
    server = Server(sensor(), sensor(stamp=START + timedelta(days=1, hours=11), uid=2))
    run(job, server, now=START + timedelta(days=1, hours=9))
    write_samples(job.ledger_directory, [sample(佩戴结束="2026-09-26 12:00:00")])
    server.fail_listing = failure == "listing"
    server.cancel_listing = failure == "cancel"
    server.fail_uid = 2 if failure == "detail" else None
    unsuccessful = run(job, server)
    assert unsuccessful.canceled if failure == "cancel" else unsuccessful.failed > 0
    restarted = Server(*server.documents)
    repaired = run(job, restarted)
    assert repaired.saved == 1 and repaired.failed == 0
    assert ("motion", 2) in restarted.details
    restarted.listings.clear()
    run(job, restarted, now=START + timedelta(days=4))
    assert restarted.listings == []


def test_core_sheet_missing_refuses_download(tmp_path):
    job = job_for(tmp_path)
    (job.ledger_directory / "样本试验台账.csv").unlink()
    server = Server()
    with pytest.raises(DownloadError):
        run(job, server)
    assert server.listings == []


def test_auxiliary_conflicts_cannot_change_core_sheet_identity_or_close_it(tmp_path):
    job = job_for(tmp_path)
    (job.ledger_directory / "扬大测试设备台账.csv").write_text(
        f"设备编码,新佩戴牛号,日期,记录类型\n{DEVICE},23077E,2026-09-25 00:30:00,佩戴\n",
        encoding="utf-8-sig",
    )
    (job.ledger_directory / "扬大产犊登记汇总.csv").write_text(
        "牛号,生产日期,牛场登记生产时间\n23414,2026-09-25,死胎\n", encoding="utf-8-sig",
    )
    server = Server(sensor())
    assert run(job, server).saved == 1
    assert csv_rows(job.farm)[0]["佩戴结束"] == ""


def test_future_start_is_not_queried_until_covered_by_cutoff(tmp_path):
    job = job_for(tmp_path, [sample(佩戴开始="2026-09-27 09:00:00")])
    server = Server(sensor(stamp=START + timedelta(days=2, hours=10)))
    assert run(job, server, now=START + timedelta(days=2, hours=14)).saved == 0
    assert server.listings == []
    assert run(job, server, now=START + timedelta(days=3, hours=1)).saved == 1


def test_closed_wear_does_not_finish_when_requested_job_range_is_incomplete(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor(), sensor(stamp=START + timedelta(days=1, hours=11), uid=2))
    run(replace(job, end=START + timedelta(hours=12)), server)
    write_samples(job.ledger_directory, [sample(佩戴结束="2026-09-26 12:00:00")])
    run(replace(job, start=START + timedelta(days=1)), server)
    server.listings.clear()
    run(job, server)
    assert server.listings, "Unqueried middle interval must prevent final completion"
    server.listings.clear()
    run(job, server)
    assert server.listings == []


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_identity_with_conflicting_end_pauses_regardless_of_row_order(tmp_path, reverse):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server, now=START + timedelta(days=1, hours=9))
    rows = [sample(), sample(记录ID="duplicate-row", 佩戴结束="2026-09-26 12:00:00")]
    write_samples(job.ledger_directory, rows[::-1] if reverse else rows)
    server.listings.clear()
    run(job, server)
    assert server.listings == []
    plan = PredictionPlan(job.ledger_directory, job.farm)
    assert any("结束时间" in issue["message"] for issue in plan.issues)
    assert csv_rows(job.farm, "待预测清单.csv") == []


def test_deleted_file_and_failed_repair_clear_previous_finished_state(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server, now=START + timedelta(days=1, hours=9))
    write_samples(job.ledger_directory, [sample(佩戴结束="2026-09-26 00:00:00")])
    run(job, server)
    assert csv_rows(job.farm)[0]["下载状态"] == "已完成收尾"
    raw_files(job.farm)[0].unlink()
    server.fail_listing = True
    failed = run(job, server)
    assert failed.failed > 0
    tracked = csv_rows(job.farm)[0]
    assert tracked["下载状态"] != "已完成收尾"
    assert tracked["已下载文件数"] == "0"
    server.fail_listing = False
    assert run(job, server).saved == 1
    assert csv_rows(job.farm)[0]["下载状态"] == "已完成收尾"


def test_valid_but_modified_local_payload_requires_remote_check_and_preserves_conflict(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server)
    path = raw_files(job.farm)[0]
    changed = json.loads(path.read_bytes())
    changed["imu"] = base64.b64encode(bytes(21) + b"\x01").decode()
    modified_bytes = json.dumps(changed).encode("utf-8")
    path.write_bytes(modified_bytes)
    server.details.clear()
    result = run(job, server)
    assert server.details == [("motion", 1)]
    assert result.failed > 0, "A differing valid original must be reported, not silently re-approved"
    assert path.read_bytes() == modified_bytes


def test_conflicting_cows_cannot_receive_each_others_cached_files(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server)
    original = {path: path.read_bytes() for path in raw_files(job.farm)}
    write_samples(job.ledger_directory, [
        sample(),
        sample(记录ID="overlap", 牛号="23077E"),
        sample(记录ID="unrelated", 设备号=OTHER, 牛号="23111E"),
    ])
    server.listings.clear()
    run(job, server)
    tracked = {row["牛号"]: row for row in csv_rows(job.farm)}
    assert tracked["23077E"]["原始数据路径"] == "[]"
    assert tracked["23077E"]["已下载文件数"] == "0"
    assert {row["设备号"] for row in csv_rows(job.farm, "待预测清单.csv")} == {OTHER}
    assert all(device == OTHER for device, _, _ in server.listings)
    assert all(path.read_bytes() == before for path, before in original.items())
    plan = PredictionPlan(job.ledger_directory, job.farm)
    assert any("重叠" in issue["message"] for issue in plan.issues)


def test_selected_download_still_revokes_completion_for_missing_unselected_original(tmp_path):
    job = job_for(tmp_path)
    server = Server(sensor())
    run(job, server, now=START + timedelta(days=1, hours=9))
    closed = sample(佩戴结束="2026-09-26 00:00:00")
    write_samples(job.ledger_directory, [closed])
    run(job, server)
    assert csv_rows(job.farm)[0]["下载状态"] == "已完成收尾"
    raw_files(job.farm)[0].unlink()
    write_samples(job.ledger_directory, [
        closed, sample(设备号=OTHER, 牛号="23111E", 记录ID="unrelated"),
    ])
    server.listings.clear()
    selected = [{"device": OTHER, "start": START.isoformat(), "end": ""}]
    run(job, server, only=selected)
    tracked = {row["设备号"]: row for row in csv_rows(job.farm)}
    assert tracked[DEVICE]["下载状态"] != "已完成收尾"
    assert tracked[DEVICE]["已下载文件数"] == "0"
    assert all(device == OTHER for device, _, _ in server.listings)
    assert run(job, server).saved == 1
    assert {row["设备号"]: row for row in csv_rows(job.farm)}[DEVICE]["下载状态"] == "已完成收尾"
