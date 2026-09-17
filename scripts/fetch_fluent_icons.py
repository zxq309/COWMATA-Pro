"""Developer-only fetch of a tiny pinned MIT-licensed icon subset. No runtime downloads."""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

REVISION = "5bae3fb7771054c252a54b1d9210e9c03439fa1b"
ROOT = Path(__file__).resolve().parents[1] / "assets" / "fluent"
ICONS = ("Folder Open", "Panel Left", "Pin", "Text Bullet List", "Save")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    base = f"https://raw.githubusercontent.com/microsoft/fluentui-system-icons/{REVISION}/"
    manifest = {"project": "https://github.com/microsoft/fluentui-system-icons", "revision": REVISION, "license": "MIT", "files": {}}
    for name in ICONS:
        stem = name.lower().replace(" ", "_")
        upstream = f"assets/{name}/SVG/ic_fluent_{stem}_20_regular.svg"
        with urllib.request.urlopen(base + urllib.parse.quote(upstream), timeout=30) as response:
            data = response.read()
        if b"<svg" not in data or len(data) > 100_000:
            raise ValueError(upstream)
        (ROOT / (stem + ".svg")).write_bytes(data)
        manifest["files"][stem + ".svg"] = {"path": upstream, "sha256": hashlib.sha256(data).hexdigest()}
    with urllib.request.urlopen(base + "LICENSE", timeout=30) as response:
        (ROOT / "LICENSE.txt").write_bytes(response.read())
    (ROOT / "provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
