"""GitHub-only release transport. Standard library; usable by detached updater.

Trust: HTTPS to the fixed public repository plus GitHub's SHA-256 asset digest.
This is NOT an Authenticode signature and does not bypass Windows SmartScreen.
No account token, sensor data, or local paths are sent to GitHub.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "zxq309/cattle-tail-ring-annotator"
API = "https://api.github.com/repos/" + REPO
PAGE = "https://github.com/" + REPO + "/releases"
MAX_INSTALLER = 2 * 1024**3 - 1
DESCRIPTOR = "cowmata-update.json"


def version_key(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-?(alpha|beta|rc)\.?([0-9]+))?(?:-r(\d+))?", value)
    if not match:
        raise ValueError("Unsupported release version: " + str(value))
    major, minor, patch, stage, number, revision = match.groups()
    return (int(major), int(minor), int(patch), {None: 3, "rc": 2, "beta": 1, "alpha": 0}[stage], int(number or 0), int(revision or 0))


def valid_url(url, *, redirect=False):
    value = urllib.parse.urlsplit(url)
    if value.scheme != "https" or value.username or value.password or value.port not in {None, 443}:
        raise ValueError("Update requires trusted HTTPS")
    host = value.hostname
    direct = ((host == "api.github.com" and value.path.startswith("/repos/" + REPO + "/releases"))
              or (host == "github.com" and value.path.startswith("/" + REPO + "/releases/download/")))
    if not direct and not (redirect and host in {"release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}):
        raise ValueError("Update URL outside the configured repository")
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        valid_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url, headers=None):
    valid_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "COWMATA-Annotator-Updater",
                                                  "Accept": "application/vnd.github+json", **(headers or {})})
    return urllib.request.build_opener(_Redirect()).open(request, timeout=30)


def read_bytes(url, limit, opener=open_url):
    with opener(url) as response:
        result = response.read(limit + 1)
    if len(result) > limit:
        raise ValueError("Update metadata too large")
    return result


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            result.update(block)
    return result.hexdigest()


def asset_info(asset):
    sha = asset.get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", sha):
        raise ValueError("Release asset lacks GitHub SHA-256; publish it again")
    size = asset.get("size")
    if not isinstance(size, int) or not 0 < size <= MAX_INSTALLER or asset.get("state") != "uploaded":
        raise ValueError("Release asset is incomplete or too large")
    return {"name": asset["name"], "size": size, "sha256": sha[7:],
            "url": valid_url(asset["browser_download_url"])}


def check_update(current, channel="preview", opener=open_url):
    if channel not in {"stable", "preview"}:
        raise ValueError("Invalid update channel")
    releases = json.loads(read_bytes(API + "/releases?per_page=100", 4 * 1024**2, opener))
    if not isinstance(releases, list):
        raise ValueError("Unexpected GitHub response")
    newer = []
    for release in releases:
        if release.get("draft"):
            continue
        try:
            key = version_key(release["tag_name"])
        except (KeyError, ValueError):
            continue
        if channel == "stable" and (release.get("prerelease") or key[3] != 3):
            continue
        if key > version_key(current):
            newer.append((key, release))
    if not newer:
        return None
    # A release without the protocol descriptor is not installable in place.
    # Report this instead of silently choosing an older, unrelated executable.
    release = max(newer, key=lambda pair: pair[0])[1]
    assets = {item["name"]: item for item in release.get("assets", [])}
    if DESCRIPTOR not in assets:
        raise ValueError("新版本尚未上传自动更新清单，请稍后重试或查看发布页")
    entry = asset_info(assets[DESCRIPTOR])
    content = read_bytes(entry["url"], 65536, opener)
    if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise ValueError("Update descriptor checksum mismatch")
    document = json.loads(content)
    if document.get("schema") != 1 or document.get("product") != "cowmata-annotator":
        raise ValueError("Unsupported update protocol or product")
    if version_key(document["version"]) != version_key(release["tag_name"]):
        raise ValueError("Release and installer versions disagree")
    installer = asset_info(assets[document["installer"]])
    if not re.fullmatch(r"COWMATA-Annotator-[A-Za-z0-9.-]+-Setup.exe", installer["name"]):
        raise ValueError("Unexpected installer name")
    if installer["size"] != document["size"] or installer["sha256"] != document["sha256"]:
        raise ValueError("Installer digest differs from GitHub metadata")
    if not re.fullmatch(r"[0-9a-f]{64}", document.get("package_sha256", "")):
        raise ValueError("Missing package integrity manifest digest")
    unpacked = document.get("unpacked_size", 0)
    if not isinstance(unpacked, int) or not 0 < unpacked <= 10 * 1024**3:
        raise ValueError("Invalid unpacked size")
    return {**document, **installer, "notes": str(release.get("body") or "")[:12000],
            "release_url": PAGE + "/tag/" + urllib.parse.quote(release["tag_name"], safe="")}


def download(update, directory, progress=lambda *_: None, cancelled=lambda: False, opener=open_url):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = update["name"]
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Unsafe download filename")
    final = directory / name
    partial = directory / (name + ".part")
    for path in (final, partial):
        if path.is_symlink():
            raise ValueError("Linked update cache is not supported")
    if final.exists():
        if final.stat().st_size == update["size"] and digest(final) == update["sha256"]:
            return final
        raise ValueError("Existing installer checksum mismatch; clear this download in the update dialog")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > update["size"]:
        raise ValueError("Oversized partial download")
    if offset < update["size"]:
        with opener(update["url"], {"Range": f"bytes={offset}-"} if offset else {}) as response:
            status = response.status
            if status == 206:
                content_range = response.headers.get("Content-Range", "")
                expected = f"bytes {offset}-{update['size'] - 1}/{update['size']}"
                if content_range != expected:
                    raise ValueError("Invalid resumed download range")
            elif status == 200:
                offset = 0  # A server may ignore Range; restart, never append.
            else:
                raise ValueError("Unexpected download status")
            with partial.open("ab" if offset else "wb") as stream:
                while True:
                    if cancelled():
                        raise InterruptedError("下载已暂停，下次检查可续传")
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    offset += len(block)
                    if offset > update["size"]:
                        raise ValueError("Download exceeds expected size")
                    stream.write(block)
                    progress(offset, update["size"])
                stream.flush()
                os.fsync(stream.fileno())
    if partial.stat().st_size != update["size"]:
        raise ValueError("下载不完整或校验失败；未运行安装器")
    if digest(partial) != update["sha256"]:
        # A complete but corrupt partial cannot be resumed. Remove only this
        # failed download so the mandatory startup dialog's Retry can refetch.
        partial.unlink()
        raise ValueError("下载校验失败，已清除损坏下载；请重试。未运行安装器")
    partial.replace(final)
    progress(update["size"], update["size"])
    return final
