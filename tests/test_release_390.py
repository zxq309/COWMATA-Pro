import base64
import csv
import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cowmata_tailring.edge_download.core import CHINA, Job


def csvfile(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def ledgers(root):
    csvfile(
        root / "扬大测试设备台账.csv",
        [
            dict(
                日期="2026-08-03",
                新佩戴牛号="21314-D2",
                设备编码="07E5",
                已删除="0",
                记录类型="佩戴",
                **{"拆除时间(掉落）": "2026-08-04"},
            )
        ],
    )
    csvfile(
        root / "样本试验台账.csv",
        [
            dict(
                佩戴开始="2026-08-03T10:00:00",
                佩戴结束="2026-08-04T10:00:00",
                牛号="21314D2",
                设备号="546C50CA07E5",
                数据分类="calving",
                已删除="0",
            )
        ],
    )
    csvfile(
        root / "扬大产犊登记汇总.csv",
        [dict(生产日期="2026-08-04", 牛场登记生产时间="09:00:00", 牛号="21314", 已删除="0")],
    )
    return root


def sensor(kind="motion", cow=21314):
    stamp = int(datetime(2026, 8, 3, 12, tzinfo=CHINA).timestamp() * 1000)
    doc = dict(device="546C50CA07E5", create_time=stamp, version=0, cow_id=str(cow) + "D2")
    if kind == "motion":
        n = 6000
        t = np.arange(n) / 50
        frames = np.zeros((n, 9), dtype="<i2")
        frames[:, 2] = 4096
        frames[:, 0] = (100 * np.sin(t * 3)).astype(int)
        frames[:, 3] = (30 * np.sin(t)).astype(int)
        frames[(t >= 30) & (t < 35), 0] = 2500
        doc["imu"] = base64.b64encode(frames.tobytes()).decode()
    else:
        n = 6000
        t = np.arange(n) / 50
        values = (20000 + 500 * np.sin(t * 7)).astype("<u2")
        values[(t >= 30) & (t < 35)] += 1000
        doc.update(
            data=base64.b64encode(values.tobytes()).decode(),
            sample_rate_hz=50,
            configs=dict(pulse_sample_time=120),
            imu_data=base64.b64encode(np.zeros((n, 3), dtype="<i2").tobytes()).decode(),
        )
    return doc


@pytest.mark.parametrize(
    "raw,expected",
    [("07E5", "546C50CA07E5"), ("546C50CA07E5", "546C50CA07E5"), ("0C3D5EA22DCC", "0C3D5EA22DCC")],
)
def test_device_prefix_only_for_short_ids(raw, expected):
    from cowmata_tailring.edge_download.csv_targets import device_id

    assert device_id(raw) == expected


@pytest.mark.parametrize(
    "value", ["564C5CA00BB", "0C3D5EA22DE1F", "../../07E5", "546C50CA", "not-a-device"]
)
def test_ambiguous_or_bad_device_is_never_guessed(value):
    from cowmata_tailring.edge_download.csv_targets import device_id

    with pytest.raises(ValueError):
        device_id(value)


@pytest.mark.parametrize(
    "cow,folder",
    [
        ("21314-D2", "546C50CA07E5-21314-D2"),
        ("23060-I", "546C50CA07E5-23060-I"),
        ("21314", "546C50CA07E5-21314"),
        ("21314黄1", "546C50CA07E5-21314-黄1"),
    ],
)
def test_csv_to_folder_round_trip(cow, folder):
    from cowmata_tailring.edge_download.csv_targets import cow_identity
    from cowmata_tailring.workspace.device_identity import DeviceIdentity, parse_device_folder

    ear, mark = cow_identity(cow)
    identity = DeviceIdentity("546C50CA07E5", ear, mark)
    assert identity.folder_name == folder
    assert parse_device_folder(folder) == identity


def test_csv_plan_prefers_precise_wear_and_joins_birth_clock(tmp_path):
    from cowmata_tailring.edge_download.csv_targets import CsvPlan

    p = CsvPlan(ledgers(tmp_path))
    wear, reason = p.resolve("546C50CA07E5", datetime(2026, 8, 3, 12, tzinfo=CHINA))
    assert wear.identity.folder_name == "546C50CA07E5-21314-D2" and wear.category == "产犊"
    assert p.births["21314"][0]["time"] == "2026-08-04T09:00:00+08:00"
    assert p.resolve("546C50CA07E5", datetime(2026, 8, 3, 12, tzinfo=CHINA), "99999")[0] is None


def test_download_cache_hash_and_raw_intake(tmp_path):
    from cowmata_tailring.algorithms.inputs import scan_inputs
    from cowmata_tailring.edge_download.csv_download import run_csv_job

    ledger = ledgers(tmp_path / "ledger")
    root = tmp_path / "farm"
    root.mkdir()
    doc = sensor()
    calls = []

    class Fake:
        def __init__(self, *args):
            pass

        def check(self):
            pass

        def listing(self, target, start, end, kinds):
            yield "motion", doc["create_time"], doc["device"], doc["cow_id"]

        def record(self, *args):
            calls.append(args)
            return doc

    start = datetime(2026, 8, 3, 11, tzinfo=CHINA)
    job = Job(
        "http://example.test",
        root,
        "未分类",
        (),
        ("motion",),
        start,
        start + timedelta(hours=2),
        ledger,
    )
    first = run_csv_job(job, threading.Event(), client_factory=Fake)
    assert first.saved == 1 and first.failed == 0
    second = run_csv_job(job, threading.Event(), client_factory=Fake)
    assert second.skipped == 1 and len(calls) == 1
    raw = next((root / "产犊/Motion").rglob("*.json"))
    assert raw.parent.name == "546C50CA07E5-21314-D2"
    assert json.loads(raw.read_text()) == doc
    index = scan_inputs(root)
    assert len(index["records"]) == 1 and not index["issues"], index["issues"]
    raw.write_text("{}")
    third = run_csv_job(job, threading.Event(), client_factory=Fake)
    assert third.saved == 1 and len(calls) == 2
    assert list((root / ".edge-download/recovery").iterdir())


def test_ppg_load_label_pair_and_feature_schema(tmp_path):
    from cowmata_tailring.algorithms.analysis import load_features
    from cowmata_tailring.algorithms.inputs import scan_inputs
    from cowmata_tailring.workspace.catalog import Catalog

    root = tmp_path / "farm/正常"
    folder = root / "PPG/2026-08-03/546C50CA07E5-21314"
    folder.mkdir(parents=True)
    raw = folder / "signal.json"
    raw.write_text(json.dumps(sensor("ppg")))
    for name in ("Motion", "Temp", "Video"):
        (root / name).mkdir()
    result = scan_inputs(root)
    assert len(result["records"]) == 1, result["issues"]
    f = load_features(result["records"][0], tmp_path / "cache")
    assert f["feature_version"] == "ppg-second-features-1"
    assert f["valid_context"].any()
    cat = Catalog(root, day="2026-08-03")
    scan = cat.scan()
    assert raw.relative_to(root).as_posix() in scan.added
    assert [r["path"] for r in cat.rows()] == [raw.relative_to(root).as_posix()]
    assert [r["path"] for r in cat.pending(now=__import__("time").time() + 5)] == [
        raw.relative_to(root).as_posix()
    ]
    cat.close()


@pytest.mark.parametrize("modality", ["motion", "ppg"])
def test_single_model_training_import_and_raw_recognition(tmp_path, modality, monkeypatch):
    from cowmata_tailring.algorithms.inputs import scan_inputs
    from cowmata_tailring.algorithms.recognition import recognize
    from cowmata_tailring.algorithms.registry import install_suite, read_suite
    from cowmata_tailring.algorithms.training import train_suite

    dataset = tmp_path / "dataset"
    folder = dataset / "Standup" / ("PPG" if modality == "ppg" else "Motion")
    (folder / "Raw").mkdir(parents=True)
    (folder / "Label").mkdir()
    for i in range(6):
        doc = sensor(modality, 21314 + i)
        doc["create_time"] += i * 200000
        name = f"546C50CA07E5-{21314 + i}-D2_2026-08-03_12-00-00"
        raw = folder / "Raw" / (name + "_raw.json")
        raw.write_text(json.dumps(doc))
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        label = dict(
            dataset=dict(raw_sha256=sha),
            work=dict(
                asset_id=sha,
                project=dict(
                    cow_id=str(21314 + i),
                    device_identity=dict(
                        device_id="546C50CA07E5", cow_id=str(21314 + i), field_mark="D2"
                    ),
                    events=[dict(id=1, label_code="STANDING_UP", t0=30000, t1=35000)],
                ),
            ),
        )
        (folder / "Label" / (name + "_label.json")).write_text(json.dumps(label))
    index = scan_inputs(dataset, training=True)
    assert len(index["records"]) == 6
    output = tmp_path / "run"
    train_suite(index, tmp_path / "cache", output, codes=["STANDING_UP"], modality=modality)
    assert len(read_suite(output)["models"]) == 1
    imported = install_suite(output, tmp_path / "models", make_active=True)
    result = recognize(
        folder / "Raw", imported, "STANDING_UP", tmp_path / "result", tmp_path / "cache"
    )
    assert result["records"] == 6 and result["rows"] and not result["issues"], result
    assert result["score_is_probability"] is False
    from cowmata_tailring.algorithms.adapter import available_pack, predict_one
    pack=available_pack(read_suite(imported))
    rec=index["records"][0]
    candidate=predict_one(pack,pack["models"][0],rec["raw"],rec["asset_id"],rec["cow_id"],120000,tmp_path/"candidate")
    assert candidate["candidates"]
    assert candidate["audit"]["feature_version"] == ("ppg-second-features-1" if modality=="ppg" else "event-shape-1")


@pytest.mark.parametrize("algorithm", ["decision_tree", "random_forest", "xgboost"])
def test_decision_group_validation_import_roundtrip(tmp_path, algorithm):
    from cowmata_tailring.algorithms.decision import (
        attach_outcomes,
        predict_decision,
        read_decision,
        train_decision,
    )

    rows = []
    births = []
    birth = datetime(2026, 8, 12, 12, tzinfo=CHINA)
    for cow in range(20001, 20007):
        births.append(dict(牛号=str(cow), 生产日期="2026-08-12", 牛场登记生产时间="12:00:00"))
        for h in (3, 6, 12, 18, 30, 36, 48, 60):
            cutoff = (birth - timedelta(hours=h)).timestamp() * 1000
            rows.append(
                dict(
                    cow_id=str(cow),
                    decision_epoch_ms=cutoff,
                    end_epoch_ms=cutoff,
                    source="synthetic",
                    activity_index=h / 60,
                    temperature_c=38 + h / 60,
                    lying_fraction_known=0.3,
                    motion_coverage=1,
                    known_posture_coverage=0.8,
                    straining_seconds=40 if h < 24 else 0,
                )
            )
    ledger = tmp_path / "birth.csv"
    csvfile(ledger, births)
    ev = dict(rows=rows, index_fingerprint="synthetic", behavior_models=[])
    result = train_decision(ev, ledger, tmp_path / algorithm, algorithm)
    assert result["training_cows"] == 6 and result["training_rows"] == 48
    for fold in result["folds"]:
        assert not set(fold["train_cows"]) & set(fold["test_cows"])
    inference = predict_decision(ev, tmp_path / algorithm, tmp_path / "predicted")
    assert len(inference["rows"]) == 48
    assert all(0 <= r["risk_score"] <= 1 for r in inference["rows"])
    assert all(not r["probability_calibrated"] for r in inference["rows"])
    assert not attach_outcomes([dict(cow_id="99999", end_epoch_ms=rows[0]["end_epoch_ms"])], ledger)
    manifest = tmp_path / algorithm / "decision.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="校验"):
        read_decision(manifest)


def test_pro_settings_save_three_modalities_and_ui_preview(tmp_path, monkeypatch):
    from cowmata_tailring.algorithms.behavior_ui import BehaviorWindow
    from cowmata_tailring.algorithms.decision_ui import DecisionWindow
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path / "models"))
    store = ProSettings(tmp_path / "settings")
    store.save(auto_enabled=False, kinds=["motion", "pulse", "temp"], data_root=str(tmp_path / "farm"), ledger_directory=str(ledgers(tmp_path / "ledger")))
    reloaded = ProSettings(tmp_path / "settings")
    assert reloaded.value["kinds"] == ["motion", "pulse", "temp"]
    window = ProDownloadDialog(store=reloaded, launch_automatically=False)
    assert window.plan_table.rowCount() == 2
    behavior = BehaviorWindow()
    assert behavior.algorithm.count() == 6 and behavior.tabs.count() == 2
    decision = DecisionWindow()
    assert decision.tabs.count() == 3 and decision.behavior.count() == 7
    for widget in (window, behavior, decision):
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("suffix", ["", "-黄1"])
@pytest.mark.parametrize("kind", ["Motion", "PPG"])
def test_download_folder_identity_survives_label_dataset_export(tmp_path, suffix, kind):
    from cowmata_tailring.algorithms.inputs import scan_inputs
    from cowmata_tailring.workspace.paired_dataset import build_dataset, paired_label

    root = tmp_path / "farm/正常"
    owner = "546C50CA07E5-21314" + suffix
    raw = root / kind / "2026-08-03" / owner / "2026-08-03_12-00-00.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(sensor("ppg" if kind == "PPG" else "motion")))
    sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    label = paired_label(raw)
    label.parent.mkdir(parents=True)
    label.write_text(
        json.dumps(
            dict(
                format="cowmata-annotation",
                version=1,
                dataset_category="healthy",
                source=dict(asset_id=sha, path=raw.relative_to(root).as_posix()),
                work=dict(
                    asset_id=sha,
                    project=dict(
                        cow_id="21314",
                        source={},
                        labels=[dict(name="起立过程", code="STANDING_UP", type="interval")],
                        events=[dict(id=1, li=0, label_code="STANDING_UP", t0=30000, t1=35000)],
                    ),
                ),
            )
        )
    )
    result = build_dataset([root], tmp_path / "datasets", "behavior", job=tmp_path / "job")
    index = scan_inputs(result["root"], training=True)
    assert len(index["records"]) == 1, index
    record = index["records"][0]
    assert record["cow_id"] == "21314" and record["field_mark"] == suffix.lstrip("-")
    assert record["modality"] == ("ppg" if kind == "PPG" else "motion")
    assert Path(record["raw"]).read_bytes() == raw.read_bytes()


