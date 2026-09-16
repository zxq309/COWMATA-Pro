import base64
import hashlib
import json
import struct
import threading
from pathlib import Path

import pytest

START = 1786896000000
DEVICE = "ABCDEF123456"


def motion_record():
    frames = b"".join(
        struct.pack("<I9h", tick, 0, 0, 4096, 0, 0, 0, 1, 2, 3) for tick in (10, 120010)
    )
    return dict(
        device=DEVICE,
        cow_id="",
        uid=7,
        create_time=START,
        update_time=START + 130000,
        version=2,
        imu=base64.b64encode(frames).decode(),
        temperature=base64.b64encode(struct.pack("<2h", 3850, -200)).decode(),
    )


def scalar_record(value=38.5, stamp=START + 30010):
    return dict(
        configs=None,
        cow_id="",
        create_by=None,
        create_time=stamp,
        data=value,
        device=DEVICE,
        log=None,
        uid=100,
        update_by=None,
        update_time=stamp,
        vbat=4.1,
    )


def test_paired_export_materializes_temperature_without_changing_motion(tmp_path):
    from cowmata_tailring.workspace.paired_dataset import build_dataset

    raw = tmp_path / "farm/产犊/Motion/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-00.json"
    raw.parent.mkdir(parents=True)
    payload = json.dumps(motion_record()).encode()
    raw.write_bytes(payload)
    result = build_dataset([tmp_path / "farm"], tmp_path / "datasets", job=tmp_path / "job")
    root = Path(result["root"])
    temps = sorted((root / "Temp").rglob("*.json"))
    assert len(temps) == 2, "Each original temperature bucket must export one scalar Temp record"
    docs = [json.loads(p.read_bytes()) for p in temps]
    assert [d["data"] for d in docs] == [38.5, -2.0]
    assert [d["create_time"] for d in docs] == [1786896030010, 1786896090010]
    assert [p.name for p in temps] == ["2026-08-17_00-00-30.json", "2026-08-17_00-01-30.json"]
    assert all(d["_temperature"]["time_basis"] == "imu_bucket_midpoint_estimate" for d in docs)
    assert all(
        d["_temperature"]["source_sha256"] == hashlib.sha256(payload).hexdigest() for d in docs
    )
    assert all(d["vbat"] is None for d in docs)
    assert raw.read_bytes() == payload
    assert next(root.rglob("*_raw.json")).read_bytes() == payload
    before = {p: p.read_bytes() for p in temps}
    build_dataset([tmp_path / "farm"], tmp_path / "datasets", job=tmp_path / "job")
    assert before == {p: p.read_bytes() for p in temps}


def test_external_temperature_reads_case_insensitive_folder_and_checks_cow(tmp_path):
    from cowmata_tailring.algorithms.decision import external_temperatures

    file = tmp_path / "temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-30.json"
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps(scalar_record()))
    rows, issues = external_temperatures(tmp_path)
    assert len(rows) == 1 and not issues
    assert rows[0]["value"] == 38.5
    doc = scalar_record()
    doc["cow_id"] = "99999"
    file.write_text(json.dumps(doc))
    rows, issues = external_temperatures(tmp_path)
    assert not rows and len(issues) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, {"adc": 3850}, [38.5], "3850"])
def test_downloader_rejects_non_scalar_or_untyped_temperature(value):
    from cowmata_tailring.edge_download.core import DownloadError, validate_payload

    with pytest.raises(DownloadError):
        validate_payload(scalar_record(value), "temp")


def test_aux_temperature_model_accepts_same_scalar_record_as_downloader():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets/dataset_recipes"))
    from cowmata_temperature_aux.protocol import Context, ProtocolConfig, decode_packet

    got = decode_packet(
        scalar_record(), Context("10001", DEVICE, "binding", START), ProtocolConfig(), START + 60000
    )
    assert got["values"].tolist() == [38.5]
    assert got["raw"].tolist() == [3850]
    assert got["times"].tolist() == [1786896030010]


def test_aux_legacy_bucket_time_matches_annotation_acquisition_clock():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets/dataset_recipes"))
    from cowmata_temperature_aux.protocol import Context, ProtocolConfig, decode_packet

    got = decode_packet(
        motion_record(),
        Context("10001", DEVICE, "binding", START),
        ProtocolConfig(),
        START + 130000,
    )
    assert got["times"].tolist() == [1786896030010, 1786896090010]


