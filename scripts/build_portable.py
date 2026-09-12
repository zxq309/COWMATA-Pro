"""Assemble verified local runtime/vendor inputs without touching user data.

Run using the development Python. End users only extract the resulting folder.
This builder never fetches packages, mutates a system environment or overwrites
an existing distribution. Embedded dependencies are pinned in requirements-portable.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def write_checksum(archive, digest):
    # Distribution names may contain Chinese characters on Windows.
    with archive.with_suffix(".zip.sha256").open("x", encoding="utf-8") as stream:
        stream.write(digest + "  " + archive.name + "\n")


def portable_ignore(directory, names):
    """Exclude only audited unused developer/browser tools, not media codecs.

    The app uses Qt Widgets/SVG, VLC and FFmpeg/ffprobe, never Qt WebEngine or
    ffplay. Keep Qt platform/image/style plugins and both private runtimes.
    """
    path = Path(directory)
    ignored = {name for name in names if name == "__pycache__" or name.endswith(".pyc")
               or name in {".pytest_cache", ".ruff_cache"}}
    if "site-packages" in path.parts and "PySide6" in path.parts:
        ignored.update(name for name in names if (
            "webengine" in name.lower() or name in {"include", "typesystems", "glue", "doc", "examples"}
            or name.startswith("objects-") or name.endswith((".lib", ".exp", ".pdb"))))
    if path.name == "bin" and path.parent.name == "ffmpeg":
        ignored.add("ffplay.exe")
    if any(p in {"runtime", "model_runtime_20260906"} for p in path.parts):
        ignored.add("tests")
    return ignored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--components", type=Path, help="Existing matching offline package supplying private runtimes and model weights")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    components = args.components.resolve() if args.components else source

    def input_path(relative):
        candidate = source / relative
        return candidate if candidate.exists() else components / relative
    destination = Path(args.out).resolve()
    if destination.exists():
        raise SystemExit("Output already exists; choose a fresh distribution directory.")
    archive = destination.with_name(destination.name + ".zip")
    if not args.no_zip and (archive.exists() or archive.with_suffix(".zip.sha256").exists()):
        raise SystemExit("Archive already exists; choose a fresh distribution name.")
    required = ["COWMATA.exe", "runtime/python.exe", "runtime/pythonw.exe", "runtime/Lib/site-packages/PySide6/QtWidgets.pyd",
                "runtime/Lib/site-packages/rapidocr/__init__.py",
                "assets/ocr/ppocrv6_medium/models.json",
                "assets/ocr/ppocrv6_medium/PP-OCRv6_det_medium.onnx",
                "assets/ocr/ppocrv6_medium/PP-OCRv6_rec_medium.onnx",
                "assets/ocr/ppocrv6_medium/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                "runtime/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx",
                "vendor/vlc/libvlc.dll", "vendor/ffmpeg/bin/ffmpeg.exe", "vendor/ffmpeg/bin/ffprobe.exe"]
    required += ["model_runtime_20260906/python.exe", "assets/event_models/20260906/pack.json"]
    for relative in required:
        if not input_path(relative).is_file():
            raise SystemExit("Missing portable input: " + relative)
    model_root = source / "assets" / "ocr" / "ppocrv6_medium"
    registry = json.loads((model_root / "models.json").read_text(encoding="utf-8"))
    for item in registry["files"].values():
        with input_path("assets/ocr/ppocrv6_medium/" + item["name"]).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise SystemExit("Portable model hash mismatch: " + item["name"])
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("cowmata_tailring", "runtime", "model_runtime_20260906", "vendor", "assets"):
        # Exclude upstream test corpora and C++ build objects, not runtime DLLs
        # or our reviewed event algorithms. This also avoids NSIS/MAX_PATH
        # failures on deeply nested sklearn test fixtures and Qt object files.
        shutil.copytree(components / name if name == "assets" else input_path(name), destination / name,
                        ignore=portable_ignore)
        if name == "assets" and components != source:
            shutil.copytree(source / name, destination / name, dirs_exist_ok=True, ignore=portable_ignore)
    for name in ("COWMATA.exe", "START_ANNOTATOR.bat", "portable_start.py", "使用说明.txt", "CHANGELOG.md", "LICENSE", "NOTICE", "requirements-portable.txt", "requirements-events-20260906.txt"):
        shutil.copy2(input_path(name), destination / name)
    (destination / "docs").mkdir()
    for name in ("workspace-acceptance.md", "portable-components.md", "algorithm-phase1-acceptance.md", "ui-next-stage-proposal.md", "ui-phase2-acceptance.md", "ui-performance-acceptance.md", "annotation-history-acceptance.md", "event-models-acceptance.md", "windows-distribution.md", "ocr-lightweight-integration.md", "live-demos.md"):
        shutil.copy2(input_path("docs/" + name), destination / "docs" / name)
    (destination / "scripts").mkdir()
    for name in ("capture-timing.md", "client-updates.md", "evidence-archive.md", "on-demand-indexing.md", "team-returns.md", "native-video-timing.md", "playback-performance-312.md", "algorithm-inspection.md", "release-320-validation.md", "release-321-validation.md", "release-321.md", "data-organization.md", "release-330.md", "release-330-validation.md", "code-audit-330.md", "quick-start-illustrated.pdf", "quick-start-illustrated.md", "quick-start-illustrated.source.json", "quick-start-illustrated.manifest.json", "manual-330-validation.md", "manual-330-capture-evidence.json"):
        shutil.copy2(input_path("docs/" + name), destination / "docs" / name)
    for name in ("portable_self_test.py", "build_portable.py", "verify_label_history.py", "verify_event_models.py", "verify_candidate_ui.py", "register_event_pack.py", "verify_evidence_archive.py", "train_mother_dataset.py"):
        shutil.copy2(source / "scripts" / name, destination / "scripts" / name)
    for name in ("release-331.md", "release-331-validation.md", "playback-annotation-fix-20260910.md", "manual-331-validation.md", "manual-331-capture-evidence.json", "release-332.md", "release-332-validation.md", "manual-332-validation.md", "release-340.md", "release-341.md", "release-341-validation.md"):
        shutil.copy2(source / "docs" / name, destination / "docs" / name)
    for name in ("release-342.md", "release-342-validation.md", "legacy-dataset-workflow.md"):
        shutil.copy2(source / "docs" / name, destination / "docs" / name)
    inventory = []
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            inventory.append({"path": path.relative_to(destination).as_posix(), "size": path.stat().st_size, "sha256": digest})
    (destination / "package-manifest.json").write_text(json.dumps({"files": inventory}, indent=2), encoding="utf-8")
    print(json.dumps({"directory": str(destination), "files": len(inventory), "bytes": sum(x["size"] for x in inventory)}), flush=True)
    if not args.no_zip:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as bundle:
            for path in sorted(destination.rglob("*")):
                if path.is_file():
                    bundle.write(path, str(Path(destination.name) / path.relative_to(destination)))
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        write_checksum(archive, digest)
        print(json.dumps({"zip": str(archive), "bytes": archive.stat().st_size, "sha256": digest}), flush=True)


if __name__ == "__main__":
    main()
