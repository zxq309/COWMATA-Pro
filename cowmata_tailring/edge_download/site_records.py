"""Read-only Ledger 1.3.1 protocol and verified, recoverable three-CSV refresh."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PureWindowsPath

from .core import Cancelled, DownloadError
from .deduplication import RootSyncLock, _safe
from .ledger_direct import DirectBridge, clean_environment
from .paths import DEFAULT_LEDGER_ROOT
from .settings import atomic_json

SCHEMAS = {
    "samples": {
        "filename": "样本试验台账.csv",
        "fields": [
            "序号",
            "牛号",
            "预产期",
            "设备号",
            "佩戴开始",
            "佩戴结束",
            "产犊开始",
            "产犊结束",
            "监测目的",
            "九轴",
            "脉搏",
            "温度",
            "尾环佩戴人",
            "事件标注人",
            "样本评价",
            "记录日期",
            "牧场",
            "数据分类",
            "现场标记",
            "归类目录",
            "核对提示",
            "来源",
            "原始单元格",
            "时间说明",
            "记录ID",
            "版本",
            "修改时间",
            "已删除",
        ],
    },
    "calving": {
        "filename": "扬大产犊登记汇总.csv",
        "fields": [
            "生产日期",
            "牛场登记生产时间",
            "牛号",
            "预产期",
            "提前预产期天数",
            "是否佩戴过尾环",
            "是否处于有效监测范围",
            "备注(最后一次佩戴时间）",
            "记录日期",
            "牧场",
            "工作表",
            "记录类型",
            "核对提示",
            "来源",
            "原始单元格",
            "时间说明",
            "归类目录",
            "记录ID",
            "版本",
            "修改时间",
            "已删除",
        ],
    },
    "equipment": {
        "filename": "扬大测试设备台账.csv",
        "fields": [
            "日期",
            "新佩戴牛号",
            "设备编码",
            "拆除时间(掉落）",
            "佩戴时长（天）",
            "设备去向",
            "库存数量",
            "备注",
            "设备来源",
            "设备数量",
            "设备号",
            "故障数量",
            "故障设备编码",
            "故障率(%)",
            "遗失设备",
            "记录日期",
            "牧场",
            "工作表",
            "记录类型",
            "核对提示",
            "来源",
            "原始单元格",
            "时间说明",
            "归类目录",
            "记录ID",
            "版本",
            "修改时间",
            "已删除",
        ],
    },
}
SERVER_DIRECTORY = r"F:\牛舍_现场记录"
# A local mirror is kept in the user's standard COWMATA data area.  The
# server-side Windows path above is independent and remains configurable.
LOCAL_DIRECTORY = str(DEFAULT_LEDGER_ROOT)
DEFAULT_HOST = "61.177.77.222"
DEFAULT_PORT = 8022
DEFAULT_USER = "cowmata_upload"
MAX_CSV = 64 * 1024 * 1024


def read_csv(content, sheet):
    if len(content) > MAX_CSV:
        raise DownloadError("现场记录 CSV 超过 64 MiB")
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
    if reader.fieldnames != SCHEMAS[sheet]["fields"]:
        raise DownloadError(SCHEMAS[sheet]["filename"] + " 表头不符合上传器 1.3.1")
    rows = list(reader)
    ids = set()
    for row in rows:
        if None in row or any(v is None or "\0" in v or len(v) > 65536 for v in row.values()):
            raise DownloadError("现场记录 CSV 行列或字段格式错误")
        try:
            uuid.UUID(row["记录ID"])
        except ValueError as exc:
            raise DownloadError("现场记录 ID 无效") from exc
        if row["记录ID"] in ids or row["已删除"] not in ("0", "1"):
            raise DownloadError("现场记录 ID 重复或删除状态无效")
        ids.add(row["记录ID"])
    return rows


def default_key():
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    from cowmata_security.profile import profile_path
    candidates = [profile_path(), local / "Programs/COWMATA 现场台账/auth/upload_key"]
    if os.environ.get("COWMATA_LEDGER_AUTH"):
        candidates.insert(0, Path(os.environ["COWMATA_LEDGER_AUTH"]) / "upload_key")
    return str(next((p for p in candidates if p.is_file()), candidates[0]))


def settings_defaults():
    return dict(
        ledger_host=DEFAULT_HOST,
        ledger_port=DEFAULT_PORT,
        ledger_user=DEFAULT_USER,
        ledger_server_directory=SERVER_DIRECTORY,
        ledger_directory=LOCAL_DIRECTORY,
        ledger_key=default_key(),
        sync_ledger=True,
    )


def validate_connection(values):
    import ipaddress

    try:
        if not ipaddress.IPv4Address(values["ledger_host"]).is_global:
            raise ValueError()
        if not 1 <= int(values["ledger_port"]) <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise DownloadError("台账服务器需填写公网 IPv4 地址和有效 SSH 端口") from None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", values["ledger_user"]):
        raise DownloadError("台账 SSH 用户名格式无效")
    remote = PureWindowsPath(values["ledger_server_directory"])
    if not remote.is_absolute() or ".." in remote.parts or len(remote.parts) < 2:
        raise DownloadError("服务器现场记录目录需填写完整 Windows 路径")


class LedgerClient:
    def __init__(
        self, values, cancel, log=lambda message: None, app_root=None, bridge_factory=DirectBridge
    ):
        self.values, self.cancel, self.log = values, cancel, log
        self.app_root = Path(app_root) if app_root else Path(__file__).resolve().parents[2]
        self.bridge_factory = bridge_factory
        validate_connection(values)

    def pull(self, sheet):
        if self.cancel.is_set():
            raise Cancelled()
        target = str(
            PureWindowsPath(self.values["ledger_server_directory"]) / SCHEMAS[sheet]["filename"]
        )
        request = dict(version=2, action="pull", sheet_id=sheet, target=target,
                       changes=[], known_ids=[], session_token=self.values.get('session_token', ''))
        return decode_reply(self.request(request), sheet, target)

    def login(self, username='', password=''):
        from cowmata_security.client import current_session

        session = current_session()
        session.require("prepare")
        session.refresh()
        identity=session.identity
        return {'token':session.token,'expires_at':identity['expires_at'],
                'user':{'username':identity['account'],'role':identity['role']}}

    def request(self, request):
        from cowmata_security.client import current_session

        session = current_session()
        session.require("prepare")
        request={**request,'session_token':session.token,'security_product':'pro'}
        if self.cancel.is_set():
            raise Cancelled()
        key = Path(self.values["ledger_key"])
        if not key.is_file():
            key = Path(default_key())
        executable = self.app_root / "vendor/ledger-ssh/usr/bin/ssh.exe"
        hosts = Path(__file__).with_name("ledger_known_hosts")
        for p in (key, executable, hosts):
            if not p.is_file():
                raise DownloadError("缺少台账连接组件或已有授权：" + str(p))
        with self.bridge_factory(
            self.values["ledger_host"], int(self.values["ledger_port"])
        ) as bridge:
            alias = "[" + self.values["ledger_host"] + "]:" + str(self.values["ledger_port"])
            args = [
                str(executable),
                "-F",
                "none",
                "-T",
                "-o",
                "ProxyCommand=none",
                "-o",
                "ProxyJump=none",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "HostKeyAlias=" + alias,
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=10",
                "-o",
                "ServerAliveCountMax=2",
                "-o",
                "UserKnownHostsFile=" + hosts.name,
                "-i",
                str(key),
                "-p",
                str(bridge.port),
                self.values["ledger_user"] + "@127.0.0.1",
                "cowmata-ledger-upload-v2",
            ]
            # Poll bounded temporary output files, allowing cancel without retaining unbounded SSH output.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=errors,
                    cwd=hosts.parent,
                    env=clean_environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    process.stdin.write(
                        json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
                    )
                    process.stdin.close()
                    deadline = time.monotonic() + 50
                    while process.poll() is None:
                        if self.cancel.wait(0.1):
                            raise Cancelled()
                        if time.monotonic() > deadline:
                            raise DownloadError("台账服务器连接超时，保留现有 CSV，下轮重试")
                        if (
                            os.fstat(output.fileno()).st_size > MAX_CSV * 2
                            or os.fstat(errors.fileno()).st_size > 65536
                        ):
                            raise DownloadError("台账服务器响应超过允许大小")
                    output.seek(0)
                    raw = output.read(MAX_CSV * 2 + 1)
                    errors.seek(0)
                    error = errors.read(8192).decode("utf-8", "replace")
                    if len(raw) > MAX_CSV * 2:
                        raise DownloadError("台账服务器响应超过允许大小")
                    if process.returncode:
                        try:
                            rejection = json.loads(raw)
                            reason = (
                                rejection.get("error", "") if isinstance(rejection, dict) else ""
                            )
                        except (ValueError, UnicodeError):
                            reason = ""
                        raise DownloadError(
                            "台账连接未完成："
                            + (reason or error[-800:] or "请检查授权、网络及主机密钥")
                        )
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
        if self.cancel.is_set():
            raise Cancelled()
        return raw


def decode_reply(raw, sheet, target):
    try:
        reply = json.loads(raw)
        if not isinstance(reply, dict) or reply.get("version") != 2 or reply.get("ok") is not True:
            raise DownloadError(
                "台账服务器未返回上传器 v2 协议确认："
                + str(reply.get("error", "") if isinstance(reply, dict) else "")
            )
        if reply.get("target") != target or reply.get("sheet_id", sheet) != sheet:
            raise DownloadError("台账服务器返回的 Sheet 或固定保存路径不一致")
        content = base64.b64decode(reply["content"], validate=True)
        if hashlib.sha256(content).hexdigest() != reply.get("sha256"):
            raise DownloadError("台账 CSV 的 SHA-256 校验失败，保留原文件")
        rows = read_csv(content, sheet)
        if reply.get("count") != len(rows):
            raise DownloadError("台账 CSV 行数与服务器确认不一致")
        return content
    except (KeyError, UnicodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError("台账服务器返回内容无效：" + str(exc)) from exc


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".csv-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def records_state_directory(folder):
    """Keep locks, history and recoverable CSV backups out of the CSV directory."""
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    identity = os.path.normcase(str(Path(folder).resolve()))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return local / "COWMATA-Pro" / "site-records" / key


def _record_changes(previous, rows, sheet):
    if previous is None:
        return dict(added=len(rows), updated=0, removed=0, baseline="new")
    try:
        old = {r["记录ID"]: r for r in read_csv(previous, sheet)}
    except (UnicodeError, csv.Error, DownloadError):
        return dict(added=0, updated=0, removed=0, baseline="rebuilt")
    new = {r["记录ID"]: r for r in rows}
    return dict(
        added=len(new.keys() - old.keys()),
        updated=sum(new[k] != old[k] for k in new.keys() & old.keys()),
        removed=len(old.keys() - new.keys()),
        baseline="compared",
    )


def refresh_records(values, cancel, log=lambda message: None, client_factory=LedgerClient):
    """Fetch and validate all three files before touching the local mirror. Never upload."""
    folder = Path(values["ledger_directory"])
    if not folder.is_absolute() or folder == Path(folder.anchor):
        raise DownloadError("现场记录必须放在专用的绝对路径文件夹")
    folder.mkdir(parents=True, exist_ok=True)
    state_root = records_state_directory(folder)
    state_root.mkdir(parents=True, exist_ok=True)
    with RootSyncLock(state_root, cancel):
        client = client_factory(values, cancel, log)
        contents, previous, rows, changes = {}, {}, {}, {}
        for sheet, schema in SCHEMAS.items():
            p = folder / schema["filename"]
            if not _safe(folder, p, missing=True):
                raise DownloadError("现场记录路径包含链接，停止刷新")
            previous[sheet] = p.read_bytes() if p.exists() else None
            if cancel.is_set():
                raise Cancelled()
            contents[sheet] = client.pull(sheet)
            rows[sheet] = read_csv(contents[sheet], sheet)
            changes[sheet] = _record_changes(previous[sheet], rows[sheet], sheet)
        changed = [s for s in SCHEMAS if previous[s] != contents[s]]
        if cancel.is_set():
            raise Cancelled()
        # Keep a content-addressed backup outside the three business filenames.
        backup = state_root / "csv-backups"
        for sheet in changed:
            old = previous[sheet]
            if old is not None:
                dest = backup / hashlib.sha256(old).hexdigest() / SCHEMAS[sheet]["filename"]
                if not _safe(state_root, dest, missing=True):
                    raise DownloadError("现场记录备份路径包含链接")
                _write(dest, old)
        written = []
        try:
            for sheet in changed:
                dest = folder / SCHEMAS[sheet]["filename"]
                current = dest.read_bytes() if dest.exists() else None
                if current != previous[sheet] or not _safe(folder, dest, missing=True):
                    raise DownloadError("本地 CSV 在同步期间有修改，停止刷新；请稍后重试")
                _write(dest, contents[sheet])
                written.append(sheet)
        except BaseException:
            for sheet in reversed(written):
                dest = folder / SCHEMAS[sheet]["filename"]
                if dest.read_bytes() == contents[sheet]:
                    if previous[sheet] is None:
                        dest.unlink()
                    else:
                        _write(dest, previous[sheet])
            raise
        counts = {s: len(rows[s]) for s in SCHEMAS}
        checked_at = time.time()
        state = state_root / "last-csv-sync.json"
        if not _safe(state_root, state, missing=True):
            raise DownloadError("现场记录同步状态路径包含链接")
        atomic_json(
            state,
            dict(
                updated_at=checked_at,
                changes=changes,
                server_directory=values["ledger_server_directory"],
                files={
                    s: dict(sha256=hashlib.sha256(contents[s]).hexdigest(), count=counts[s])
                    for s in SCHEMAS
                },
            ),
        )
        log(
            "现场记录已核验："
            + "；".join(SCHEMAS[s]["filename"] + " " + str(counts[s]) + " 条" for s in SCHEMAS)
            + "。更新 "
            + str(len(changed))
            + " 个文件。"
        )
        return dict(changed=len(changed), counts=counts, changes=changes, checked_at=checked_at)
