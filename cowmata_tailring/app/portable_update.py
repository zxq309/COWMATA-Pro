"""Manifest-verified portable ZIP updates, including legacy installed copies."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path

try:
    from . import update_worker as worker
    from .update_core import digest, version_key
except ImportError:
    import update_worker as worker
    from update_core import digest, version_key

MAX_EXPANDED = 10 * 1024**3

def zip_plan(archive, version):
    roots = {f"COWMATA-{brand}-{version.removeprefix('v')}-Portable" for brand in ("Pro", "Annotator")}
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if not infos or len(infos) > 30000:
            raise ValueError("便携包文件清单异常")
        entries, names = {}, set()
        prefix = None
        for info in infos:
            parts = info.filename.split("/")
            if parts[0] not in roots or prefix and parts[0] != prefix:
                raise ValueError("便携包顶层目录与版本不一致")
            prefix = parts[0]
            rel = "/".join(parts[1:]).rstrip("/")
            if not rel and info.is_dir():
                continue
            if (not rel or "\\" in rel or ":" in rel or any(p in {"", ".", ".."} or p.rstrip(" .") != p
                    or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in rel.split("/"))):
                raise ValueError("便携包包含不安全路径")
            if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError("便携包不支持链接或加密条目")
            if info.is_dir():
                continue
            if rel.casefold() in names:
                raise ValueError("便携包包含重复文件名")
            names.add(rel.casefold())
            entries[rel] = info
        manifest_info = entries.get("package-manifest.json")
        if not manifest_info or not 0 < manifest_info.file_size <= 8 * 1024**2:
            raise ValueError("便携包缺少有效文件清单")
        raw = bundle.read(manifest_info)
        manifest = json.loads(raw)
        if version_key(manifest["version"]) != version_key(version):
            raise ValueError("便携包文件清单版本不一致")
        rows = manifest["files"]
        if not isinstance(rows, list):
            raise ValueError("便携包清单格式错误")
        owned = {}
        for row in rows:
            rel, size, sha = row["path"], row["size"], row["sha256"]
            if rel in owned or rel == "package-manifest.json":
                raise ValueError("便携包清单包含重复项")
            if type(size) is not int or not 0 <= size <= MAX_EXPANDED or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("便携包清单大小或哈希无效")
            if rel not in entries or entries[rel].file_size != size:
                raise ValueError("便携包文件与清单不一致：" + rel)
            owned[rel] = row
        required = {"COWMATA.exe", "runtime/python.exe", "runtime/pythonw.exe", "cowmata_tailring/__init__.py",
                    "cowmata_tailring/workspace/modern_window.py"}
        if not required.issubset(owned) or set(entries) != set(owned) | {"package-manifest.json"}:
            raise ValueError("便携包缺少运行文件或含有清单外文件")
        total = sum(info.file_size for info in entries.values())
        if not 0 < total <= MAX_EXPANDED:
            raise ValueError("便携包解压大小超出限制")
        return dict(prefix=prefix, files=owned, total=total, manifest=raw)

def local_update(archive):
    """The user explicitly selected this local package; still verify every file on extraction."""
    archive = Path(archive).resolve()
    match = re.fullmatch(r"COWMATA-(?:Pro|Annotator)-(.+)-Portable.zip", archive.name)
    if not match:
        raise ValueError("请选择完整 COWMATA Portable.zip")
    version = match[1]
    version_key(version)
    plan = zip_plan(archive, version)
    return dict(schema=2, product="cowmata-annotator", version=version, kind="portable_zip",
                name=archive.name, size=archive.stat().st_size, sha256=digest(archive),
                unpacked_size=plan["total"], notes="用户选择的本地完整便携包", source="local")

def extract(archive, stage, plan, progress=lambda *_:None):
    stage.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for i, (rel, row) in enumerate(plan["files"].items(), 1):
            dest = worker.member(stage, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(plan["prefix"] + "/" + rel) as source, dest.open("xb") as target:
                shutil.copyfileobj(source, target, 1024*1024)
            if dest.stat().st_size != row["size"] or digest(dest) != row["sha256"]:
                raise ValueError("便携包解压校验失败：" + rel)
            progress(i, len(plan["files"]), rel)
    (stage / "package-manifest.json").write_bytes(plan["manifest"])

def forget_install_registration(root, old_version):
    # Existing shortcuts continue pointing at the same COWMATA.exe.
    # A portable distribution has no installer-owned uninstaller.
    if os.name == "nt" and worker.registered(root, old_version):
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, worker.REG_BASE + "COWMATA-" + old_version)

def install_locked(job, *, runner, restart=True):
    root = worker.safe_path(job["root"])
    archive = worker.safe_path(job["setup"], allow_file=True)
    update = job["update"]
    version = update["version"]
    old = json.loads((root/"package-manifest.json").read_text(encoding="utf-8"))["version"]
    if version_key(version) <= version_key(old):
        raise ValueError("仅允许更新到更高版本")
    if not worker.is_product_installation(root):
        raise ValueError("目标不是已核验的 COWMATA 程序目录")
    if archive.stat().st_size != update["size"] or digest(archive) != update["sha256"]:
        raise ValueError("便携包在下载后发生变化")
    plan = zip_plan(archive, version)
    worker.inventory(root)
    if shutil.disk_usage(root.parent).free < plan["total"] + 128*1024**2:
        raise OSError("磁盘空间不足，旧版保持不变")
    job_dir = worker.safe_path(job["job_dir"])
    if job_dir.is_relative_to(root) or archive.is_relative_to(root):
        raise ValueError("更新程序和压缩包必须位于目标软件目录外")
    token = uuid.uuid4().hex[:8]
    stage = worker.safe_path(root.parent/(".cma-"+token+"-stage"))
    backup = worker.safe_path(root.parent/(".cma-"+token+"-backup"))
    if stage.exists() or backup.exists():
        raise ValueError("更新暂存目录已存在")
    state = dict(root=str(root), stage=str(stage), backup=str(backup), version=version, old_version=old,
                 package_kind="portable_zip", timings_seconds={})
    start = time.monotonic()
    def phase(value):
        state["phase"] = value
        state["timings_seconds"][value] = round(time.monotonic()-start,3)
        worker.write_json(job_dir/"result.json",state)
    phase("waiting")
    deadline = time.monotonic()+180
    while runner([root/"COWMATA.exe","--check-running"],timeout=15).returncode:
        if time.monotonic() > deadline:
            raise TimeoutError("请保存并关闭旧版软件，再重新运行更新")
        time.sleep(.5)
    (root/worker.LOCK).write_text(str(os.getpid()),encoding="ascii")
    swapped = False
    try:
        phase("extracting")
        last = [0.]
        def progress(current,total,rel):
            if time.monotonic()-last[0] >= .4 or current == total:
                state.update(verified_files=current,total_files=total,current_file=rel)
                worker.write_json(job_dir/"result.json",state)
                last[0]=time.monotonic()
        extract(archive,stage,plan,progress)
        phase("verifying")
        worker.inventory(stage,verify=True)
        phase("import_testing")
        smoke = ("import sys;sys.path.insert(0,sys.argv[1]);from cowmata_tailring import __version__;"
                 "from cowmata_tailring.workspace.modern_window import MainWindow;"
                 "from cowmata_tailring.app.update_ui import UpdateController;print(__version__)")
        result = runner([stage/"runtime/python.exe","-I","-B","-c",smoke,stage],timeout=90)
        if result.returncode or version_key(result.stdout.decode("utf-8","replace").strip()) != version_key(version):
            raise RuntimeError("新版运行库检查失败，旧版未修改")
        worker.inventory(root)
        if runner([root/"COWMATA.exe","--check-running"],timeout=15).returncode:
            raise RuntimeError("旧版被重新打开，请保存关闭后重试")
        (stage/worker.LOCK).write_text(str(os.getpid()),encoding="ascii")
        phase("swapping")
        root.rename(backup)
        try:
            stage.rename(root)
        except OSError:
            backup.rename(root)
            raise
        swapped = True
        phase("committed")
    except Exception as exc:
        if swapped:
            root.rename(stage)
            backup.rename(root)
        (root/worker.LOCK).unlink(missing_ok=True)
        state["error"]=str(exc)
        phase("rolled_back" if swapped else "failed_before_swap")
        raise
    (root/worker.LOCK).unlink(missing_ok=True)
    if restart:
        phase("restarting")
        try:
            worker.start_updated_app(root,version)
            state["application_launched"]=True
        except OSError as exc:
            state["restart_warning"]=str(exc)
    try:
        forget_install_registration(root,old)
        if not state.get("restart_warning"):
            phase("cleaning_backup")
            worker.remove_owned(backup)
    except (OSError,ValueError) as exc:
        state["cleanup_warning"]=str(exc)
    phase("complete")
    return state
