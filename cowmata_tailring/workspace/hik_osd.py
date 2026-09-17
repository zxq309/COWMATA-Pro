"""Local surveillance OSD recognition. Default scan is a preview, with no renaming.

Calibrated layouts: 2560x1440 without weekday, and 1280x720 with weekday.
All dates and times come from image OCR; no timestamp values are hard-coded.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

FORMAT = "%Y-%m-%d %H_%M_%S"
SAMPLE_TARGETS = (0.0, 0.24, 1.0, 2.0, 3.0)
PROFILES = {
    (2560, 1440): {"name": "hd_no_weekday", "time_x": 544, "cell": 32, "top": 80},
    (1280, 720): {"name": "sd_with_weekday", "time_x": 384, "cell": 16, "top": 40},
}


def write_json(path, data):
    # Reports are created in a new output directory, never over an existing run.
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def crop(img, box):
    return img.crop(tuple(round(x) for x in box))


def recognize(engine, arr, size=None):
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
    if size:
        arr = cv2.resize(arr, size, interpolation=cv2.INTER_NEAREST)
    arr = cv2.copyMakeBorder(arr, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    # Passing PIL explicitly avoids an RGB/BGR ambiguity in the OCR loader.
    result, _ = engine(Image.fromarray(arr), use_det=False, use_cls=False)
    if not result:
        return "", 0.0
    return unicodedata.normalize("NFKC", result[0][0]).strip(), float(result[0][1])


def recognize_date(engine, img):
    w, h = img.size
    arr = np.array(crop(img, (.017*w, .055*h, .194*w, .098*h)))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    passes = []
    for name, variant in (("raw", arr), ("gray", gray), ("invert", 255-gray),
                          ("black20", np.where(gray < 20, 0, 255).astype(np.uint8)),
                          ("white235", np.where(gray > 235, 0, 255).astype(np.uint8)),
                          ("white245", np.where(gray > 245, 0, 255).astype(np.uint8))):
        text, score = recognize(engine, variant, (arr.shape[1]*3, arr.shape[0]*3))
        match = re.fullmatch(r"\D*(20\d{2})\D+(\d{2})\D+(\d{2})\D*", text)
        value = None
        if match:
            try:
                value = datetime(*map(int, match.groups())).strftime("%Y-%m-%d")
            except ValueError:
                pass
        passes.append({"variant": name, "text": text, "score": score, "date": value})
    votes = Counter(row["date"] for row in passes if row["date"] and row["score"] >= .90)
    best = votes.most_common(1)
    accepted = best[0][0] if best and best[0][1] >= 2 else None
    if len(votes) > 1:
        accepted = None
    return accepted, passes


def recognize_time(engine, img, profile):
    rows = []
    selected_images = []
    for digit_index, position in enumerate((0, 1, 3, 4, 6, 7)):
        step, top = profile["cell"], profile["top"]
        left = profile["time_x"] + position*step
        candidates = defaultdict(list)
        passes = []
        variant_images = {}
        for geometry in ("full", "tight"):
            if geometry == "full":
                box = (left, top, left+step, top+step*2)
                size = (96, 192)
            else:
                box = (left+step/8, top+step/4, left+step-step/8, top+step*1.5)
                size = (96, 160)
            arr = np.array(crop(img, box))
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            variants = {"raw": arr}
            for threshold in (4, 10, 20):
                variants[f"black{threshold}"] = np.where(gray < threshold, 0, 255).astype(np.uint8)
            for threshold in (245, 235):
                variants[f"white{threshold}"] = np.where(gray > threshold, 0, 255).astype(np.uint8)
            for name, variant in variants.items():
                text, score = recognize(engine, variant, size)
                key = geometry + "_" + name
                row = {"variant": key, "text": text, "score": score}
                passes.append(row)
                # Do not silently substitute lookalike letters or incomplete digits.
                if re.fullmatch(r"[0-9]", text):
                    candidates[text].append(row)
                    variant_images[key] = variant
        allowed = "012" if digit_index == 0 else "012345" if digit_index in (2, 4) else "0123456789"
        if digit_index == 1 and rows[0]["digit"] == "2":
            allowed = "0123"
        ranked = sorted(
            ({"digit": digit, "score": max(p["score"] for p in evidence),
              "support": sum(p["score"] >= .85 for p in evidence),
              "variant": max(evidence, key=lambda p: p["score"])["variant"]}
             for digit, evidence in candidates.items() if digit in allowed),
            key=lambda row: row["score"], reverse=True)
        best = ranked[0] if ranked else {"digit": None, "score": 0.0, "variant": None}
        # A single threshold may turn a background line into a confident "1".
        # A competing reading needs repeated support before it vetoes agreement.
        ambiguous = (len(ranked) > 1 and ranked[1]["support"] >= 2
                     and ranked[1]["score"] >= .90 and best["score"]-ranked[1]["score"] < .02)
        accepted = best["score"] >= .85 and not ambiguous
        rows.append({"position": position, **best, "accepted": accepted,
                     "alternatives": ranked[1:], "passes": passes})
        selected_images.append(variant_images.get(best["variant"]))
    value = None
    if all(row["accepted"] for row in rows):
        digits = "".join(row["digit"] for row in rows)
        value = f"{digits[:2]}:{digits[2:4]}:{digits[4:]}"
        try:
            datetime.strptime(value, "%H:%M:%S")
        except ValueError:
            value = None
    return value, rows, selected_images
