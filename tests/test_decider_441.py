"""4.4.1: the downloader starts the decider package in 4_决策器\\产犊\\预测算法, never the trainer."""

from cowmata_tailring.edge_download import decider


def _package(root, *parts):
    folder = root.joinpath(*parts)
    (folder / "runtime").mkdir(parents=True)
    (folder / "runtime" / "python.exe").write_bytes(b"")
    (folder / "calving.py").write_text("", encoding="utf-8")
    return folder


def test_decider_package_is_found_in_decider_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(decider, "_drives", lambda: [tmp_path])
    _package(tmp_path, "3_训练器", "产犊", "算法", "4.4.9")
    _package(tmp_path, "4_决策器", "产犊", "预测算法", "4.4.2")
    newest = _package(tmp_path, "4_决策器", "产犊", "预测算法", "4.4.10")
    (tmp_path / "4_决策器" / "产犊" / "预测算法" / "4.5.0").mkdir()  # no runtime: not a package

    assert decider.algorithm_home(app_root="Q:\\COWMATA") == newest


def test_trainer_package_alone_does_not_start_a_decider(tmp_path, monkeypatch):
    monkeypatch.setattr(decider, "_drives", lambda: [tmp_path])
    _package(tmp_path, "3_训练器", "产犊", "算法", "4.4.1")
    messages = []

    assert decider.algorithm_home(app_root="Q:\\COWMATA") is None
    monkeypatch.setenv("COWMATA_DECIDER", "1")
    monkeypatch.setattr(decider, "_process", None)
    assert decider.start(messages.append, app_root="Q:\\COWMATA") is False
    assert messages and "4_决策器" in messages[0]
