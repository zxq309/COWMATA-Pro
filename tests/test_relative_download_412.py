"""Relocation, upgrade isolation and the explicit refresh-and-download action."""
import json
import os
import shutil
from contextlib import nullcontext
from functools import partial
from pathlib import Path

import pytest

from cowmata_tailring.edge_download.core import DownloadError
from cowmata_tailring.edge_download.paths import resolve_location
from cowmata_tailring.edge_download.pro_settings import ProSettings
from cowmata_tailring.edge_download.settings import SettingsStore, defaults
from cowmata_tailring.edge_download.site_records import SCHEMAS, refresh_records
from test_download_repair_391 import client_for, record
from test_edge_download_384 import csv_content, spin


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = tmp_path / "installation" / "Pro"
    root.mkdir(parents=True)
    return ProSettings(tmp_path / "config", app_root=root)


def site_tree(base):
    """A minimal 4.4.6 site tree with the portable app unpacked under 2_标注器."""
    farm = base / "1_下载器" / "扬大_高邮牧场"
    (farm / "台账").mkdir(parents=True)
    app = base / "2_标注器" / "4.4.6" / "COWMATA-Pro-4.4.6-Portable"
    app.mkdir(parents=True)
    return farm, app


def test_relocation_uses_application_location_not_working_directory(tmp_path, monkeypatch):
    """4.4.6: locations are saved relative to the directory tree and follow it to another drive or computer."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    farm, app = site_tree(tmp_path / "E")
    store = ProSettings(tmp_path / "config", app_root=app)
    assert Path(store.value["data_root"]) == farm and Path(store.value["ledger_directory"]) == farm / "台账"
    store.save()
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["data_root"] == "目录树/1_下载器/扬大_高邮牧场"
    assert saved["ledger_directory"] == "目录树/1_下载器/扬大_高邮牧场/台账"
    (farm / "existing.json").write_bytes(b"original data")
    shutil.move(str(tmp_path / "E"), tmp_path / "F")  # the whole tree on another drive
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    moved_app = tmp_path / "F" / "2_标注器" / "4.4.6" / "COWMATA-Pro-4.4.6-Portable"
    loaded = ProSettings(store.directory, app_root=moved_app)
    assert Path(loaded.value["data_root"]) == tmp_path / "F" / "1_下载器" / "扬大_高邮牧场"
    assert (Path(loaded.value["data_root"]) / "existing.json").read_bytes() == b"original data"
    assert not any(cwd.iterdir())


def test_tree_on_another_drive_is_found_and_missing_tree_never_writes_elsewhere(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download import paths
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    farm, _ = site_tree(tmp_path / "E")
    app = tmp_path / "D" / "COWMATA Annotator"  # installed outside the tree
    app.mkdir(parents=True)
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path / "E"))
    store = ProSettings(tmp_path / "config", app_root=app)
    assert Path(store.value["data_root"]) == farm
    store.save()
    assert json.loads(store.path.read_text("utf-8"))["data_root"] == "目录树/1_下载器/扬大_高邮牧场"
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path / "unplugged"))  # the tree's drive is not plugged in
    offline = ProSettings(tmp_path / "config", app_root=app)
    assert offline.tree_missing and offline.display_path(offline.value["data_root"]) == "目录树/1_下载器/扬大_高邮牧场"
    with pytest.raises(DownloadError, match="目录树"):
        offline.check_tree()
    offline.save(sync_ledger=True)  # saving other settings keeps the tree location
    assert json.loads(store.path.read_text("utf-8"))["data_root"] == "目录树/1_下载器/扬大_高邮牧场"
    assert not any(p.name == "1_下载器" for p in tmp_path.rglob("*") if p.is_dir() and "E" not in p.parts)
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path / "E"))
    back = ProSettings(tmp_path / "config", app_root=app)
    back.check_tree()
    assert Path(back.value["data_root"]) == farm and paths.tree_root(app) == tmp_path / "E"


def test_files_of_the_old_default_folder_move_into_the_tree(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download.site_adopt import adopt_all
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    farm, _ = site_tree(tmp_path / "E")
    app = tmp_path / "D" / "COWMATA Annotator"
    app.mkdir(parents=True)
    old = tmp_path / "D" / "COWMATA Pro 数据" / "下载数据"
    (old / "产犊" / "Motion" / "2026-09-01" / "A-1").mkdir(parents=True)
    (old / "产犊" / "Motion" / "2026-09-01" / "A-1" / "x.json").write_bytes(b"new file")
    (old / "产犊" / "Motion" / "2026-09-01" / "A-1" / "same.json").write_bytes(b"same")
    (farm / "产犊" / "Motion" / "2026-09-01" / "A-1").mkdir(parents=True)
    (farm / "产犊" / "Motion" / "2026-09-01" / "A-1" / "same.json").write_bytes(b"same")
    (old / ".edge-download").mkdir()
    (old / ".edge-download" / "csv-provenance.jsonl").write_bytes(b'{"path":"x"}\n')
    settings = tmp_path / "config"
    settings.mkdir()
    (settings / "automatic-download.json").write_text(json.dumps(dict(
        defaults_value(), data_root="../COWMATA Pro 数据/下载数据", ledger_directory="../COWMATA Pro 数据/现场台账")),
        encoding="utf-8")
    monkeypatch.setenv("COWMATA_SITE_TREE", str(tmp_path / "E"))
    store = ProSettings(settings, app_root=app)
    assert Path(store.value["data_root"]) == farm and "目录树" in store.notice
    assert [(Path(a), Path(b)) for a, b in store.adopt] == [(old, farm)]
    messages = []
    adopt_all(store.adopt, messages.append)
    assert (farm / "产犊" / "Motion" / "2026-09-01" / "A-1" / "x.json").read_bytes() == b"new file"
    assert (farm / ".edge-download" / "csv-provenance.jsonl").read_bytes() == b'{"path":"x"}\n'
    assert not (tmp_path / "D" / "COWMATA Pro 数据").exists() and "移入 1" in messages[0]


def test_upgrade_replaces_program_tree_without_touching_sibling_data(isolated_store):
    store = isolated_store
    store.save()
    raw = Path(store.value["data_root"]) / "existing.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"raw unchanged")
    old = store.app_root.with_name("old-program")
    store.app_root.rename(old)
    store.app_root.mkdir()
    loaded = ProSettings(store.directory, app_root=store.app_root)
    assert loaded.value["data_root"] == store.value["data_root"]
    assert raw.read_bytes() == b"raw unchanged"


def test_custom_existing_absolute_directory_is_preserved(isolated_store, tmp_path):
    target = tmp_path / "my-data"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"keep")
    isolated_store.save(data_root=str(target))
    loaded = ProSettings(isolated_store.directory, app_root=tmp_path / "other-app")
    assert loaded.value["data_root"] == str(target)
    assert (target / "keep.bin").read_bytes() == b"keep"


@pytest.mark.parametrize("path", ["", ".", "..", "runtime/data", "F:", r"\data"])
def test_rejects_broad_ambiguous_or_program_owned_paths(isolated_store, path):
    with pytest.raises(DownloadError):
        isolated_store.save(data_root=path)


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy defaults")
def test_missing_legacy_drive_is_backed_up_without_rewriting_source(isolated_store, monkeypatch):
    store = isolated_store
    value = dict(store.value, data_root=r"F:\扬大_高邮牧场", ledger_directory=r"F:\牛舍\_现场记录")
    original = json.dumps(value, ensure_ascii=False).encode("utf-8")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(original)
    is_dir = Path.is_dir
    # Pretend the legacy drive is gone (the test folders themselves may live on F:).
    keep = str(store.directory.parent)
    monkeypatch.setattr(Path, "is_dir", lambda p: False if p.drive.upper() == "F:" and not str(p).startswith(keep) else is_dir(p))
    loaded = ProSettings(store.directory, app_root=store.app_root)
    assert store.path.read_bytes() == original
    loaded.save()
    assert store.path.with_name("automatic-download.before-4.1.2.json").read_bytes() == original
    # 4.4.6: the old F: folders switch to the tree's farm (test mode: the tree beside the app).
    assert Path(loaded.value["data_root"]) == store.app_root.parent / "1_下载器" / "扬大_高邮牧场"


def defaults_value():
    from cowmata_tailring.edge_download.site_records import settings_defaults
    value = dict(defaults(Path("C:/placeholder")), **settings_defaults())
    value.update(schema_390=1, kinds=["motion", "pulse", "temp"], server="http://device.cowmata.com:8010")
    return value


def test_standalone_downloader_retains_absolute_settings_contract(tmp_path):
    store = SettingsStore(tmp_path / "standalone", tmp_path / "raw")
    store.save()
    assert json.loads(store.path.read_text(encoding="utf-8"))["data_root"] == str(tmp_path / "raw")


def test_equipment_merged_excel_date_uses_derived_catalog_date_without_error(tmp_path):
    from cowmata_tailring.edge_download.csv_targets import CsvPlan
    from test_edge_download_384 import csv_content
    folder = tmp_path / "ledger"
    folder.mkdir()
    sample = csv_content("samples")
    equipment = csv_content("equipment", **{
        "日期": "新硅胶垫",
        "新佩戴牛号": "23077-E",
        "设备编码": "546C50CA07FA",
        "记录类型": "佩戴",
        "归类目录": "扬大_高邮牧场/设备台账/佩戴台账/2026-09-06/23077-E",
    })
    calving = csv_content("calving")
    (folder / "样本试验台账.csv").write_bytes(sample)
    (folder / "扬大测试设备台账.csv").write_bytes(equipment)
    (folder / "扬大产犊登记汇总.csv").write_bytes(calving)
    plan = CsvPlan(folder)
    assert not any(issue["row"] == 2 and issue["source"] == "扬大测试设备台账.csv"
                   for issue in plan.issues)


def test_unknown_calving_outcome_is_a_note_and_does_not_block_plan(tmp_path):
    from cowmata_tailring.edge_download.csv_targets import CsvPlan
    from test_edge_download_384 import csv_content
    folder = tmp_path / "ledger"
    folder.mkdir()
    (folder / "样本试验台账.csv").write_bytes(csv_content("samples"))
    (folder / "扬大测试设备台账.csv").write_bytes(csv_content("equipment"))
    (folder / "扬大产犊登记汇总.csv").write_bytes(
        csv_content("calving", **{"牛场登记生产时间": "死胎", "牛号": "23077"})
    )
    plan = CsvPlan(folder)
    assert plan.ready
    assert not any("死胎" in x["message"] for x in plan.issues)
    assert any("死胎" in x["message"] for x in plan.notes)


def payloads():
    return {sheet: csv_content(sheet, **({
        "牛号": "23077-E", "佩戴开始": "2026-08-18 00:00:00", "佩戴结束": "2026-08-19 00:00:00",
        "监测目的": "产犊监测", "数据分类": "calving", "产犊开始": "2026-08-18 08:00:00",
        "产犊结束": "2026-08-18 09:00:00", "九轴": "有效", "脉搏": "有效", "温度": "有效",
    } if sheet == "samples" else {})) for sheet in SCHEMAS}


@pytest.mark.parametrize("fail_refresh", [False, True])
def test_one_click_fetches_all_csv_before_downloading_and_keeps_cache_on_failure(
        isolated_store, qt_application, monkeypatch, fail_refresh):
    from cowmata_tailring.edge_download import pro_dialog as ui
    from cowmata_tailring.edge_download.csv_download import run_csv_job
    from cowmata_tailring.edge_download.csv_targets import CsvPlan
    store = isolated_store
    store.value.update(sync_ledger=False, download_mode="scheduled",
                       start_time="2026-08-18T00:00:00+08:00", end_time="2026-08-19T00:00:00+08:00")
    content = payloads()
    ledger = Path(store.value["ledger_directory"])
    ledger.mkdir(parents=True)
    for sheet, schema in SCHEMAS.items():
        (ledger / schema["filename"]).write_bytes(content[sheet])
    calls, downloads, operations = [], [], []
    class Ledger:
        def __init__(self, *args): pass
        def pull(self, sheet):
            calls.append(sheet)
            if fail_refresh and sheet == "equipment":
                raise OSError("test connection unavailable")
            return content[sheet]
    raw_client = client_for([("motion", record("motion")), ("pulse", record("pulse"))], downloads)
    def run(job, *args, **kwargs):
        assert calls == list(SCHEMAS)
        assert CsvPlan(job.ledger_directory).ready
        return run_csv_job(job, *args, client_factory=raw_client, **kwargs)
    def factory(values, operation, parent):
        operations.append(operation)
        assert values["sync_ledger"] is True
        return ui.SyncWorker(values, operation, parent, runner=run,
                             refresher=partial(refresh_records, client_factory=Ledger))
    monkeypatch.setattr(ui, "raw_connection", lambda *args: nullcontext())
    dialog = ui.ProDownloadDialog(store=store, worker_factory=factory)
    try:
        assert Path(dialog.directory.text()) == store.app_root.parent / "1_下载器" / "扬大_高邮牧场"
        dialog.download_button.click()
        spin(qt_application, lambda: not dialog.running)
        assert operations == ["all"]
        # 4.4.1: download keeps going (next round scheduled) until 暂停.
        assert dialog.timer.isActive() == (not fail_refresh) or dialog.armed
        dialog.download_button.click()
        assert not dialog.timer.isActive() and not dialog.armed
        if fail_refresh:
            assert not downloads
            assert "刷新未完成" in dialog.csv_receipt.text()
        else:
            assert len(downloads) == 2
            assert store.value["last_cycle"]["saved"] == 2
            assert len(list(Path(store.value["data_root"]).glob("产犊/*/*/*/*.json"))) == 2
        for sheet, schema in SCHEMAS.items():
            assert (ledger / schema["filename"]).read_bytes() == content[sheet]
    finally:
        dialog.stop_task()
        spin(qt_application, lambda: not dialog.workers())
        dialog.deleteLater()


def test_opening_window_only_refreshes_ledger_not_raw_data(isolated_store, qt_application):
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog
    calls = []
    dialog = ProDownloadDialog(store=isolated_store, refresh_on_open=True)
    dialog.start_task = lambda operation, **kwargs: calls.append(operation)
    try:
        dialog.show()
        spin(qt_application, lambda: bool(calls))
        assert calls == ["ledger"]
        dialog.hide()
        dialog.show()
        qt_application.processEvents()
        assert calls == ["ledger"]
        assert not dialog.armed
    finally:
        dialog.stop_task()
        spin(qt_application, lambda: not dialog.workers())
        dialog.deleteLater()


def test_incomplete_ledger_rows_are_ignored_from_the_visible_download_plan(isolated_store, qt_application):
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog
    dialog = ProDownloadDialog(store=isolated_store)
    class Plan:
        issues = []
        def preview(self):
            def row(source, eligibility, number, reason, device):
                return {"source": source, "eligibility": eligibility, "row": number,
                        "reason": reason, "device": device, "cow": "", "mark": "",
                        "start": "2026-01-01T00:00:00+08:00", "end": "",
                        "category": "待核对", "warnings": ""}
            return [row("样本试验台账.csv", "eligible", 2, "ready", "A"),
                    row("样本试验台账.csv", "pending", 3, "待补全：九轴", "B"),
                    row("样本试验台账.csv", "excluded", 4, "温度无效", "C"),
                    row("扬大测试设备台账.csv", "reference", 5, "reference", "D")]
    try:
        dialog.receive_plan(Plan(), "")
        assert [row["device"] for row in dialog.plan_records] == ["A", "C"]
        assert "待补全 1" in dialog.plan_label.text()
        assert all(item["row"] != 3 for item in dialog.csv_issues)
    finally:
        dialog.deleteLater()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows installer")
def test_installer_creates_sibling_data_directories_and_preserves_existing_files(tmp_path):
    import subprocess
    compiler = os.environ.get("COWMATA_NSIS_COMPILER")
    if not compiler:
        pytest.skip("NSIS compiler required")
    source = (Path(__file__).parents[1] / "packaging/installer.nsi").read_text(encoding="utf-8")
    create = source.split("  register_installation:\n", 1)[1].split("  SetOutPath", 1)[0]
    destination = tmp_path / "installed-pro"
    exe = tmp_path / "create-dirs.exe"
    script = tmp_path / "create-dirs.nsi"
    keep = tmp_path / "COWMATA Pro 数据/下载数据/keep.json"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"original recording")
    script.write_text("\n".join([
        "Unicode true", f'OutFile "{exe}"', f'InstallDir "{destination}"',
        "RequestExecutionLevel user", "SilentInstall silent", "AutoCloseWindow true",
        "Section", create, "SectionEnd", "",
    ]), encoding="utf-8")
    subprocess.run([compiler, "/INPUTCHARSET", "UTF8", "/V2", str(script)],
                   check=True, timeout=30, creationflags=0x08000000)
    subprocess.run([str(exe), "/S"], check=True, timeout=30, creationflags=0x08000000)
    assert (keep.parent.parent / "现场台账").is_dir()
    assert keep.read_bytes() == b"original recording"
    assert not (destination / "COWMATA Pro 数据").exists()
