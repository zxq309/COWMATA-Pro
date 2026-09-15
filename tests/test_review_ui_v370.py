import base64
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog
from test_paired_dataset_v370 import fixture_farm

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.history_window import HistoryWindow
from cowmata_tailring.workspace.label_file import load_history
from cowmata_tailring.workspace.signal_panel import ReviewWaveform, TimePositionSpinBox


def test_review_dialog_edits_existing_event_and_keeps_raw_bytes(tmp_path):
    app = QApplication.instance() or QApplication([])
    raw, label = fixture_farm(tmp_path / "farm")
    original = raw.read_bytes()
    window = HistoryWindow(raw)
    stop = time.monotonic() + 10
    while window.future is not None and time.monotonic() < stop:
        app.processEvents()
        time.sleep(0.01)
    try:
        assert window.data is not None, window.banner.text()
        window.events.setCurrentRow(0)
        checked = []

        def edit():
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, QDialog)
            fields = dialog.findChildren(TimePositionSpinBox)
            fields[0].setValue(0.02)
            checked.append(True)
            dialog.accept()

        QTimer.singleShot(0, edit)
        window.edit_existing()
        assert checked and window.review_dirty
        assert window.save_changes()
        doc = json.loads(label.read_text(encoding="utf-8"))
        assert [e["id"] for e in doc["work"]["project"]["events"]] == [11, 27]
        assert doc["work"]["project"]["events"][0]["t0"] == 20
        assert raw.read_bytes() == original
    finally:
        window.review_dirty = False
        window.dispose()
        app.processEvents()


def test_ppg_raw_label_pair_loads_with_optical_signal(tmp_path):
    from cowmata_tailring.workspace.paired_dataset import build_dataset

    raw, label = fixture_farm(tmp_path / "farm")
    ppg = Path(str(raw).replace("Motion", "PPG"))
    ppg.parent.mkdir(parents=True)
    obj = {
        "data": base64.b64encode(np.array([50, 100, 60, 80], dtype="<u4").tobytes()).decode(),
        "sample_rate_hz": 10,
        "create_time": 1787245607000,
    }
    ppg.write_text(json.dumps(obj), encoding="utf-8")
    doc = json.loads(label.read_text(encoding="utf-8"))
    aid = hashlib.sha256(ppg.read_bytes()).hexdigest()
    doc["source"].update(
        asset_id=aid, path=doc["source"]["path"].replace("Motion", "PPG"), kind="ppg"
    )
    doc["work"]["asset_id"] = aid
    ppg_label = Path(str(label).replace("Motion", "PPG"))
    ppg_label.parent.mkdir(parents=True)
    ppg_label.write_text(json.dumps(doc), encoding="utf-8")
    result = build_dataset([ppg], tmp_path / "datasets", job=tmp_path / "job")
    assert result["counts"]["errors"] == 0
    paired = Path(result["rows"][0]["raw_target"])
    history = load_history(paired)
    assert history.motion.kind == "ppg"
    assert np.array_equal(history.motion.times_ms, [0, 100, 200, 300])
    assert history.motion.plot_series()[0]["key"] == "ppg_primary"


def test_three_gyroscope_axes_get_separate_rows():
    app = QApplication.instance() or QApplication([])
    wave = ReviewWaveform()
    series = [
        PlotSeries(
            key="g" + axis,
            name=axis,
            unit="deg/s",
            color="#888888",
            times_ms=np.arange(3.0),
            values=np.arange(3.0),
        )
        for axis in "xyz"
    ]
    wave.set_data(series, duration_ms=2)
    groups = wave.visible_groups()
    assert len(groups) == 3
    wave.close()
    app.processEvents()
