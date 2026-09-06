"""Maintainer-only model fetch. The desktop app never downloads anything."""
import hashlib
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "assets" / "ocr" / "ppocrv6_medium"


def fetch(item):
    target = ROOT / item["name"]
    if target.exists():
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Existing model has a wrong hash; preserved: " + str(target))
        return {"name": target.name, "reused": True}
    temporary = target.with_suffix(".onnx.download")
    # Exclusive creation preserves an interrupted download for inspection.
    with urllib.request.urlopen(item["url"], timeout=60) as response, temporary.open("xb") as output:
        digest = hashlib.sha256()
        while block := response.read(1024 * 1024):
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != item["sha256"]:
        raise ValueError("Downloaded model failed SHA-256 validation: " + str(temporary))
    temporary.rename(target)
    return {"name": target.name, "bytes": target.stat().st_size, "sha256": digest.hexdigest()}


if __name__ == "__main__":
    registry = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(fetch, registry["files"].values()):
            print(json.dumps(result), flush=True)
