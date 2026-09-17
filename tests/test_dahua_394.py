from __future__ import annotations

import hashlib

import pytest

from cowmata_tailring.workspace import dahua_tasks as tasks


@pytest.fixture
def panel():
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.dahua_ui import DahuaPanel

    _app = QApplication.instance() or QApplication([])
    widget = DahuaPanel()
    yield widget
    widget.close()


def test_shuffled_channels_fill_same_numbered_views_without_manual_selection(panel):
    panel.apply_index(
        dict(
            adapter=tasks.ADAPTER,
            mode="disk",
            groups={"channel:20": 2, "channel:3": 7, "channel:01": 4, "channel:16": 9},
            total=22,
            invalid=0,
        )
    )
    assert panel.selected_mapping() == {
        "channel:01": "视角01",
        "channel:3": "视角03",
        "channel:16": "视角16",
        "channel:20": "视角20",
    }
    assert panel.mapping[1].currentData() == ""
    assert "未选择" not in panel.table.item(0, 3).text()
    assert "无" in panel.table.item(1, 3).text()


def test_rescan_does_not_keep_channels_from_previous_disk(panel):
    panel.apply_index(
        dict(adapter=tasks.ADAPTER, mode="disk", groups={"channel:1": 4}, total=4, invalid=0)
    )
    panel.apply_index(
        dict(adapter=tasks.ADAPTER, mode="disk", groups={"channel:20": 2}, total=2, invalid=0)
    )
    assert panel.selected_mapping() == {"channel:20": "视角20"}
    assert panel.mapping[0].currentData() == ""


def test_unknown_or_ambiguous_number_is_not_guessed(panel):
    panel.apply_index(
        dict(
            adapter=tasks.ADAPTER,
            mode="disk",
            groups={
                "channel:0": 1,
                "channel:21": 1,
                "channel:1": 1,
                "channel:01": 1,
                "channel:2": 1,
            },
            total=5,
            invalid=0,
        )
    )
    assert panel.selected_mapping() == {"channel:2": "视角02"}


def test_explicit_channel_names_in_exported_file_folders_fill_views(panel):
    panel.apply_index(
        dict(
            adapter=tasks.ADAPTER,
            mode="file",
            groups={
                "folder:C:/incoming/Channel 02": 2,
                "folder:C:/incoming/视角20": 1,
                "file:C:/incoming/channel-01.dav": 1,
                "folder:C:/incoming/2026-09-17": 3,
            },
            total=7,
            invalid=0,
        )
    )
    assert panel.selected_mapping() == {
        "file:C:/incoming/channel-01.dav": "视角01",
        "folder:C:/incoming/Channel 02": "视角02",
        "folder:C:/incoming/视角20": "视角20",
    }


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "locks"))
    farm = tmp_path / "farm"
    job = tmp_path / "job"
    source = tmp_path / "raw/channel-01.dav"
    source.parent.mkdir()
    source.write_bytes(b"original raw stream, not modified by the archive stage")
    index = tasks.scan(dict(mode="files", files=[str(source)]), job)
    prepared = job / "records/one/prepared.mp4"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"verified prepared media fixture")
    value = dict(
        path=str(prepared),
        sha256=hashlib.sha256(prepared.read_bytes()).hexdigest(),
        start_ms=1789272000000,
        duration_ms=1000,
        metadata={"dahua": {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}},
    )
    # Media decoding is validated separately with a real DAV. Keep the entire
    # source validation, optional JSON import and archive commit real here.
    monkeypatch.setattr(tasks, "prepare_record", lambda *a: [value])
    request = dict(
        target=str(farm),
        category="calving",
        scenario="mixed",
        mapping={index["rows"][0]["group"]: "视角01"},
        json_sources=[],
        start="",
        end="",
        split_midnight=True,
    )
    return farm, job, source, request


@pytest.mark.parametrize("scenario", ["mixed", "attach_video"])
@pytest.mark.parametrize("attached", ["", "产犊", "产犊/Motion"])
def test_existing_farm_json_does_not_block_video_or_get_reimported(preparation, scenario, attached):
    from test_resources_v34 import record

    farm, job, source, request = preparation
    raw = record(farm / "产犊/Motion")
    original = raw.read_bytes()
    request.update(scenario=scenario, json_sources=[str(farm / attached)])
    first = tasks.organize(request, job)
    assert first["status"] == "completed"
    videos = list((farm / "产犊/Video").rglob("*.mp4"))
    assert [p.relative_to(farm).as_posix() for p in videos] == [
        "产犊/Video/2026-09-13/视角01/2026-09-13_12-00-00.mp4"
    ]
    assert len(list((farm / "产犊/Video/2026-09-13").iterdir())) == 20
    assert raw.read_bytes() == original
    assert list((farm / "产犊/Motion").rglob("*.json")) == [raw]
    assert source.exists()
    tasks.organize(request, job)
    assert list((farm / "产犊/Video").rglob("*.mp4")) == videos


