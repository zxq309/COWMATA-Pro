r"""努责算法命令行：train（训练/增量精调）、detect（识别）、patterns（仅重算规律）。

示例：
  python scripts/straining_algorithm.py train  --dataset "F:\科牧特_数据集\COWMATA_Behavior_Dataset" --out "F:\科牧特_模型\努责\<版本>"
  python scripts/straining_algorithm.py train  ... --previous "F:\科牧特_模型\努责\<上一版本>"
  python scripts/straining_algorithm.py detect --bundle <版本目录> --raw <原始 motion JSON 或目录> --out 结果.csv
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


_BUNDLE = {}


def _detect_one(args):
    from cowmata_tailring.algorithms.straining import detect_file, load_bundle

    bundle_path, raw = args
    if bundle_path not in _BUNDLE:
        _BUNDLE[bundle_path] = load_bundle(bundle_path)
    try:
        return raw, detect_file(_BUNDLE[bundle_path], raw), None
    except (ValueError, KeyError, OSError) as exc:
        return raw, [], str(exc)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--dataset", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--cache")
    t.add_argument("--previous")
    t.add_argument("--max-fa-per-hour", type=float, default=0.5)
    t.add_argument("--workers", type=int, default=16)
    d = sub.add_parser("detect")
    d.add_argument("--bundle", required=True)
    d.add_argument("--raw", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--workers", type=int, default=16)
    a = ap.parse_args(argv)
    if a.cmd == "train":
        from cowmata_tailring.algorithms.straining.train import train_bundle

        train_bundle(
            a.dataset,
            a.out,
            cache=a.cache,
            previous=a.previous,
            max_fa_per_hour=a.max_fa_per_hour,
            workers=a.workers,
        )
    else:
        from concurrent.futures import ProcessPoolExecutor

        raw = Path(a.raw)
        files = (
            [raw]
            if raw.is_file()
            else sorted(p for p in raw.rglob("*.json") if "Label" not in p.parts)
        )
        rows = []
        # Each worker reads and decodes its own records: N-way parallel disk reads, not a queue behind one reader.
        with ProcessPoolExecutor(max_workers=max(1, min(a.workers, len(files)))) as pool:
            for f, events, error in pool.map(
                _detect_one, [(a.bundle, str(f)) for f in files], chunksize=1
            ):
                if error:
                    print("跳过", f, error)
                rows += [dict(source=f, **e) for e in events]
        fields = [
            "source",
            "code",
            "start_ms",
            "end_ms",
            "start_epoch_ms",
            "end_epoch_ms",
            "confidence",
            "contraction_pulses",
            "score",
            "algorithm_version",
            "requires_review",
        ]
        with open(a.out, "w", encoding="utf-8-sig", newline="") as s:
            w = csv.DictWriter(s, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"{len(files)} 份记录，{len(rows)} 段努责候选 -> {a.out}")


if __name__ == "__main__":
    main()
