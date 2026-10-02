"""4.4.6: one site tree (目录树) holds every business file; algorithms live in routes (常规经典算法 / AI大模型算法)."""
import json
from pathlib import Path

from cowmata_tailring.edge_download import decider, paths


def _tree(base):
    farm = base / "1_下载器" / "扬大_高邮牧场"
    (farm / "台账").mkdir(parents=True)
    (farm / ".cowmata-farm.json").write_text(json.dumps(
        {"schema": "cowmata-farm-v1", "farm_id": "e8d8aa8e-9644-46a0-babf-658a8b0d97f5", "recordings": "录像"}),
        encoding="utf-8")
    (base / "2_标注器").mkdir()
    return farm


def _package(root, *parts):
    folder = root.joinpath(*parts)
    (folder / "runtime").mkdir(parents=True)
    (folder / "runtime" / "python.exe").write_bytes(b"")
    (folder / "calving.py").write_text("", encoding="utf-8")
    return folder


def test_tree_is_found_from_the_app_inside_it_or_on_a_drive(tmp_path, monkeypatch):
    _tree(tmp_path / "E")
    app = tmp_path / "E" / "2_标注器" / "4.4.6" / "COWMATA-Pro-4.4.6-Portable"
    app.mkdir(parents=True)
    assert paths.tree_root(app) == tmp_path / "E"
    outside = tmp_path / "D" / "COWMATA Annotator"
    outside.mkdir(parents=True)
    assert paths.tree_root(outside) is None  # test mode: no drive scan
    monkeypatch.setenv("COWMATA_SITE_TREE", "")
    monkeypatch.setattr(paths, "_drives", lambda kinds=(2, 3): [tmp_path / "D", tmp_path / "E"])
    assert paths.tree_root(outside) == tmp_path / "E"
    assert paths.site_ledger(outside) == tmp_path / "E" / "1_下载器" / "扬大_高邮牧场" / "台账"


def test_forward_decider_of_each_route_and_version_first_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path))
    _tree(tmp_path)
    monkeypatch.setattr(decider, "_drives", lambda: [])
    old = _package(tmp_path, "5_正向决策器", "产犊", "4.4.5", "推理算法")
    assert decider.algorithm_home(app_root=tmp_path / "x", track="常规经典算法") == old
    classic = _package(tmp_path, "常规经典算法", "5_正向决策器", "产犊", "4.4.6", "推理算法")
    llm = _package(tmp_path, "AI大模型算法", "5_正向决策器", "产犊", "4.4.6", "推理算法")
    _package(tmp_path, "常规经典算法", "4_逆向决策器", "产犊", "4.4.7", "决策算法")  # never the reverse decider
    assert decider.algorithm_home(app_root=tmp_path / "x", track="常规经典算法") == classic
    assert decider.algorithm_home(app_root=tmp_path / "x", track="AI大模型算法") == llm
    page = tmp_path / "AI大模型算法" / "5_正向决策器" / "产犊" / "4.4.6" / "前端对接" / "监测总览.html"
    page.parent.mkdir(parents=True)
    page.write_text("", encoding="utf-8")
    assert decider.decision_app(app_root=tmp_path / "x", tracks=["AI大模型算法"]) == page
    assert decider.decision_app(app_root=tmp_path / "x", tracks=["常规经典算法"]) == page  # only route with a page
    monkeypatch.setenv("COWMATA_DECIDER", "1")
    messages = []
    assert decider.start(messages.append, app_root=tmp_path / "x", tracks=[]) is False
    assert "没有选择" in messages[0]


def test_uploader_working_copy_moves_into_the_tree(tmp_path, monkeypatch):
    from cowmata_tailring.ledger import host
    monkeypatch.delenv("COWMATA_LEDGER_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    legacy = tmp_path / "local" / "COWMATA Annotator" / "ledger" / "admin"
    (legacy / "calving").mkdir(parents=True)
    (legacy / "样本试验台账.csv").write_bytes(b"pending edits")
    (legacy / "calving" / "台账缓存.sqlite").write_bytes(b"cache")
    assert host.data_root("admin") == legacy  # no tree yet: unchanged
    farm = _tree(tmp_path / "E")
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path / "E"))
    moved = host.data_root("admin")
    assert moved == farm / "台账" / "上传" / "admin"
    assert (moved / "样本试验台账.csv").read_bytes() == b"pending edits"
    assert (moved / "calving" / "台账缓存.sqlite").read_bytes() == b"cache" and not legacy.exists()
    assert host.data_root("admin") == moved