def test_fusion_ppg_evidence_does_not_invent_motion_coverage(tmp_path):
    from cowmata_tailring.algorithms.decision import build_fusion

    root = tmp_path / "farm/正常"
    raw = root / "PPG/2026-08-03/546C50CA07E5-21314/signal.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(sensor("ppg")))
    result = build_fusion(root, [], tmp_path / "evidence", tmp_path / "cache")
    assert result["rows"] and not result["issues"]
    assert all(r["motion_coverage"] == 0 and r["ppg_coverage"] > 0 for r in result["rows"])
    assert all(r["heart_rate_bpm"] is None and r["spo2_percent"] is None for r in result["rows"])


def test_duplicate_raw_with_conflicting_csv_identity_is_not_assigned(tmp_path):
    from cowmata_tailring.algorithms.inputs import scan_inputs

    raw = json.dumps(sensor("ppg"))
    for cow in ("21314", "21315"):
        p = tmp_path / "PPG/2026-08-03" / ("546C50CA07E5-" + cow) / "signal.json"
        p.parent.mkdir(parents=True)
        p.write_text(raw)
    result = scan_inputs(tmp_path)
    assert result["records"] == []
    assert any("不同牛号" in issue["reason"] for issue in result["issues"])


def test_dataset_decision_export_keeps_observations_without_bundled_model(tmp_path):
    from cowmata_tailring.workspace.decision_dataset import export_decision

    raw = sensor("motion")
    raw["update_time"] = raw["create_time"] + 300000
    frames = np.zeros(6000, dtype=[("tick", "<u4"), ("values", "<i2", (9,))])
    frames["tick"] = np.arange(6000) * 20
    frames["values"] = np.frombuffer(base64.b64decode(raw["imu"]), dtype="<i2").reshape(-1, 9)
    raw.update(
        version=2,
        uid="synthetic-packet",
        imu=base64.b64encode(frames.tobytes()).decode(),
        temperature=base64.b64encode(np.array([3800, 3801], dtype="<i2").tobytes()).decode(),
    )
    (tmp_path / "raw.json").write_text(json.dumps(raw))
    (tmp_path / "sources.jsonl").write_text(
        json.dumps(
            dict(
                path="raw.json",
                asset_id="synthetic",
                cow_id="21314",
                device_id=raw["device"],
                identity_eligible=True,
                record_start_ms=raw["create_time"],
            )
        )
    )
    (tmp_path / "events.jsonl").write_text("")
    result = export_decision(tmp_path)
    assert result["temperature_model_imported"] is False
    assert result["temperature_packets"] == 0
    assert result["activity_packets"] == 1
    assert result["temperature_samples"] > 0
    assert (tmp_path / "综合决策/temperature_samples.csv").is_file()
