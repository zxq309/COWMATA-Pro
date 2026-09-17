"""Transfer byte-identical, verified binaries from public bases and a small data patch."""
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
        return h.hexdigest()


def api(endpoint, *, method="GET"):
    data = subprocess.check_output(["gh", "api", "--method", method, endpoint])
    return json.loads(data) if data else None


def download(repo, asset, target):
    with target.open("xb") as stream:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                        f"repos/{repo}/releases/assets/{asset['id']}"], stdout=stream, check=True)


def reconstruct(base, blob, output, operations, expected_size):
    limits = [base.stat().st_size, blob.stat().st_size]
    written = 0
    with base.open("rb") as old, blob.open("rb") as changes, output.open("xb") as target:
        for source, offset, length in operations:
            if (source not in (0, 1) or not all(isinstance(v, int) for v in (offset, length)) or
                    offset < 0 or length < 0 or offset + length > limits[source] or
                    written + length > expected_size):
                raise ValueError("Patch copy range is invalid")
            stream = (old, changes)[source]
            stream.seek(offset)
            remaining = length
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("Patch source ended early")
                target.write(block)
                remaining -= len(block)
            written += length
    if written != expected_size:
        raise ValueError("Patch output size mismatch")


def main():
    release_id, patch_id, patch_sha = sys.argv[1:]
    if not release_id.isdecimal() or not patch_id.isdecimal() or not re.fullmatch(r"[0-9a-f]{64}", patch_sha):
        raise ValueError("Invalid transfer identifiers")
    repo = os.environ["GH_REPO"]
    endpoint = f"repos/{repo}/releases/{release_id}"
    release = api(endpoint)
    if not release["draft"]:
        raise ValueError("Refusing to change a published release")
    patch = next(a for a in release["assets"] if str(a["id"]) == patch_id)
    download(repo, patch, Path("release-patch.zip"))
    if digest(Path("release-patch.zip")) != patch_sha:
        raise ValueError("Patch checksum mismatch")
    with zipfile.ZipFile("release-patch.zip") as bundle:
        if sum(i.file_size for i in bundle.infolist()) > 256 * 1024**2:
            raise ValueError("Patch exceeds the bounded transfer size")
        manifest = json.loads(bundle.read("manifest.json"))
        tag = manifest["tag"]
        if (not re.fullmatch(r"v[0-9]+[.][0-9]+[.][0-9]+", tag) or release["tag_name"] != tag or
                release["target_commitish"] != manifest["commit"] or
                not re.fullmatch(r"v[0-9]+[.][0-9]+[.][0-9]+", manifest["base_tag"])):
            raise ValueError("Release differs from the tested patch target")
        allowed = {f"COWMATA-Pro-{tag[1:]}-Setup.exe"}
        if {f["name"] for f in manifest["files"]} != allowed or len(manifest["files"]) != 1:
            raise ValueError("Unexpected target assets")
        base_release = api(f"repos/{repo}/releases/tags/{manifest['base_tag']}")
        if base_release["draft"]:
            raise ValueError("The base must be a published release")
        for index, spec in enumerate(manifest["files"]):
            if not 0 < spec["size"] <= 2 * 1024**3:
                raise ValueError("Invalid target size")
            base_asset = next(a for a in base_release["assets"] if a["name"] == spec["base_name"])
            base, blob, target = Path(f"base-{index}"), Path(f"literal-{index}"), Path(spec["name"])
            download(repo, base_asset, base)
            if digest(base) != spec["base_sha256"]:
                raise ValueError("Base checksum mismatch")
            blob.write_bytes(bundle.read(spec["blob"]))
            reconstruct(base, blob, target, spec["operations"], spec["size"])
            if digest(target) != spec["sha256"]:
                raise ValueError("Reconstructed asset differs from the locally tested file")
            print("EXACT_BYTES_VERIFIED", spec["name"], spec["sha256"], flush=True)
            current = api(endpoint)
            if not current["draft"]:
                raise ValueError("Release was published during transfer")
            for asset in current["assets"]:
                if asset["name"] == spec["name"]:
                    api(f"repos/{repo}/releases/assets/{asset['id']}", method="DELETE")
            subprocess.run(["gh", "release", "upload", tag, str(target), "--repo", repo], check=True)
            uploaded = next(a for a in api(endpoint)["assets"] if a["name"] == spec["name"])
            if uploaded.get("digest") != "sha256:" + spec["sha256"] or uploaded["size"] != spec["size"]:
                raise ValueError("Server checksum mismatch")
            print("REMOTE_VERIFIED", spec["name"], flush=True)
    api(f"repos/{repo}/releases/assets/{patch_id}", method="DELETE")
    print("TRANSFER_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
