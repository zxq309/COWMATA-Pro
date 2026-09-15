import hashlib
import io
import json
import subprocess
import urllib.error
import zipfile
from pathlib import Path

import pytest

from cowmata_tailring.app import portable_update as portable
from cowmata_tailring.app import update_core as core
from cowmata_tailring.app import update_worker as worker


class Response(io.BytesIO):
    def __init__(self, data=b"", status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


def product(root, version):
    root.mkdir(parents=True)
    content = {
        "COWMATA.exe": b"test launcher",
        "runtime/python.exe": b"test runtime",
        "runtime/pythonw.exe": b"test runtime",
        "cowmata_tailring/__init__.py": ('__version__="' + version + '"').encode(),
        "cowmata_tailring/workspace/modern_window.py": b"# test window",
    }
    for rel, raw in content.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    manifest = dict(
        version=version,
        files=[
            dict(path=k, size=len(v), sha256=hashlib.sha256(v).hexdigest())
            for k, v in content.items()
        ],
    )
    (root / "package-manifest.json").write_text(json.dumps(manifest))
    return root


def bundle(tmp_path, version="3.9.0", extra=None):
    root = product(tmp_path / "payload", version)
    archive = tmp_path / f"COWMATA-Pro-{version}-Portable.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob("*"):
            if p.is_file():
                z.write(p, f"COWMATA-Pro-{version}-Portable/" + p.relative_to(root).as_posix())
        if extra:
            z.writestr(extra, b"bad")
    return archive


def test_rest_403_uses_official_feed_then_downloads_sha_verified_zip(tmp_path):
    archive = bundle(tmp_path)
    raw = archive.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    name = archive.name
    url = f"https://github.com/{core.REPO}/releases/download/v3.9.0/{name}"
    feed = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="{core.PAGE}/tag/v3.9.0"/><content>Release</content></entry></feed>'.encode()
    html = f'<li><a href="{url}">{name}</a><span>sha256:{sha}</span></li>'.encode()
    calls = []

    def opener(url, headers=None):
        calls.append((url, headers))
        if url.startswith(core.API):
            raise urllib.error.HTTPError(url, 403, "rate limit", {}, None)
        if url.endswith(".atom"):
            return Response(feed)
        if "/tag/" in url:
            return Response(b"<span>Latest</span>")
        if "/expanded_assets/" in url:
            return Response(html)
        offset = int((headers or {}).get("Range", "bytes=0-").split("=")[1].split("-")[0])
        if (headers or {}).get("Range") == "bytes=0-0":
            return Response(raw[:1], 206, {"Content-Range": f"bytes 0-0/{len(raw)}"})
        return Response(
            raw[offset:], 206, {"Content-Range": f"bytes {offset}-{len(raw) - 1}/{len(raw)}"}
        )

    update = core.check_update("3.8.0", "stable", opener)
    assert update["kind"] == "portable_zip" and update["metadata_source"] == "github_public_release"
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (name + ".part")).write_bytes(raw[:13])
    result = core.download(update, cache, opener=opener)
    assert result.read_bytes() == raw
    assert any(headers == {"Range": "bytes=13-"} for _, headers in calls)


@pytest.mark.parametrize(
    "extra",
    [
        "COWMATA-Pro-3.9.0-Portable/../outside.txt",
        "COWMATA-Pro-3.9.0-Portable/unknown.txt",
        "COWMATA-Pro-3.9.0-Portable/CON.txt",
        "COWMATA-Pro-3.9.0-Portable/RUNTIME/PYTHON.EXE",
    ],
)
def test_zip_rejects_traversal_unowned_reserved_and_case_collisions(tmp_path, extra):
    archive = bundle(tmp_path, extra=extra)
    with pytest.raises(ValueError):
        portable.zip_plan(archive, "3.9.0")


