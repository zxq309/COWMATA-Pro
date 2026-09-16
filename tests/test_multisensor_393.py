import json
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.signal_panel import SignalPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def series(key, times=(0, 1000, 2000), values=(1, 2, 3)):
    return PlotSeries(
        key,
        key,
        "°C" if key == "temperature" else "ADC",
        "#159c8d",
        np.array(times),
        np.array(values),
    )


def test_sheet_switch_keeps_one_annotation_track_and_view(app):
    panel = SignalPanel()
    panel.set_modalities(
        {"motion": [series("ax")], "ppg": [series("ppg_primary")], "temp": [series("temperature")]},
        2000,
    )
    labels = [{"name": "站立", "color": "#159c8d"}]
    events = [{"id": 7, "li": 0, "t0": 600, "t1": 1400, "confirmation": "confirmed"}]
    panel.set_events(labels, events)
    panel.set_selected_event(7)
    panel.set_view(500, 1500)
    panel.set_playhead(1000)
    for i, key in enumerate(("ax", "ppg_primary", "temperature")):
        panel.sheets.setCurrentIndex(i)
        assert panel.wave._series[0].key == key
        assert panel.wave._events is events
        assert panel.wave._selected_event_id == 7
        assert panel.view_range == (500, 1500)
        assert panel.wave._playhead_ms == 1000
    panel.close()


def test_missing_sheet_keeps_labels_but_does_not_show_other_signal(app):
    panel = SignalPanel()
    panel.set_data([series("ax")], 2000)
    panel.sheets.setCurrentIndex(1)
    assert panel.wave._series == []
    assert "无" in panel.sensor_status.text()
    panel.close()


def test_async_related_result_preserves_user_sheet_zoom_and_label(app):
    panel = SignalPanel()
    panel.set_data([series("ax")], 2000)
    panel.set_view(300, 1000)
    panel.set_playhead(500)
    panel.sheets.setCurrentIndex(2)
    events = [{"id": 3, "li": 0, "t0": 400, "t1": 600}]
    panel.set_events([{"name": "行为"}], events)
    panel.update_modalities(
        {"motion": [series("ax")], "temp": [series("temperature")]},
        {"temp": "温度时间为区间中点估计"},
    )
    assert panel.sheets.currentIndex() == 2
    assert panel.view_range == (300, 1000)
    assert panel.wave._events is events
    assert "估计" in panel.sensor_status.text()
    panel.close()


def test_identity_and_real_time_gate_related_temperature(tmp_path):
    from cowmata_tailring.workspace.multi_sensor import load_related
    from cowmata_tailring.workspace.resource_layout import day_at

    epoch = 1788249600000
    device = "AABBCCDDEEFF"
    primary = tmp_path / "Motion" / day_at(epoch) / (device + "-12345-A") / "record.json"
    primary.parent.mkdir(parents=True)
    primary.write_text("{}")
    motion = SimpleNamespace(
        source_path=primary,
        device=device,
        duration_ms=2000,
        epoch_at=lambda ms: epoch + ms,
        plot_series=lambda: [],
        kind="imu",
    )

    def write(owner, name, stamp, value, cow=""):
        file = tmp_path / "Temp" / day_at(epoch) / owner / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(dict(device=device, cow_id=cow, create_time=stamp, data=value)))

    write(device + "-12345-A", "ok.json", epoch + 500, 38.2)
    write(device + "-99999-A", "other.json", epoch + 500, 39)
    write(device + "-12345-B", "othermark.json", epoch + 500, 39)
    write(device + "-12345-A", "outside.json", epoch + 9000, 39)
    write(device + "-12345-A", "conflict.json", epoch + 1000, 39, "99999")
    bundle = load_related(motion, tmp_path, "12345")
    temp = bundle["modalities"]["temp"][0]
    assert temp.times_ms.tolist() == [500]
    assert temp.values.tolist() == [38.2]
    assert bundle["issues"]


def test_unknown_identity_cannot_borrow_related_data(tmp_path):
    from cowmata_tailring.workspace.multi_sensor import load_related

    motion = SimpleNamespace(
        source_path=tmp_path / "x.json",
        device="AABBCCDDEEFF",
        duration_ms=1000,
        epoch_at=lambda ms: 1788249600000 + ms,
        plot_series=lambda: [],
        kind="imu",
    )
    bundle = load_related(motion, tmp_path, "")
    assert not bundle["modalities"].get("temp")
    assert "牛号" in bundle["messages"]["temp"]