def test_decision_import_rejects_temperature_unit_mismatch(tmp_path):
    from cowmata_tailring.algorithms.decision import FEATURES, SCHEMA, read_decision

    blob = b"{}"
    (tmp_path / "forest.json").write_bytes(blob)
    manifest = dict(
        schema=SCHEMA,
        complete=True,
        feature_names=list(FEATURES),
        model_file="forest.json",
        sha256=hashlib.sha256(blob).hexdigest(),
        median=[0] * len(FEATURES),
        temperature_contract={"schema": "cowmata-temperature-1", "unit": "fahrenheit"},
    )
    (tmp_path / "decision.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="温度|temperature"):
        read_decision(tmp_path)


def test_manual_downloader_never_creates_hash_suffix_for_conflicting_second(tmp_path):
    from datetime import datetime, timedelta

    from cowmata_tailring.edge_download.core import CHINA, DownloadError, Job, Target, save_record

    start = datetime.fromtimestamp(START / 1000, CHINA)
    target = Target(DEVICE, "10001", "A")
    job = Job(
        "http://localhost",
        tmp_path,
        "产犊",
        (target,),
        ("motion",),
        start,
        start + timedelta(days=1),
    )
    original = motion_record()
    first, _ = save_record(job, target, "motion", original, threading.Event())
    different = dict(original)
    different["imu"] = base64.b64encode(bytes(44)).decode()
    with pytest.raises(DownloadError, match="冲突"):
        save_record(job, target, "motion", different, threading.Event())
    assert list(first.parent.glob("*.json")) == [first]
    assert json.loads(first.read_bytes()) == original


def test_dataset_exports_native_temp_without_relabeling_it_as_motion(tmp_path):
    from cowmata_tailring.workspace.paired_dataset import build_dataset

    raw = tmp_path / "farm/产犊/Motion/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-00.json"
    native = tmp_path / "farm/产犊/Temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-45.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(motion_record()))
    native.parent.mkdir(parents=True)
    payload = json.dumps(scalar_record(39.1, START + 45000)).encode()
    native.write_bytes(payload)
    result = build_dataset([tmp_path / "farm"], tmp_path / "datasets", job=tmp_path / "job")
    destination = (
        Path(result["root"]) / "Temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-45.json"
    )
    assert destination.is_file(), "Native Temp must accompany the exported motion dataset"
    assert json.loads(destination.read_bytes()) == json.loads(payload)
    assert native.read_bytes() == payload


def test_raw_scanner_excludes_lowercase_temp_from_ppg(tmp_path):
    from cowmata_tailring.algorithms.inputs import scan_inputs

    native = tmp_path / "temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-30.json"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps(scalar_record()))
    result = scan_inputs(tmp_path)
    assert not result["records"] and not result["issues"]


def test_temperature_aux_rejects_mismatched_model_contract():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets/dataset_recipes"))
    from cowmata_temperature_aux.engine import TemperatureModule
    from cowmata_temperature_aux.protocol import Context

    model = {"temperature_contract": {"schema": "cowmata-temperature-1", "unit": "fahrenheit"}}
    with pytest.raises(ValueError, match="温度|temperature"):
        TemperatureModule(Context("10001", DEVICE, "binding", START), model=model)


def test_automatic_download_keeps_only_timestamp_name_on_conflict(tmp_path):
    from datetime import datetime, timedelta

    from cowmata_tailring.edge_download.automatic import _save_record
    from cowmata_tailring.edge_download.core import CHINA, DownloadError, Job, Target

    start = datetime.fromtimestamp(START / 1000, CHINA)
    job = Job(
        "http://localhost",
        tmp_path,
        "产犊",
        (Target(DEVICE, "10001", "A"),),
        ("motion",),
        start,
        start + timedelta(days=1),
    )
    original = motion_record()
    original["cow_id"] = "10001A"
    first, _, _ = _save_record(job, original, threading.Event())
    different = dict(original)
    different["imu"] = base64.b64encode(bytes(44)).decode()
    with pytest.raises(DownloadError, match="冲突"):
        _save_record(job, different, threading.Event())
    assert list(first.parent.glob("*.json")) == [first]


def test_motion_dedup_fingerprint_includes_embedded_temperature():
    from cowmata_tailring.edge_download.core import fingerprint

    a = motion_record()
    b = dict(a)
    b["temperature"] = base64.b64encode(struct.pack("<2h", 3900, -200)).decode()
    assert fingerprint(a, "motion") != fingerprint(b, "motion"), (
        "Equal IMU cannot hide changed temperature samples"
    )


def test_training_rejects_temperature_contract_before_using_rows(tmp_path):
    from cowmata_tailring.algorithms.decision import train_decision

    evidence = {
        "rows": [],
        "temperature_contract": {"schema": "cowmata-temperature-1", "unit": "fahrenheit"},
    }
    with pytest.raises(ValueError, match="温度|temperature"):
        train_decision(evidence, tmp_path / "not-used.csv", tmp_path / "model")


