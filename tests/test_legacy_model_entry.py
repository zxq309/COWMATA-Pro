"""Compatibility windows must keep manual work usable without optional models."""
import builtins
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from test_label_file import source as source

from cowmata_tailring.app import model_assist_window
from cowmata_tailring.model_assist import assist


@pytest.fixture
def legacy_window(monkeypatch, tmp_path):
    # These are real native-player UI integration tests, not media stubs.
    # The Windows distribution supplies VLC; source-only CI does not.
    package = Path(__file__).resolve().parents[2] / "COWMATA Annotator"
    if sys.platform != "win32":
        pytest.skip("Legacy native player UI requires Windows")
    if not (package / "vendor/vlc/libvlc.dll").is_file():
        pytest.skip("Legacy native player UI requires the adjacent offline distribution")
    from cowmata_tailring.app.mixins import production_safety
    from cowmata_tailring.ui import main_window
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "legacy.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_window, "QSettings", lambda *_: settings)
    monkeypatch.setattr(production_safety.QStandardPaths, "writableLocation", lambda *_: str(tmp_path / "user-data"))
    monkeypatch.setenv("VLC_HOME", str(package / "vendor/vlc"))
    window = model_assist_window.MainWindow()
    window.autosave_timer.stop()
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    window.show()
    app.processEvents()
    yield window, errors
    window.close()
    app.processEvents()


def test_legacy_manual_window_can_load_mark_save_reload_without_torch(legacy_window, source, tmp_path):
    window, errors = legacy_window
    window.open_json(str(source[1].source_path))
    assert window.data is not None and window.isVisible()
    assert window.data.sample_count == 11
    window._range_selected(20, 80)
    assert len(window.events) == 1
    path = tmp_path / "manual-project.json"
    window.save_project_dialog(str(path))
    window.events.clear()
    window.load_project_dialog(str(path))
    assert len(window.events) == 1 and window.events[0]["t0"] == 20
    assert window.events[0]["t1"] == 80
    assert not errors


@pytest.mark.parametrize("action", ["run_model_prediction", "choose_prediction_model"])
def test_missing_optional_runtime_explains_limitation_before_model_picker(legacy_window, source, monkeypatch, action):
    window, errors = legacy_window
    window.open_json(str(source[1].source_path))
    real_import = builtins.__import__
    def without_torch(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise ModuleNotFoundError("No module named 'torch'", name="torch")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_torch)
    monkeypatch.setattr(assist.QFileDialog, "getExistingDirectory",
                        lambda *_: pytest.fail("Missing optional dependencies must be explained before choosing a model"))
    getattr(window, action)()
    assert errors and "torch" in errors[-1]
    assert "人工标注" in errors[-1]
    assert window.isVisible() and window._prediction_worker is None