def test_shared_projection_uses_epoch_and_same_cow_device_without_copying_truth():
    from cowmata_tailring.workspace.shared_labels import project_events

    event = dict(
        id="master1",
        cow_id="12345",
        device_id="ABC",
        field_mark="A",
        code="STANDING_UP",
        start_epoch_ms=1500,
        end_epoch_ms=2500,
        confirmation="confirmed",
        source_asset_id="source",
    )
    target = dict(cow_id="12345", device_id="ABC", field_mark="A", asset_id="target")
    got = project_events([event], target, 1000, 2000)
    assert got[0]["start_ms"] == 500 and got[0]["end_ms"] == 1500
    assert got[0]["shared_event_id"] == "master1"
    assert got[0]["confirmation"] == "confirmed"
    assert not project_events([event], {**target, "cow_id": "99999"}, 1000, 2000)
    assert not project_events([event], {**target, "device_id": "OTHER"}, 1000, 2000)
    assert not project_events([event], target, 3000, 2000)
    clipped = project_events([event], target, 2000, 2000)[0]
    assert clipped["confirmation"] == "needs_review" and clipped["boundary_truncated"]
    assert event["start_epoch_ms"] == 1500


def test_point_labels_are_points_not_intervals():
    from cowmata_tailring.workspace.shared_labels import project_events

    event = dict(
        id="p",
        cow_id="12345",
        device_id="ABC",
        field_mark="",
        code="CALF_FULLY_EXPELLED",
        start_epoch_ms=2000,
        end_epoch_ms=None,
        confirmation="confirmed",
        source_asset_id="source",
    )
    target = dict(cow_id="12345", device_id="ABC", field_mark="", asset_id="target")
    assert project_events([event], target, 1000, 2000)[0]["end_ms"] is None


def test_scan_projects_motion_truth_to_ppg_and_sees_later_edits(tmp_path):
    import base64
    import hashlib
    import struct

    from cowmata_tailring.algorithms.dataset import scan_dataset

    epoch = 1788249600000
    device = "AABBCCDDEEFF"
    motion = dict(
        device=device,
        create_time=epoch,
        version=2,
        imu=base64.b64encode(
            b"".join(struct.pack("<I9h", t, 0, 0, 4096, 0, 0, 0, 1, 2, 3) for t in (0, 1000, 2000))
        ).decode(),
    )
    ppg = dict(
        device=device,
        create_time=epoch,
        sample_rate_hz=10,
        configs={"pulse_sample_time": 3},
        data=base64.b64encode(struct.pack("<30I", *range(30))).decode(),
    )

    def pair(kind, obj, events):
        folder = tmp_path / kind
        raw = folder / "Raw" / (device + "-12345-A_2026-09-01_raw.json")
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps(obj))
        asset = hashlib.sha256(raw.read_bytes()).hexdigest()
        label = folder / "Label" / raw.name.replace("_raw", "_label")
        label.parent.mkdir()
        document = dict(
            work=dict(
                asset_id=asset,
                project=dict(
                    cow_id="12345",
                    device_identity=dict(device_id=device, cow_id="12345", field_mark="A"),
                    events=events,
                ),
            )
        )
        label.write_text(json.dumps(document))
        return raw, label, document

    raw, label, doc = pair(
        "Motion",
        motion,
        [dict(id=1, label_code="STANDING_UP", t0=500, t1=1500, confirmation="confirmed")],
    )
    ppgraw, _, _ = pair("PPG", ppg, [])
    before = raw.read_bytes(), ppgraw.read_bytes()
    first = scan_dataset(tmp_path)
    rows = {r["modality"]: r for r in first["records"]}
    assert not first["issues"]
    assert rows["ppg"]["events"][0]["start_ms"] == 500
    assert rows["ppg"]["events"][0]["shared_event_id"] == first["shared_labels"][0]["id"]
    doc["work"]["project"]["events"][0]["t0"] = 700
    label.write_text(json.dumps(doc))
    second = scan_dataset(tmp_path)
    ppgrow = next(r for r in second["records"] if r["modality"] == "ppg")
    assert ppgrow["events"][0]["start_ms"] == 700
    assert ppgrow["events"][0]["shared_event_id"] == first["shared_labels"][0]["id"]
    assert second["fingerprint"] != first["fingerprint"]
    assert (raw.read_bytes(), ppgraw.read_bytes()) == before


def test_combined_cow_id_accepts_matching_mark_but_rejects_different_mark(tmp_path):
    from cowmata_tailring.workspace.multi_sensor import load_related
    from cowmata_tailring.workspace.resource_layout import day_at

    epoch = 1788249600000
    device = "AABBCCDDEEFF"
    raw = tmp_path / "Motion" / day_at(epoch) / (device + "-12345-A") / "x.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(dict(device=device, cow_id="12345A")))
    temp = tmp_path / "Temp" / day_at(epoch) / raw.parent.name / "t.json"
    temp.parent.mkdir(parents=True)
    obj = dict(device=device, cow_id="12345A", create_time=epoch + 500, data=38.5)
    temp.write_text(json.dumps(obj))
    motion = SimpleNamespace(
        source_path=raw,
        device=device,
        duration_ms=2000,
        epoch_at=lambda ms: epoch + ms,
        plot_series=lambda: [],
        kind="imu",
    )
    assert len(load_related(motion, tmp_path, "12345")["modalities"]["temp"][0].values) == 1
    obj["cow_id"] = "12345B"
    temp.write_text(json.dumps(obj))
    assert not load_related(motion, tmp_path, "12345")["modalities"]["temp"]