@pytest.mark.parametrize("failure", [None, "corrupt", "smoke", "unowned", "swap"])
def test_portable_update_transaction_keeps_user_data_and_rolls_back(tmp_path, monkeypatch, failure):
    target = product(tmp_path / "app", "3.8.0")
    archive = bundle(tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job = dict(
        root=str(target),
        setup=str(archive),
        job_dir=str(job_dir),
        update=portable.local_update(archive),
    )
    outside = tmp_path / "user-label.json"
    outside.write_text("KEEP")
    if failure == "corrupt":
        archive.write_bytes(archive.read_bytes() + b"corrupt")
    if failure == "unowned":
        (target / "customer-label.json").write_text("KEEP")
    old = (target / "cowmata_tailring/__init__.py").read_bytes()

    def runner(args, timeout=0):
        if "-c" in args:
            return subprocess.CompletedProcess(
                args, 1 if failure == "smoke" else 0, b"3.9.0\n", b""
            )
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(portable, "forget_install_registration", lambda *_: None)
    if failure == "swap":
        rename = Path.rename

        def fail_stage(path, dest):
            if path.name.endswith("-stage") and Path(dest) == target:
                raise PermissionError("synthetic locked target")
            return rename(path, dest)

        monkeypatch.setattr(Path, "rename", fail_stage)
    if failure:
        with pytest.raises((ValueError, RuntimeError, PermissionError)):
            worker.install(job, runner=runner, restart=False)
        assert (target / "cowmata_tailring/__init__.py").read_bytes() == old
        if failure == "unowned":
            assert (target / "customer-label.json").read_text() == "KEEP"
    else:
        result = worker.install(job, runner=runner, restart=False)
        assert result["phase"] == "complete"
        assert json.loads((target / "package-manifest.json").read_text())["version"] == "3.9.0"
        assert not Path(result["backup"]).exists()
    assert outside.read_text() == "KEEP"
    assert not (target / worker.LOCK).exists()


def test_public_release_never_trusts_foreign_asset(tmp_path):
    feed = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="{core.PAGE}/tag/v3.9.0"/></entry></feed>'.encode()

    def opener(url, headers=None):
        if url.endswith(".atom"):
            return Response(feed)
        if "/tag/" in url:
            return Response(b"Latest")
        return Response(
            b'<li><a href="https://evil.invalid/COWMATA-Pro-3.9.0-Portable.zip">package</a>sha256:'
            + b"a" * 64
            + b"</li>"
        )

    with pytest.raises(ValueError):
        core.public_releases("3.8.0", "stable", opener)


def test_portable_package_must_be_newer_before_replacement(tmp_path, monkeypatch):
    target = product(tmp_path / "app", "3.9.0")
    archive = bundle(tmp_path)
    jobdir = tmp_path / "job"
    jobdir.mkdir()
    with pytest.raises(ValueError, match="更高"):
        worker.install(
            dict(
                root=str(target),
                setup=str(archive),
                job_dir=str(jobdir),
                update=portable.local_update(archive),
            ),
            restart=False,
        )


@pytest.mark.parametrize("limited", [False, True])
def test_installed_release_prefers_setup_with_and_without_rest(limited):
    version = "3.9.0"
    sha = "a" * 64
    names = [f"COWMATA-Pro-{version}-Portable.zip", f"COWMATA-Pro-{version}-Setup.exe"]
    assets = [
        dict(
            name=n,
            size=100,
            digest="sha256:" + sha,
            state="uploaded",
            browser_download_url=f"{core.PAGE}/download/v{version}/{n}",
        )
        for n in names
    ]
    release = dict(tag_name="v" + version, assets=assets, body="release")
    feed = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="{core.PAGE}/tag/v{version}"/></entry></feed>'.encode()
    listing = "".join(
        f'<li><a href="{a["browser_download_url"]}">{a["name"]}</a>sha256:{sha}</li>'
        for a in assets
    ).encode()

    def opener(url, headers=None):
        if url.startswith(core.API):
            if limited:
                raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
            return Response(json.dumps([release]).encode())
        if url.endswith(".atom"):
            return Response(feed)
        if "/tag/" in url:
            return Response(b"Latest")
        if "/expanded_assets/" in url:
            return Response(listing)
        return Response(b"x", 206, {"Content-Range": "bytes 0-0/100"})

    installed = core.check_update("3.8.0", "stable", opener, package_kind="installer")
    assert installed["kind"] == "installer_exe" and installed["name"] == names[1]
    assert core.check_update("3.8.0", "stable", opener)["name"] == names[0]


def test_transient_download_retries_from_written_offset(tmp_path, monkeypatch):
    raw = b"abcdef"
    calls = []

    class InterruptedResponse(Response):
        def read(self, n=-1):
            if self.tell():
                raise ConnectionResetError("connection reset")
            return super().read(3)

    def opener(url, headers=None):
        calls.append(headers)
        if len(calls) == 1:
            return InterruptedResponse(raw)
        return Response(raw[3:], 206, {"Content-Range": "bytes 3-5/6"})

    monkeypatch.setattr(core.time, "sleep", lambda *_: None)
    tick = iter(range(100))
    monkeypatch.setattr(core.time, "monotonic", lambda: next(tick))
    update = dict(
        name="test.zip",
        size=6,
        sha256=hashlib.sha256(raw).hexdigest(),
        url="https://github.com/" + core.REPO + "/releases/download/v3.9.0/test.zip",
    )
    assert core.download(update, tmp_path, opener=opener).read_bytes() == raw
    assert calls == [{}, {"Range": "bytes=3-"}]


def test_controller_keeps_installed_preference_at_final_prepare(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.app.update_ui import UpdateController

    _app = QApplication.instance() or QApplication([])
    (tmp_path / "COWMATA.install-id").write_text("COWMATA-3.8.0")
    calls = []
    monkeypatch.setattr(core, "check_update", lambda *a, **kw: calls.append(kw) or None)

    # Exercise the controller's routing without starting background transport.
    class Controller:
        root = tmp_path

        def channel(self):
            return "stable"

    assert UpdateController.check_release(Controller()) is None
    assert calls == [{"package_kind": "installer"}]