def test_csv_sync_state_of_a_farm_ledger_stays_in_the_farm(tmp_path, monkeypatch):
    import hashlib
    import os

    from cowmata_tailring.edge_download.site_records import records_state_directory
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    farm = _tree(tmp_path / "E")
    ledger = farm / "台账"
    key = hashlib.sha256(os.path.normcase(str(ledger.resolve())).encode("utf-8")).hexdigest()[:24]
    legacy = tmp_path / "local" / "COWMATA-Pro" / "site-records" / key
    (legacy / "csv-backups").mkdir(parents=True)
    (legacy / "last-csv-sync.json").write_text("{}", encoding="utf-8")
    state = records_state_directory(ledger)
    assert state == farm / ".edge-download" / "ledger-sync"
    assert (state / "last-csv-sync.json").is_file() and not legacy.exists()
    other = tmp_path / "loose-folder"
    assert records_state_directory(other).is_relative_to(tmp_path / "local")


def test_collaboration_home_is_inside_the_farm(tmp_path):
    from cowmata_tailring.workspace.farm_layout import collaboration_home
    farm = _tree(tmp_path)
    assert collaboration_home(farm) == farm / "科牧特_协作标注"
    legacy = tmp_path / "2_标注器" / "科牧特_协作标注"
    legacy.mkdir()
    assert collaboration_home(farm) == legacy  # a site still using the 4.4.5 place keeps it
    (farm / "科牧特_协作标注").mkdir()
    assert collaboration_home(farm) == farm / "科牧特_协作标注"


def test_behaviour_models_are_found_in_every_trainer_layout(tmp_path, monkeypatch):
    from cowmata_tailring.algorithms import paths as algorithm_paths

    def suite(home):
        (home / "versions" / "v1").mkdir(parents=True)
        (home / "versions" / "v1" / "suite.json").write_text("{}", encoding="utf-8")
        return home

    monkeypatch.delenv("COWMATA_MODEL_LIBRARY", raising=False)
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path))
    _tree(tmp_path)
    monkeypatch.setattr("cowmata_tailring.edge_download.paths._drives", lambda kinds=(2, 3): [])
    algorithm_paths._DISCOVERY.clear()
    old = suite(tmp_path / "3_训练器" / "产犊" / "模型" / "4.4.1" / "行为识别")
    version_first = suite(tmp_path / "3_训练器" / "产犊" / "4.4.5" / "模型" / "行为识别")
    route = suite(tmp_path / "常规经典算法" / "3_训练器" / "产犊" / "4.4.6" / "模型" / "行为识别")
    found = algorithm_paths.discover_model_homes()
    assert found[:3] == [route, version_first, old]
    algorithm_paths._DISCOVERY.clear()


def test_tools_never_assume_a_drive_letter(monkeypatch, tmp_path):
    from cowmata_tailring.media import ffmpeg_tools
    tools = tmp_path / "X" / "Applications" / "ffmpeg-9"
    tools.mkdir(parents=True)
    monkeypatch.setattr("cowmata_tailring.edge_download.paths._drives", lambda kinds=(2, 3): [tmp_path / "X"])
    monkeypatch.delenv("FFMPEG_HOME", raising=False)
    monkeypatch.delenv("FFMPEG_DIR", raising=False)
    dirs = ffmpeg_tools._candidate_dirs()
    assert dirs[0] == ffmpeg_tools.FFMPEG_HOME and tools in dirs
    assert not any(str(d).upper().startswith("F:\\APPLICATIONS") for d in dirs)
    source = Path(ffmpeg_tools.__file__).read_text(encoding="utf-8")
    assert "F:\\\\Applications" not in source and 'r"F:' not in source
