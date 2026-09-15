"""GitHub-only release transport. Standard library; usable by detached updater.

Trust: HTTPS to the fixed public repository plus GitHub's SHA-256 asset digest.
This is NOT an Authenticode signature and does not bypass Windows SmartScreen.
No account token, sensor data, or local paths are sent to GitHub.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "zxq309/COWMATA-Pro"
REPOSITORY_ID = 1318095307
REPO_ALIASES = (REPO, "zxq309/cattle-tail-ring-annotator")
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
    path = urllib.parse.unquote(value.path)
    if '\\' in path or any(part in {'.', '..'} for part in path.split('/')):
        raise ValueError("Update URL outside the configured repository")
    api_roots = ["/repos/" + repo + "/releases" for repo in REPO_ALIASES]
    api_roots.append(f"/repositories/{REPOSITORY_ID}/releases")
    direct = ((host == "api.github.com" and any(path == root or path.startswith(root + '/') for root in api_roots))
              or (host == "github.com" and any(path in ('/'+repo+'/releases', '/'+repo+'/releases.atom') or any(path.startswith('/'+repo+'/releases/'+section+'/') for section in ('download','tag','expanded_assets')) for repo in REPO_ALIASES)))
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
    transport = urllib.request.build_opener(_Redirect())
    try:
        return transport.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code != 403 or "/releases/download/" not in urllib.parse.urlsplit(url).path:
            raise
        # A cached GitHub redirect can contain an expired, signed asset URL.
        # Refresh through the same official release URL; never change trust hosts.
        split = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(split.query) + [("cowmata_refresh", str(time.time_ns()))]
        fresh = urllib.parse.urlunsplit(split._replace(query=urllib.parse.urlencode(query)))
        retry = urllib.request.Request(fresh, headers={**dict(request.header_items()), "Cache-Control":"no-cache"})
        return transport.open(retry, timeout=30)


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


def check_update(current, channel="preview", opener=open_url, package_kind="portable"):
    if package_kind not in {"portable", "installer"}:
        raise ValueError("Invalid update package preference")
    if channel not in {"stable", "preview"}:
        raise ValueError("Invalid update channel")
    try:
        releases = json.loads(read_bytes(API + "/releases?per_page=100", 4 * 1024**2, opener))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code not in {403, 429, 500, 502, 503, 504}:
            raise
        return public_releases(current, channel, opener, package_kind)
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
    release = max(newer, key=lambda pair: pair[0])[1]
    assets = {item["name"]: item for item in release.get("assets", [])}
    if package_kind == "installer":
        for brand in ("Pro", "Annotator"):
            name = f"COWMATA-{brand}-{release['tag_name'].removeprefix('v')}-Setup.exe"
            if name in assets:
                return installer_info(release["tag_name"], assets[name], str(release.get("body") or ""))
    portable_names = [f"COWMATA-{brand}-{release['tag_name'].removeprefix('v')}-Portable.zip" for brand in ("Pro", "Annotator")]
    for name in portable_names:
        if name in assets:
            return portable_info(release["tag_name"], assets[name], str(release.get("body") or ""))
    if DESCRIPTOR in assets:
        entry = asset_info(assets[DESCRIPTOR])
        content = read_bytes(entry["url"], 65536, opener)
        if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Update descriptor checksum mismatch")
    else:
        # Release body comes from the same fixed HTTPS repository API. Keep
        # package integrity metadata without adding a fourth download asset.
        body = str(release.get('body') or '')
        matches = re.findall(r'<!-- cowmata-update\s+(\{.*?\})\s*-->', body, re.DOTALL)
        if len(matches) != 1 or len(matches[0].encode('utf-8')) > 65536:
            raise ValueError("新版本尚未上传自动更新清单，请稍后重试或查看发布页")
        content = matches[0]
    document = json.loads(content)
    if document.get("schema") != 1 or document.get("product") != "cowmata-annotator":
        raise ValueError("Unsupported update protocol or product")
    if version_key(document["version"]) != version_key(release["tag_name"]):
        raise ValueError("Release and installer versions disagree")
    installer = asset_info(assets[document["installer"]])
    if not re.fullmatch(r"COWMATA-(?:Pro|Annotator)-[A-Za-z0-9.-]+-Setup.exe", installer["name"]):
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


def _download_once(update, directory, progress=lambda *_: None, cancelled=lambda: False, opener=open_url):
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

def portable_info(version, asset, notes=""):
    item = asset_info(asset)
    if item["name"] != f"COWMATA-Pro-{version.removeprefix('v')}-Portable.zip" and item["name"] != f"COWMATA-Annotator-{version.removeprefix('v')}-Portable.zip":
        raise ValueError("Unexpected portable package name")
    return dict(schema=2, product="cowmata-annotator", version=version.removeprefix("v"),
                kind="portable_zip", **item, notes=notes[:12000],
                release_url=PAGE + "/tag/" + urllib.parse.quote(version if version.startswith("v") else "v"+version, safe=""))

def public_releases(current, channel, opener, package_kind="portable"):
    """Use GitHub's official public feed and per-asset SHA-256 when REST is limited."""
    import html
    import xml.etree.ElementTree as ET
    feed = ET.fromstring(read_bytes(PAGE + ".atom", 4 * 1024**2, opener))
    ns = {"a":"http://www.w3.org/2005/Atom"}
    candidates = []
    for entry in feed.findall("a:entry", ns):
        link = next((x.get("href", "") for x in entry.findall("a:link", ns) if x.get("rel") == "alternate"), "")
        try:
            valid_url(link)
            tag = urllib.parse.unquote(urllib.parse.urlsplit(link).path.rsplit("/", 1)[-1])
            key = version_key(tag)
        except ValueError:
            continue
        if key <= version_key(current) or channel == "stable" and key[3] != 3:
            continue
        candidates.append((key, tag, entry.findtext("a:content", "", ns)))
    for _, tag, body in sorted(candidates, reverse=True):
        page = read_bytes(PAGE + "/tag/" + urllib.parse.quote(tag, safe=""), 4*1024**2, opener).decode("utf-8")
        # Stable-looking tags can still be marked pre-release by their publisher.
        if channel == "stable" and re.search(r">\s*Pre-release\s*<", page, re.I):
            continue
        listing = read_bytes(PAGE + "/expanded_assets/" + urllib.parse.quote(tag, safe=""), 2*1024**2, opener).decode("utf-8")
        suffix = "Setup.exe" if package_kind == "installer" and f"-{tag.removeprefix('v')}-Setup.exe" in listing else "Portable.zip"
        expected = {f"COWMATA-{brand}-{tag.removeprefix('v')}-{suffix}" for brand in ("Pro","Annotator")}
        found = []
        for row in re.findall(r"<li\b[^>]*>(.*?)</li>", listing, re.S):
            links = re.findall(r'<a\b[^>]*href="([^"]+)"', row)
            for value in links:
                url = urllib.parse.urljoin("https://github.com", html.unescape(value))
                name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/",1)[-1])
                if name not in expected:
                    continue
                valid_url(url)
                if urllib.parse.unquote(urllib.parse.urlsplit(url).path).split("/")[-2] != tag:
                    raise ValueError("Public release asset tag mismatch")
                hashes = set(re.findall(r"\bsha256:([0-9a-f]{64})\b", row))
                if len(hashes) != 1:
                    raise ValueError("Official release asset SHA-256 is missing or ambiguous")
                # Displayed sizes are rounded. Obtain exact bytes from the official asset response.
                with opener(url, {"Range":"bytes=0-0"}) as response:
                    if response.status == 206:
                        match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range",""))
                        if not match:
                            raise ValueError("Invalid public asset length response")
                        size = int(match[1])
                    elif response.status == 200:
                        size = int(response.headers.get("Content-Length", "0"))
                    else:
                        raise ValueError("Unexpected public asset response")
                found.append(dict(name=name,size=size,digest="sha256:"+hashes.pop(),state="uploaded",browser_download_url=url))
        if len(found) != 1:
            raise ValueError("最新版尚未提供可校验的完整便携 ZIP，请查看发布附件")
        notes = html.unescape(re.sub(r"<[^>]+>", " ", body))
        result = (installer_info if suffix == "Setup.exe" else portable_info)(tag, found[0], notes)
        result["metadata_source"] = "github_public_release"
        return result
    return None