def test_fusion_never_uses_a_temperature_received_after_prediction_cutoff(tmp_path):
    from cowmata_tailring.algorithms.decision import build_fusion

    root = tmp_path / "farm"
    raw = root / "Motion/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-00.json"
    raw.parent.mkdir(parents=True)
    doc = motion_record()
    doc["imu"] = base64.b64encode(
        b"".join(
            struct.pack("<I9h", 10 + i * 20, 0, 0, 4096, 0, 0, 0, 1, 2, 3) for i in range(6001)
        )
    ).decode()
    raw.write_text(json.dumps(doc))
    temp = root / "Temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-45.json"
    temp.parent.mkdir(parents=True)
    obj = scalar_record(40.0, START + 45000)
    obj["update_time"] = START + 600000
    temp.write_text(json.dumps(obj))
    result = build_fusion(root, [], tmp_path / "out", tmp_path / "cache")
    assert result["rows"]
    assert result["rows"][0]["temperature_c"] != 40.0, (
        "Future received measurement leaked into prediction"
    )


def test_fusion_marks_embedded_temperature_unavailable_before_packet_receipt(tmp_path):
    from cowmata_tailring.algorithms.decision import build_fusion

    root = tmp_path / "farm"
    raw = root / "Motion/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-00.json"
    raw.parent.mkdir(parents=True)
    doc = motion_record()
    doc["update_time"] = START + 600000
    doc["imu"] = base64.b64encode(
        b"".join(
            struct.pack("<I9h", 10 + i * 20, 0, 0, 4096, 0, 0, 0, 1, 2, 3) for i in range(6001)
        )
    ).decode()
    raw.write_text(json.dumps(doc))
    result = build_fusion(root, [], tmp_path / "out", tmp_path / "cache")
    assert result["rows"] and result["rows"][0]["temperature_c"] is None
    assert result["rows"][0]["temperature_samples"] == 0


def test_combined_cow_code_matches_same_ear_tag_and_field_mark(tmp_path):
    from cowmata_tailring.algorithms.decision import external_temperatures

    file = tmp_path / "Temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-30.json"
    file.parent.mkdir(parents=True)
    doc = scalar_record()
    doc["cow_id"] = "10001A"
    file.write_text(json.dumps(doc))
    rows, issues = external_temperatures(tmp_path)
    assert len(rows) == 1 and not issues
    doc["cow_id"] = "10001B"
    file.write_text(json.dumps(doc))
    rows, issues = external_temperatures(tmp_path)
    assert not rows and len(issues) == 1


def test_temperature_discovery_ignores_unrelated_temp_ancestor(tmp_path):
    from cowmata_tailring.temperature import find_temperature_sources

    root = tmp_path / "Temp/workspace/farm"
    raw = root / "Motion/raw.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}")
    temp = root / "temp/2026-08-17/ABCDEF123456-10001-A/2026-08-17_00-00-30.json"
    temp.parent.mkdir(parents=True)
    temp.write_text(json.dumps(scalar_record()))
    assert find_temperature_sources([root]) == [temp]


def test_mother_dataset_exports_scalar_temperature_and_declares_contract(tmp_path, monkeypatch):
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    raw, label = sources(tmp_path)
    obj = json.loads(raw.read_text(encoding="utf-8-sig"))
    obj["temperature"] = base64.b64encode(struct.pack("<h", 3850)).decode()
    raw.write_text(json.dumps(obj))
    execute_migration(
        plan_migration([label], [raw], tmp_path / "project", category="pregnancy_late")
    )
    export_dataset(tmp_path / "project", tmp_path / "dataset")
    got = read_dataset(tmp_path / "dataset")
    temps = list((tmp_path / "dataset/Temp").rglob("*.json"))
    assert len(temps) == 1
    assert json.loads(temps[0].read_bytes())["data"] == 38.5
    assert got["manifest"]["temperature"]["contract"]["unit"] == "celsius"


