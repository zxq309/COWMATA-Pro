import base64
import hashlib
import json
from pathlib import Path

import pytest

from cowmata_tailring.workspace import ppg_intake


def digest(path, *_):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source(tmp_path, modality, data):
    p = tmp_path / "incoming" / modality / "ABCDEF000001-90001-A" / "old-name.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            dict(
                device="ABCDEF000001",
                cow_id="90001A",
                create_time=1788228000123,
                update_time=1788228001123,
                data=data,
                configs=None,
            )
        ),
        encoding="utf-8",
    )
    return p


def test_ppg_intake_uses_seconds_timestamp_and_blocks_conflicting_same_second(tmp_path):
    p = source(tmp_path, "PPG", base64.b64encode(bytes(40)).decode())
    root = tmp_path / "target"
    row = ppg_intake.plan_ppg(p, root, tmp_path / "cache", "copy", lambda: False, digest=digest)
    target = Path(row["target"])
    assert target.name == "2026-09-01_10-00-00.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"different":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="同名"):
        ppg_intake.plan_ppg(p, root, tmp_path / "cache", "copy", lambda: False, digest=digest)
    assert list(target.parent.iterdir()) == [target]


def test_temperature_intake_preserves_scalar_bytes_and_owner(tmp_path):
    p = source(tmp_path, "Temp", 38.52)
    before = p.read_bytes()
    row = ppg_intake.plan_temperature(
        p, tmp_path / "target", tmp_path / "cache", "copy", lambda: False, digest=digest
    )
    assert (
        Path(row["target"])
        .as_posix()
        .endswith("/Temp/2026-09-01/ABCDEF000001-90001-A/2026-09-01_10-00-00.json")
    )
    assert row["kind"] == "temp" and row["metadata"]["temperature_contract"]["unit"] == "celsius"
    assert row["sha256"] == hashlib.sha256(before).hexdigest() and p.read_bytes() == before
    doc = json.loads(before)
    doc["cow_id"] = "90002A"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        ppg_intake.plan_temperature(
            p, tmp_path / "target", tmp_path / "cache", "copy", lambda: False, digest=digest
        )


def test_temperature_organization_executes_and_repeats_without_changing_source(
    tmp_path, monkeypatch
):
    from cowmata_tailring.workspace.video_intake import organize

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    p = source(tmp_path, "Temp", 38.52)
    before = p.read_bytes()
    result = organize(
        tmp_path / "farm",
        [dict(path=str(p), kind="imu")],
        category="calving",
        farm=str(tmp_path / "farm"),
        cache=tmp_path / "cache",
        job=tmp_path / "job",
    )
    assert result["completed"] and result["unresolved"] == 0
    outputs = list((tmp_path / "farm").rglob("*.json"))
    matches = [f for f in outputs if f.name == "2026-09-01_10-00-00.json"]
    assert len(matches) == 1 and matches[0].read_bytes() == before
    assert "/Temp/" in matches[0].as_posix()
    assert p.read_bytes() == before
    again = organize(
        tmp_path / "farm",
        [dict(path=str(p), kind="imu")],
        category="calving",
        farm=str(tmp_path / "farm"),
        cache=tmp_path / "cache",
        job=tmp_path / "job2",
    )
    assert again["completed"] and again["unresolved"] == 0
    assert matches[0].read_bytes() == before