def download(update, directory, progress=lambda *_: None, cancelled=lambda: False, opener=open_url):
    """Resume transient transport failures with a bounded retry budget."""
    for attempt in range(3):
        if cancelled():
            raise InterruptedError("下载已暂停，下次可续传")
        try:
            return _download_once(update, directory, progress, cancelled, opener)
        except (urllib.error.URLError, http.client.IncompleteRead, ConnectionError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in {403, 408, 429, 500, 502, 503, 504}:
                raise
            if attempt == 2:
                raise
            deadline = time.monotonic() + attempt + 1
            while time.monotonic() < deadline:
                if cancelled():
                    raise InterruptedError("下载已暂停，下次可续传") from exc
                time.sleep(.1)


def installer_info(version, asset, notes=""):
    item = asset_info(asset)
    if item["name"] not in {f"COWMATA-{brand}-{version.removeprefix('v')}-Setup.exe" for brand in ("Pro", "Annotator")}:
        raise ValueError("Unexpected installer package name")
    return dict(schema=2, product="cowmata-annotator", version=version.removeprefix("v"), kind="installer_exe",
                **item, notes=notes[:12000], unpacked_size=min(10*1024**3,max(2*1024**3,item["size"]*6)),
                release_url=PAGE+"/tag/"+urllib.parse.quote(version if version.startswith("v") else "v"+version,safe=""))
