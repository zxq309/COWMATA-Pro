from pathlib import Path

from cowmata_tailring.algorithms.behavior_library import catalog, capability_matrix
from cowmata_tailring.algorithms.paths import model_home


def test_behavior_catalog_covers_external_algorithms(monkeypatch, tmp_path):
    monkeypatch.setenv("COWMATA_ALGORITHM_HOME", str(tmp_path / "models"))
    rows = capability_matrix()
    assert {row["code"] for row in rows} == {
        "STANDING_UP", "LYING_DOWN", "STRAINING_BOUT", "URINATION",
        "FETAL_PART_FIRST_VISIBLE", "CALF_FULLY_EXPELLED",
    }
    assert all(row["outputs_are_external"] for row in rows)
    assert all(str(tmp_path / "models") in row["model_home"] for row in rows)


def test_model_home_has_no_developer_drive_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("COWMATA_ALGORITHM_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert model_home() == (tmp_path / "local" / "COWMATA Pro" / "data" / "models" / "行为识别").resolve()


def test_catalog_is_static_and_complete():
    assert len(catalog()) == 6
    assert all(item.train_entry and item.predict_entry and item.candidate_entry for item in catalog())