import re


def test_resource_name_is_only_start_seconds(tmp_path):
    from pathlib import Path

    from test_resources_v34 import record

    from cowmata_tailring.workspace.resource_import import plan_import

    raw = record(tmp_path / "incoming")
    row = plan_import(
        tmp_path / "out",
        [dict(kind="imu", path=str(raw))],
        category="calving",
        cache=tmp_path / "cache",
    )["rows"][0]
    assert Path(row["target"]).name == "2026-08-03_23-59-59.json"


def test_dataset_names_use_identity_and_only_start_time():
    from cowmata_tailring.workspace.resource_layout import dataset_filename

    raw = dataset_filename("23282", "0C3D5EA22DDE", 1787997107019, ".json")
    sample = dataset_filename("23282", "0C3D5EA22DDE", 1787997107019, ".npz", milliseconds=True)
    assert raw == "23282_0C3D5EA22DDE_2026-08-29_17-51-47.json"
    assert sample == "23282_0C3D5EA22DDE_2026-08-29_17-51-47-019.npz"
    assert not re.search(r"_[a-f0-9]{10,}\.npz$", sample)


def test_annotation_recovery_does_not_clutter_the_customer_folder(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.storage import atomic_json, read_json, recovery_path

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "customer/2026-08-29_17-51-47.标注.json"
    atomic_json(path, {"version": 1})
    atomic_json(path, {"version": 2})
    assert list(path.parent.iterdir()) == [path]
    assert recovery_path(path).is_file()
    path.write_text("interrupted", encoding="utf-8")
    assert read_json(path) == {"version": 1}