def test_optional_sensor_import_ignores_neighboring_videos(preparation, tmp_path, monkeypatch):
    from test_resources_v34 import record

    from cowmata_tailring.workspace import video_intake

    farm, job, source, request = preparation
    raw = record(tmp_path / "incoming")
    (raw.parent / "unrelated.mp4").write_bytes(b"not part of sensor import")
    monkeypatch.setattr(
        video_intake, "inspect", lambda *a: pytest.fail("Optional JSON import inspected a video")
    )
    request["json_sources"] = [str(raw.parent)]
    result = tasks.organize(request, job)
    assert result["status"] == "completed"
    saved = list((farm / "产犊/Motion").rglob("*.json"))
    assert len(saved) == 1 and saved[0].read_bytes() == raw.read_bytes()
    assert len(list((farm / "产犊/Video").rglob("*.mp4"))) == 1


def test_raw_source_inside_output_is_still_rejected(preparation):
    farm, job, source, request = preparation
    inside = farm / "产犊/Video/original.dav"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(source.read_bytes())
    index = tasks.scan(dict(mode="files", files=[str(inside)]), job)
    request["mapping"] = {index["rows"][0]["group"]: "视角01"}
    with pytest.raises(ValueError, match="独立"):
        tasks.organize(request, job)
    assert inside.exists()


def test_restoring_explicit_mapping_preserves_excluded_channels(panel):
    panel.apply_index(
        dict(
            adapter=tasks.ADAPTER,
            mode="disk",
            groups={"channel:1": 1, "channel:2": 1},
            total=2,
            invalid=0,
        )
    )
    panel.apply_restored_options(
        dict(
            target="C:/qa/farm",
            category="calving",
            mapping={"channel:2": "视角02"},
            json_sources=["C:/qa/farm"],
        )
    )
    assert panel.selected_mapping() == {"channel:2": "视角02"}
    assert not panel.import_json.isChecked() and panel.json_sources == []


def test_disabling_optional_import_keeps_video_request_sensor_free(panel, monkeypatch, tmp_path):
    panel.apply_index(
        dict(adapter=tasks.ADAPTER, mode="disk", groups={"channel:1": 1}, total=1, invalid=0)
    )
    panel.target.setText(str(tmp_path / "farm"))
    panel.import_json.setChecked(True)
    panel.json_sources = [str(tmp_path / "incoming")]
    panel.import_json.setChecked(False)
    requests = []
    monkeypatch.setattr(panel, "start", lambda action, request, job: requests.append(request))
    panel.organize()
    assert requests[0]["options"]["json_sources"] == []
    assert requests[0]["options"]["mapping"] == {"channel:1": "视角01"}


@pytest.mark.parametrize("modality", ["imu", "ppg"])
@pytest.mark.parametrize("folder", ["incoming", "Motion", "PPG"])
def test_system_temp_ancestor_does_not_turn_waveforms_into_temperature(tmp_path, modality, folder):
    import json

    from cowmata_tailring.workspace.ppg_intake import plan_temperature

    path = tmp_path / "Temp" / folder / "one.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({modality: "MTIzNA==", "create_time": 1786896000000}))
    assert plan_temperature(
        path, tmp_path / "farm", tmp_path / "cache", "copy", lambda: False,
        digest=lambda *args: pytest.fail("Waveform was handled as temperature"),
    ) is None


def test_ppg_data_field_under_system_temp_uses_nearest_sensor_folder(tmp_path):
    import json

    from cowmata_tailring.workspace.ppg_intake import plan_temperature

    path = tmp_path / "Temp" / "PPG" / "one.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"data": "MTIzNA==", "create_time": 1786896000000}))
    assert plan_temperature(
        path, tmp_path / "farm", tmp_path / "cache", "copy", lambda: False,
        digest=lambda *args: pytest.fail("PPG data field was handled as temperature"),
    ) is None
