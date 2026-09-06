from types import SimpleNamespace

import numpy as np
import pytest

from cowmata_tailring.workspace.rapid_backend import RapidV6Adapter, local_models


def test_v6_adapter_resets_detection_after_recognition_only():
    adapter = RapidV6Adapter.__new__(RapidV6Adapter)
    switches = []
    def engine(_image, **kwargs):
        switches.append(kwargs)
        return SimpleNamespace(txts=("2026",), scores=(.99,), boxes=np.zeros((1, 4, 2)))
    adapter.engine = engine
    assert adapter(None, use_det=False)[0] == [["2026", .99]]
    assert adapter(None)[0][0][1] == "2026"
    assert [switch["use_det"] for switch in switches] == [False, True]
    assert all(not switch["use_cls"] and switch["use_rec"] for switch in switches)


def test_v6_adapter_empty_numpy_output():
    adapter = RapidV6Adapter.__new__(RapidV6Adapter)
    adapter.engine = lambda *a, **k: SimpleNamespace(txts=(), scores=np.array([]), boxes=np.array([]))
    assert adapter(None) == ([], None)


def test_missing_models_do_not_trigger_automatic_download(tmp_path):
    (tmp_path / "models.json").write_text('{"files": {"det": {"name": "missing.onnx", "sha256": "x"}}}')
    with pytest.raises(FileNotFoundError):
        local_models(tmp_path)
