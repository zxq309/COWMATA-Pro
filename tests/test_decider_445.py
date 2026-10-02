"""4.4.5: the downloader starts the forward decider (5_正向决策器\\产犊\\推理算法), never the trainer or the
reverse decider; sites still on the 4.4.1 layout (4_决策器\\产犊\\预测算法) keep working."""

from cowmata_tailring.edge_download import decider


def _package(root, *parts):
    folder = root.joinpath(*parts)
    (folder / "runtime").mkdir(parents=True)
    (folder / "runtime" / "python.exe").write_bytes(b"")
    (folder / "calving.py").write_text("", encoding="utf-8")
    return folder


def test_forward_decider_is_preferred(tmp_path, monkeypatch):
    monkeypatch.setattr(decider, "_drives", lambda: [tmp_path])
    _package(tmp_path, "3_训练器", "产犊", "算法", "4.4.9")
    _package(tmp_path, "4_逆向决策器", "产犊", "决策算法", "4.4.9")
    _package(tmp_path, "4_决策器", "产犊", "预测算法", "4.4.9")
    _package(tmp_path, "5_正向决策器", "产犊", "推理算法", "4.4.5")
    newest = _package(tmp_path, "5_正向决策器", "产犊", "推理算法", "4.4.10")
    (tmp_path / "5_正向决策器" / "产犊" / "推理算法" / "4.5.0").mkdir()  # no runtime: not a package

    assert decider.algorithm_home(app_root="Q:\\COWMATA") == newest


def test_legacy_decider_layout_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(decider, "_drives", lambda: [tmp_path])
    legacy = _package(tmp_path, "4_决策器", "产犊", "预测算法", "4.4.1")

    assert decider.algorithm_home(app_root="Q:\\COWMATA") == legacy


def test_trainer_or_reverse_decider_alone_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(decider, "_drives", lambda: [tmp_path])
    _package(tmp_path, "3_训练器", "产犊", "算法", "4.4.5")
    _package(tmp_path, "4_逆向决策器", "产犊", "决策算法", "4.4.5")
    messages = []

    assert decider.algorithm_home(app_root="Q:\\COWMATA") is None
    monkeypatch.setenv("COWMATA_DECIDER", "1")
    monkeypatch.setattr(decider, "_processes", {})
    assert decider.start(messages.append, app_root="Q:\\COWMATA") is False
    assert messages and "5_正向决策器" in messages[0]


def test_overview_is_the_forward_decider_risk_page(tmp_path, monkeypatch):
    monkeypatch.setattr(decider, "_drives", lambda: [tmp_path])
    old = tmp_path / "4_决策器" / "产犊" / "前端APP" / "4.4.1"
    old.mkdir(parents=True)
    (old / "COWMATA产犊预警APP.html").write_text("", encoding="utf-8")
    assert decider.decision_app(app_root="Q:\\COWMATA") == old / "COWMATA产犊预警APP.html"
    for version in ("4.4.5", "4.4.10"):
        folder = tmp_path / "5_正向决策器" / "产犊" / "前端对接" / version
        (folder / "监测报告").mkdir(parents=True)
        (folder / "监测总览.html").write_text("", encoding="utf-8")
        (folder / "监测报告" / "24214R7.html").write_text("", encoding="utf-8")

    assert decider.decision_app(app_root="Q:\\COWMATA") == folder / "监测总览.html"
