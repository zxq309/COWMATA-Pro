import base64
import json
import struct
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.multi_sensor import load_related
from cowmata_tailring.workspace.signal_panel import SignalPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_compact_nine_axis_plot_preserves_readable_lane_height(app):
    panel = SignalPanel()
    signals = [
        PlotSeries(p + a, p + a, "g", "#159c8d", np.arange(100), np.sin(np.arange(100) / 4))
        for p in "agm"
        for a in "xyz"
    ]
    panel.set_data(signals, 100)
    panel.resize(1000, 240)
    panel.show()
    app.processEvents()
    assert panel.wave._plot_rect().height() / 9 >= 32
    assert panel.height() <= 260
    assert panel.wave_scroll.verticalScrollBar().maximum() > 0
    panel.group.setCurrentIndex(1)
    app.processEvents()
    assert len(panel.wave.visible_groups()) == 3
    panel.close()


def test_ppg_record_selects_explicit_ppg_tab_and_no_motion_curve(app):
    panel = SignalPanel()
    series = [PlotSeries("ppg_primary", "PPG 主通道", "ADC", "#159c8d", np.arange(3), np.arange(3))]
    panel.set_data(series, 3)
    assert panel.sheets.currentIndex() == 1
    assert "PPG" in panel.sensor_status.text()
    panel.sheets.setCurrentIndex(0)
    assert not panel.wave._series
    assert "selected" in panel.sheets.styleSheet()
    panel.close()


@pytest.mark.parametrize("flat", [False, True])
def test_legacy_device_scope_can_display_only_same_device_overlap(tmp_path, flat):
    epoch = 1787782800000
    device = "0C3D5EA22E37"
    primary = tmp_path / "Motion" / "2026-08-27" / device / "motion.json"
    primary.parent.mkdir(parents=True)
    primary.write_text(json.dumps(dict(device=device)))
    motion = SimpleNamespace(
        source_path=primary,
        device=device,
        duration_ms=2000,
        epoch_at=lambda ms: epoch + ms,
        plot_series=lambda: [],
        kind="imu",
    )
    folder = tmp_path / "PPG" / "2026-08-27"
    if not flat:
        folder /= device
    folder.mkdir(parents=True)
    obj = dict(
        device=device,
        create_time=epoch,
        sample_rate_hz=10,
        configs={"pulse_sample_time": 3},
        data=base64.b64encode(struct.pack("<30I", *range(30))).decode(),
    )
    (folder / "correct.json").write_text(json.dumps(obj))
    (folder / "other-device.json").write_text(json.dumps({**obj, "device": "AABBCCDDEEFF"}))
    (folder / "other-cow.json").write_text(json.dumps({**obj, "cow_id": "99999"}))
    result = load_related(motion, tmp_path, "")
    assert len(result["modalities"]["ppg"]) == 1
    assert len(result["modalities"]["ppg"][0].times_ms) == 21
    assert [s["path"] for s in result["sources"]] == [str(folder / "correct.json")]
    assert "待核对" in result["messages"]["ppg"]
    assert result["issues"]


def test_gap_message_distinguishes_present_files_from_missing_directory(tmp_path):
    epoch = 1787782800000
    device = "0C3D5EA22E37"
    owner = device + "-12345-A"
    primary = tmp_path / "Motion" / "2026-08-27" / owner / "motion.json"
    primary.parent.mkdir(parents=True)
    primary.write_text("{}")
    ppg = tmp_path / "PPG" / "2026-08-27" / owner / "early.json"
    ppg.parent.mkdir(parents=True)
    ppg.write_text(
        json.dumps(
            dict(
                device=device,
                create_time=epoch - 600000,
                sample_rate_hz=10,
                configs={"pulse_sample_time": 3},
                data=base64.b64encode(struct.pack("<30I", *range(30))).decode(),
            )
        )
    )
    motion = SimpleNamespace(
        source_path=primary,
        device=device,
        duration_ms=2000,
        epoch_at=lambda ms: epoch + ms,
        plot_series=lambda: [],
        kind="imu",
    )
    result = load_related(motion, tmp_path, "12345")
    assert not result["modalities"]["ppg"]
    assert "采样时段不重叠" in result["messages"]["ppg"]
