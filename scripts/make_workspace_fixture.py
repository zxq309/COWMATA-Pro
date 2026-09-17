"""Build isolated test projects. Original data is copied, never linked/modified."""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.media.timeline import MediaTimelineIndex, probe_media_timeline
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.clocks import wall_ms
from cowmata_tailring.workspace.probe import SourceInspector, build_observed_intervals
from cowmata_tailring.workspace.storage import atomic_json
from cowmata_tailring.workspace.work import SessionWork


def build(args):
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    originals = Path(args.sources).resolve()
    json_source = originals / "2026-08-03 12_44_58.json"
    imu_dir = output / "九轴数据" / "546C50CA07D5"
    imu_dir.mkdir(parents=True)
    shutil.copy2(json_source, imu_dir / json_source.name)
    metadata_by_hash = {}
    if args.synthetic:
        ffmpeg, ffprobe = find_ffmpeg()
        colors = ["blue", "red", "green", "purple", "navy", "brown", "teal", "gray"]
        base = wall_ms("2026-08-03 12:00:00")
        for camera in range(8):
            folder = output / f"视角{camera + 1}" / "批次1"
            folder.mkdir(parents=True)
            split = 8 + camera
            for part, (start, length) in enumerate(((0, split), (split, 30 - split))):
                path = folder / f"{part + 1:03d}.mp4"
                corner = camera % 4
                x = "10" if corner in (0, 2) else "w-tw-10"
                y = "10" if corner < 2 else "h-th-10"
                timestamp = f"2026-08-03 %{{pts\\:hms\\:{43200 + start}}}"
                filter_ = f"drawtext=fontfile='C\\:/Windows/Fonts/consola.ttf':text='{timestamp}':x={x}:y={y}:fontsize=22:fontcolor=white:box=1:boxcolor=black@1,drawtext=fontfile='C\\:/Windows/Fonts/consola.ttf':text='VIEW {camera + 1} / PART {part + 1}':x=20:y=h/2:fontsize=28:fontcolor=yellow"
                command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
                           "-f", "lavfi", "-i", f"sine=frequency={300 + camera * 100}:sample_rate=48000",
                           "-vf", filter_, "-t", str(length), "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                           "-c:a", "aac", "-b:a", "64k", "-shortest", str(path)]
                subprocess.run(command, check=True, timeout=60, creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
                timeline = probe_media_timeline(path, ffprobe)
                metadata_by_hash[digest_file(path)] = {"camera": f"视角{camera + 1}", "duration_ms": timeline.duration_ms,
                    "width": 640, "height": 360, "codec": "h264", "timeline": timeline.to_dict(), "warnings": [],
                    "needs_review": False, "fixture": True,
                    "intervals": [{"wall_start": base + start * 1000, "wall_end": base + (start + length) * 1000,
                                   "media_start": 0, "media_end": length * 1000, "verified": True, "warnings": []}]}
                print(str(path), flush=True)
    else:
        evidence = Path(args.evidence).resolve()
        hashes = {Path(r["file"]).name: r["asset_id"] for r in json.loads((evidence / "summary.json").read_text(encoding="utf-8"))}
        base = wall_ms("2026-08-03 11:40:00")
        for path in sorted(originals.glob("hiv*.mp4")):
            asset_id = digest_file(path)
            if hashes[path.name] != asset_id:
                raise ValueError("The original changed since the integration probe")
            folder = output / ("海康视角2" if path.stem == "hiv00001" else "海康视角1") / "批次1"
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, folder / path.name)
            metadata = json.loads((evidence / (path.stem + ".probe.json")).read_text(encoding="utf-8"))
            metadata["camera"] = folder.parent.name
            metadata["intervals"], metadata["warnings"] = build_observed_intervals(metadata["samples"], MediaTimelineIndex.from_dict(metadata["timeline"]))
            metadata["needs_review"] = bool(metadata["warnings"])
            metadata_by_hash[asset_id] = metadata
    catalog = Catalog(output, stability_seconds=0)
    try:
        catalog.scan()
        inspector = SourceInspector(output, catalog.meta)

        def inspect(path, kind, asset_id):
            if kind == "imu":
                return inspector(path, kind, asset_id)
            return copy.deepcopy(metadata_by_hash[asset_id])

        for row in catalog.pending(retry_seconds=0):
            result = catalog.index_one(row["path"], inspect)
            if row["kind"] == "imu":
                work = SessionWork(result["asset_id"])
                work.project.cow_id = "TEST-ONLY" if args.synthetic else ""
                work.progress = {"reference_ms": base, "imu_ms": 0}
                atomic_json(catalog.work_path(result["asset_id"]), work.to_dict())
        catalog.save_settings({"reference_ms": base, "fixture": True,
                               "selected_cameras": [f"视角{i + 1}" for i in range(8)] if args.synthetic else ["海康视角1", "海康视角2"]})
    finally:
        catalog.close()
    print(json.dumps({"project": str(output), "created_at": time.time(), "synthetic": args.synthetic}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--sources", required=True)
    parser.add_argument("--evidence")
    parser.add_argument("--synthetic", action="store_true")
    build(parser.parse_args())