@pytest.mark.parametrize("mode", ["manual", "csv", "automatic"])
def test_download_motion_name_uses_first_frame_acquisition_second(tmp_path, mode):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from cowmata_tailring.edge_download.automatic import _save_record
    from cowmata_tailring.edge_download.core import CHINA, Job, Target, save_record
    from cowmata_tailring.edge_download.csv_download import save_record as csv_save

    obj = motion_record()
    obj["create_time"] = START + 995
    obj["cow_id"] = "10001A"
    start = datetime.fromtimestamp(START / 1000, CHINA)
    target = Target(DEVICE, "10001", "A")
    job = Job(
        "http://localhost",
        tmp_path,
        "产犊",
        (target,),
        ("motion",),
        start,
        start + timedelta(days=1),
    )
    if mode == "manual":
        file = save_record(job, target, "motion", obj, threading.Event())[0]
    elif mode == "automatic":
        file = _save_record(job, obj, threading.Event())[0]
    else:
        wear = SimpleNamespace(
            category="产犊", identity=SimpleNamespace(folder_name=DEVICE + "-10001-A")
        )
        plan = SimpleNamespace(resolve=lambda *args: (wear, ""))
        file = csv_save(job, plan, "motion", obj)[0]
    assert file.name == "2026-08-17_00-00-01.json"
    assert file.parent.name == "ABCDEF123456-10001-A"
    assert json.loads(file.read_bytes())["create_time"] == START + 995


@pytest.mark.parametrize("mode", ["manual", "automatic"])
def test_download_repairs_corrupt_file_with_exact_backup_and_no_suffix(tmp_path, mode):
    from datetime import datetime, timedelta

    from cowmata_tailring.edge_download.automatic import _save_record
    from cowmata_tailring.edge_download.core import CHINA, Job, Target, save_record

    obj = motion_record()
    obj["cow_id"] = "10001A"
    start = datetime.fromtimestamp(START / 1000, CHINA)
    target = Target(DEVICE, "10001", "A")
    job = Job(
        "http://localhost",
        tmp_path,
        "产犊",
        (target,),
        ("motion",),
        start,
        start + timedelta(days=1),
    )

    def save():
        return (
            save_record(job, target, "motion", obj, threading.Event())
            if mode == "manual"
            else _save_record(job, obj, threading.Event())
        )

    original = save()[0]
    original.write_bytes(b"corrupt original bytes")
    repaired = save()
    assert repaired[0] == original and repaired[1]
    assert json.loads(original.read_bytes())["imu"] == obj["imu"]
    assert list(original.parent.glob("*.json")) == [original]
    assert any(
        p.read_bytes() == b"corrupt original bytes"
        for p in (tmp_path / ".edge-download/recovery").iterdir()
    )


def test_legacy_decision_export_includes_native_temp_without_duplicate_buckets(tmp_path):
    from cowmata_tailring.temperature import motion_temperature_records, save_temperature_record
    from cowmata_tailring.workspace.decision_dataset import export_decision

    doc = motion_record()
    payload = json.dumps(doc).encode()
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "raw.json").write_bytes(payload)
    source = dict(
        asset_id=digest,
        path="raw.json",
        cow_id="10001",
        device_id=DEVICE,
        field_mark="A",
        identity_eligible=True,
        record_start_ms=START,
    )
    (tmp_path / "sources.jsonl").write_text(json.dumps(source) + "\n")
    (tmp_path / "events.jsonl").write_text("")
    for row in motion_temperature_records(doc, source_sha256=digest):
        save_temperature_record(tmp_path, DEVICE + "-10001-A", row)
    save_temperature_record(tmp_path, DEVICE + "-10001-A", scalar_record(39.1, START + 45000))
    result = export_decision(tmp_path)
    assert result["temperature_samples"] == 3
    assert result["temperature_contract"]["unit"] == "celsius"


def test_legacy_temperature_binding_end_includes_first_frame_delay():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets/dataset_recipes"))
    from cowmata_temperature_aux.protocol import Context, PacketError, ProtocolConfig, decode_packet

    with pytest.raises(PacketError, match="CROSSES_BINDING_END"):
        decode_packet(
            motion_record(),
            Context("10001", DEVICE, "binding", START, START + 120005),
            ProtocolConfig(),
            START + 130000,
        )


def test_moved_project_mother_export_resolves_local_raw_before_stale_hint(tmp_path, monkeypatch):
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    raw, label = sources(tmp_path)
    project = tmp_path / "old-project"
    execute_migration(plan_migration([label], [raw], project, category="pregnancy_late"))
    from cowmata_tailring.workspace.mother_dataset import _documents

    _, doc = next(iter(_documents(project)))
    original = Path(doc["source"]["project_root_hint"]) / doc["source"]["path"]
    moved = tmp_path / "moved-project"
    moved.mkdir()
    destination = moved / doc["source"]["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(original.read_bytes())
    doc["embedded_imu"] = None
    doc["source"]["project_root_hint"] = str(tmp_path / "missing-old-drive")
    (moved / "label.json").write_text(json.dumps(doc), encoding="utf-8")
    result = export_dataset(moved, tmp_path / "exported")
    assert result["positive_events"] == 1
    assert result["raw_sources"] == 1
    assert read_dataset(tmp_path / "exported")["events"][0]["training_eligible"]
