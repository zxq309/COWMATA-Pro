import pytest
from PySide6.QtWidgets import QApplication


@pytest.mark.parametrize('value',['','missing-dataset'])
def test_invalid_dataset_does_not_create_model_run(tmp_path,monkeypatch,value):
    from cowmata_tailring.algorithms.behavior_ui import BehaviorWindow
    app=QApplication.instance() or QApplication([])
    monkeypatch.setenv('COWMATA_ALGORITHM_HOME',str(tmp_path/'models'/'行为识别'))
    w=BehaviorWindow()
    monkeypatch.setattr(w,'launch',lambda _:None)
    try:
        w.dataset.setText(value)
        w.train()
        assert not (tmp_path/'models').exists()
        assert '数据集' in w.status.text()
    finally:
        w.close()
        app.processEvents()
