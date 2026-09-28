from pathlib import Path

from cowmata_tailring.algorithms import paths, registry, workbench_ui


def test_existing_model_library_root_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv('COWMATA_ALGORITHM_HOME', raising=False)
    monkeypatch.setenv('COWMATA_DATA_HOME', str(tmp_path / 'data'))
    library = tmp_path / '\u79d1\u7267\u7279_\u6a21\u578b'
    home = library / '4.3.9' / '\u884c\u4e3a\u8bc6\u522b'
    suite = home / 'versions' / 'current' / 'suite.json'
    suite.parent.mkdir(parents=True)
    suite.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('COWMATA_MODEL_LIBRARY', str(library))
    paths._DISCOVERY.clear()
    assert registry.default_home() == home.resolve()


def test_explicit_model_home_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv('COWMATA_ALGORITHM_HOME', str(tmp_path))
    assert registry.default_home() == tmp_path.resolve()


def test_configured_dataset_root_is_selected(monkeypatch, tmp_path):
    expected = tmp_path / '\u79d1\u7267\u7279_\u6570\u636e\u96c6' / 'COWMATA_Behavior_Dataset'
    expected.mkdir(parents=True)
    monkeypatch.setenv('COWMATA_DATASET_HOME', str(expected.parent))
    assert workbench_ui.dataset_default('COWMATA_Behavior_Dataset') == str(expected)
