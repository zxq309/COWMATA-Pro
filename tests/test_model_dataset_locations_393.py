from pathlib import Path

from cowmata_tailring.algorithms import registry, workbench_ui


def test_existing_farm_model_root_is_used(monkeypatch):
    monkeypatch.delenv('COWMATA_ALGORITHM_HOME', raising=False)
    monkeypatch.setattr(Path, 'is_dir', lambda p: str(p) == r'F:\科牧特_模型')
    assert registry.default_home() == (Path(r'F:\科牧特_模型') / '行为识别').resolve()


def test_explicit_model_home_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv('COWMATA_ALGORITHM_HOME', str(tmp_path))
    assert registry.default_home() == tmp_path.resolve()


def test_existing_dataset_root_is_selected(monkeypatch):
    expected = Path(r'F:\科牧特_数据集') / 'COWMATA_Behavior_Dataset'
    monkeypatch.setattr(Path, 'is_dir', lambda p: p == expected)
    assert workbench_ui.dataset_default('COWMATA_Behavior_Dataset') == str(expected)
