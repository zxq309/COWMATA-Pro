from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from datetime import datetime

import cv2
import numpy as np
from PIL import Image

from .clocks import wall_ms

CORNERS = {
    "top_left": (0, 0, .78, .23),
    "top_right": (.22, 0, 1, .23),
    "bottom_left": (0, .77, .78, 1),
    "bottom_right": (.22, .77, 1, 1),
}
STAMP = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?\D{0,12}?(\d{2})\s*[:：_]\s*(\d{2})\s*[:：_]\s*(\d{2})")
TIME_ONLY = re.compile(r"(?<!\d)(\d{2})\s*[:：_]\s*(\d{2})\s*[:：_]\s*(\d{2})(?!\d)")


def parse_stamp(text: str) -> str | None:
    text = unicodedata.normalize("NFKC", text)
    match = STAMP.search(text)
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def valid_roi(roi):
    if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
        raise ValueError("时间戳区域必须是画面内非空矩形")
    return tuple(float(v) for v in roi)


class TimestampOCR:
    def __init__(self, *, backend="v6"):
        if backend == "v6":
            from .rapid_backend import TIMESTAMP_SIGNATURE, RapidV6Adapter
            self.engine = RapidV6Adapter()
            self.signature = TIMESTAMP_SIGNATURE
        elif backend == "legacy":
            # Explicit benchmark baseline only; never silently downgrade a
            # missing or damaged medium model in a user's offline package.
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
            self.signature = "rapidocr-onnxruntime-1.4.4/PP-OCRv4"
        else:
            raise ValueError("Unknown OCR backend: " + backend)

    def routing_read(self, img: Image.Image, *, filename="frame", hint=None, max_passes=None, minimum_votes=1, raw_only=False) -> dict:
        """Bounded single-pass routing, NEVER a verified OCR observation.

        Two nearby routing frames prioritize candidate files. The normal
        multi-vote recognizer must independently verify a candidate before
        publishing intervals. No enhancement or pixel-digit retries here.
        """
        regions = list(CORNERS.items())
        if hint:
            regions.insert(0, ("cached", valid_roi(hint)))
        pixels = np.array(img.convert("RGB"))
        passes = 0
        for name, rect in regions:
            votes = Counter()
            x0, y0, x1, y1 = [round(v * (img.width if i % 2 == 0 else img.height)) for i, v in enumerate(rect)]
            patch = pixels[y0:y1, x0:x1]
            gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
            variants = (patch,) if raw_only else (patch, 255-gray, np.where(gray < 30, 0, 255).astype(np.uint8))
            for variant in variants:
                if max_passes is not None and passes >= max_passes:
                    return {"success": False, "wall_ms": None, "roi": None, "routing_only": True}
                passes += 1
                if variant.ndim == 2:
                    variant = cv2.cvtColor(variant, cv2.COLOR_GRAY2RGB)
                rows, _ = self.engine(Image.fromarray(variant), use_cls=False)
                rows = [r for r in (rows or []) if float(r[2]) >= .8]
                rows.sort(key=lambda r: (round(min(p[1] for p in r[0])/30), min(p[0] for p in r[0])))
                stamp = parse_stamp(" ".join(r[1] for r in rows))
                if not stamp:
                    continue
                votes[stamp] += 1
                if len(votes) != 1 or votes[stamp] < minimum_votes:
                    continue
                time_rows = [r for r in rows if re.search(r"20\d{2}|\d{2}[:：_]\d{2}", r[1])]
                points = [p for r in time_rows for p in r[0]]
                roi = rect
                if points:
                    roi = (max(0, (min(p[0] for p in points)+x0-8)/img.width),
                           max(0, (min(p[1] for p in points)+y0-8)/img.height),
                           min(1, (max(p[0] for p in points)+x0+8)/img.width),
                           min(1, (max(p[1] for p in points)+y0+8)/img.height))
                return {"success": True, "wall_ms": wall_ms(stamp), "timestamp": stamp,
                        "roi": list(roi), "routing_only": True, "filename": filename,
                        "metadata": {"layout": name}}
        return {"success": False, "wall_ms": None, "roi": None, "routing_only": True}

    def native_check(self, img, *, filename="frame", hint=None, family=None):
        """Small independent image check, not a replacement for fallback OCR."""
        if family == "hikvision-hk1":
            from .hik_osd import PROFILES, recognize, recognize_date
            profile = PROFILES.get(img.size)
            if profile:
                day, dates = recognize_date(self.engine, img)
                left, top, step = profile['time_x'], profile['top'], profile['cell']
                patch = np.array(img.crop((left, top, left+8*step, top+2*step)))
                gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
                passes, values = [], []
                for name, pixels in [('raw',gray),('black10',np.where(gray<10,0,255).astype(np.uint8)),
                                     ('black30',np.where(gray<30,0,255).astype(np.uint8)),
                                     ('white235',np.where(gray>235,0,255).astype(np.uint8)),
                                     ('white245',np.where(gray>245,0,255).astype(np.uint8))]:
                    text,score=recognize(self.engine,pixels,(patch.shape[1]*4,patch.shape[0]*4))
                    match=TIME_ONLY.search(text)
                    value=':'.join(match.groups()) if match else None
                    passes.append({'variant':name,'text':text,'score':score,'clock':value})
                    if value and score >= .9:
                        values.append(value)
                counts=Counter(values)
                dates_ok=day and not any(p['date'] and p['score'] >= .75 and p['date'] != day for p in dates)
                if dates_ok and len(counts)==1 and counts.most_common(1)[0][1]>=2:
                    stamp=day+' '+counts.most_common(1)[0][0]
                    return {'success':True,'wall_ms':wall_ms(stamp),'timestamp':stamp,'filename':filename,
                            'roi':[.01,.05,.62 if img.width==1280 else .42,.11],
                            'metadata':{'layout':profile['name']},'date_passes':dates,'clock_passes':passes}
                # A readable date already identifies this calibrated corner;
                # do not replace a failed clock vote with weaker single-pass OCR.
                result = ({'success':False,'wall_ms':None} if day else
                          self.routing_read(img, filename=filename, hint=hint, max_passes=15, minimum_votes=2))
                result.update(date_passes=dates, clock_passes=passes)
                return result
        return self.routing_read(img, filename=filename, hint=hint, max_passes=15, minimum_votes=2)

    def recognize(self, img: Image.Image, *, filename="frame", roi=None, hint=None, profile_hint=None) -> dict:
        report = self._recognize_once(img, filename=filename, roi=roi, hint=hint, profile_hint=profile_hint)
        if report["success"] or report.get("enhancement_conflict"):
            return report
        # Only retry failed inputs; successful ordinary images keep their path.
        pixels = np.array(img.convert("RGB"))
        low, high = int(pixels.min()), int(pixels.max())
        variants = []
        if 32 <= high - low < 180:
            stretched = cv2.normalize(pixels, None, 0, 255, cv2.NORM_MINMAX)
            gray = cv2.cvtColor(stretched, cv2.COLOR_RGB2GRAY)
            variants = [("range_rgb", Image.fromarray(stretched)),
                        ("range_gray", Image.fromarray(gray).convert("RGB"))]
        elif 640 <= img.width < 2560 and abs(img.width / img.height - 16 / 9) < .01:
            text = " ".join(p["text"] for p in report["passes"] if p.get("region") in {"top_left", "cached", "manual"})
            if re.search(r"20\d{2}.*[年月日]", text):
                width = 1280 if "星期" in text else 2560
                if width != img.width:
                    size = (width, round(img.height * width / img.width))
                    variants = [("native_nearest", img.resize(size, Image.Resampling.NEAREST)),
                                ("native_lanczos", img.resize(size, Image.Resampling.LANCZOS))]
        if not variants:
            return report
        retries = []
        for name, enhanced in variants:
            result = self._recognize_once(enhanced, filename=filename, roi=roi, hint=hint, profile_hint=profile_hint)
            retries.append({"variant": name, "report": result})
        report["enhancement_attempts"] = retries
        first, second = (item["report"] for item in retries)
        if first["success"] and second["success"] and first["timestamp"] == second["timestamp"]:
            recovered = copy.deepcopy(first)
            recovered["metadata"].update(width=img.width, height=img.height)
            recovered["confidence"] = "medium"
            recovered["enhancement_attempts"] = retries
            recovered["original_failure"] = report["warnings"]
            recovered["warnings"].append("轻量增强后两次读数一致，仍需多帧推进核查")
            return recovered
        return report

    def _recognize_once(self, img: Image.Image, *, filename="frame", roi=None, hint=None, profile_hint=None) -> dict:
        """Four-edge discovery, independent preprocessing votes and raw evidence.

        A plausible single-frame reading is NOT an assertion of timeline validity.
        Only the separate multi-frame probe can promote an interval to verified.
        """
        report = {"success": False, "filename": str(filename), "extracted_text": "", "confidence": "low",
                  "metadata": {"language_detected": "zh", "text_regions": 0, "has_tables": False,
                               "has_handwriting": False, "width": img.width, "height": img.height,
                               "ocr_engine": self.signature},
                  "warnings": [], "passes": [], "timestamp": None, "wall_ms": None, "roi": None}
        if not roi and profile_hint in {"hd_no_weekday", "sd_with_weekday"}:
            if self._hik_read(img, report, expected=profile_hint):
                return report
            if report.get("enhancement_conflict"):
                return report
        regions = [("manual", valid_roi(roi))] if roi else list(CORNERS.items())
        if hint and not roi:
            regions.insert(0, ("cached", valid_roi(hint)))
        for name, rect in regions:
            patch = np.array(img.convert("RGB").crop(tuple(round(v * (img.width if i % 2 == 0 else img.height)) for i, v in enumerate(rect))))
            gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
            variants = [("raw", patch), ("inverse", 255 - gray),
                        ("dark", np.where(gray < 30, 0, 255).astype(np.uint8))]
            votes = []
            vote_text_heights = []
            best_rect = rect
            for variant_name, pixels in variants:
                if pixels.ndim == 2:
                    pixels = cv2.cvtColor(pixels, cv2.COLOR_GRAY2RGB)
                rows, _ = self.engine(Image.fromarray(pixels), use_cls=False)
                rows = rows or []
                # Text detections are often separate date / weekday / clock boxes.
                ordered = sorted(rows, key=lambda r: (round(min(p[1] for p in r[0]) / 30), min(p[0] for p in r[0])))
                text = " ".join(r[1] for r in ordered)
                stamp = parse_stamp(text)
                score = min((float(r[2]) for r in ordered), default=0.0)
                report["passes"].append({"region": name, "variant": variant_name, "text": text,
                                         "score": score, "timestamp": stamp})
                report["metadata"]["text_regions"] += len(rows)
                if stamp and score >= .78:
                    votes.append(stamp)
                    time_rows = [r for r in ordered if re.search(r"20\d{2}|\d{2}[:：_]\d{2}", r[1])]
                    vote_text_heights.extend(max(p[1] for p in r[0]) - min(p[1] for p in r[0]) for r in time_rows)
                    if time_rows:
                        points = [p for r in time_rows for p in r[0]]
                        left = min(p[0] for p in points) + rect[0] * img.width - 8
                        top = min(p[1] for p in points) + rect[1] * img.height - 8
                        right = max(p[0] for p in points) + rect[0] * img.width + 8
                        bottom = max(p[1] for p in points) + rect[1] * img.height + 8
                        best_rect = (max(0, left / img.width), max(0, top / img.height),
                                     min(1, right / img.width), min(1, bottom / img.height))
                counts = Counter(votes)
                if counts and counts.most_common(1)[0][1] >= 2 and len(counts) == 1:
                    stamp = counts.most_common(1)[0][0]
                    # Correlated raw/inverse votes can agree on a wrong tiny digit.
                    text_heights = vote_text_heights
                    if text_heights and min(text_heights) < 12:
                        enlarged = Image.fromarray(patch).resize((patch.shape[1] * 2, patch.shape[0] * 2), Image.Resampling.LANCZOS)
                        check_rows, _ = self.engine(enlarged, use_cls=False)
                        check_rows = sorted(check_rows or [], key=lambda r: (round(min(p[1] for p in r[0]) / 30), min(p[0] for p in r[0])))
                        check_text = " ".join(r[1] for r in check_rows)
                        check_stamp = parse_stamp(check_text)
                        check_score = min((float(r[2]) for r in check_rows), default=0.0)
                        report["small_text_check"] = {"text": check_text, "timestamp": check_stamp, "score": check_score}
                        if check_stamp and check_score >= .78 and check_stamp != stamp:
                            report["enhancement_conflict"] = True
                            report["warnings"].append("小字放大后读数冲突，请换邻近帧或人工确认")
                            return report
                    report.update(success=True, extracted_text=stamp, timestamp=stamp, wall_ms=wall_ms(stamp),
                                  confidence="high", roi=list(best_rect))
                    report["metadata"]["layout"] = name
                    return report
        # Known Hikvision pixel OSD has background lines passing through digits.
        # This narrowly scoped fallback is layout-validated by subsequent frames.
        if not roi and img.size in {(2560, 1440), (1280, 720)}:
            if self._hik_read(img, report):
                return report
        report["extracted_text"] = "\n".join(p["text"] for p in report["passes"] if p["text"])
        if any(TIME_ONLY.search(p["text"]) for p in report["passes"]):
            report["warnings"].append("存在时间候选但完整日期未通过核验；请框选或人工确认日期，不能自动补日期")
        else:
            report["warnings"].append("未取得一致的完整时间戳，请框选区域、换邻近帧或确认人工读数")
        return report

    def _hik_read(self, img, report, *, expected=None):
        from .hik_osd import PROFILES, recognize, recognize_date, recognize_time
        profile = PROFILES.get(img.size)
        if not profile or expected and profile["name"] != expected:
            return False
        day, date_passes = recognize_date(self.engine, img)
        plausible_dates = {p["date"] for p in date_passes if p["date"] and p["score"] >= .75}
        if day and any(value != day for value in plausible_dates):
            report["hik_evidence"] = {"date_passes": date_passes, "digits": []}
            report["enhancement_conflict"] = True
            report["warnings"].append("日期预处理读数存在冲突，拒绝自动采用多数年份")
            return False
        clock, digits, _ = recognize_time(self.engine, img, profile)
        report["hik_evidence"] = {"date_passes": date_passes, "digits": digits}
        if day and not clock:
            # White OSD on a dark scene can lose the edge of a digit in the
            # narrow cell crop. Independently recognize the complete clock.
            left, top, step = profile["time_x"], profile["top"], profile["cell"]
            arr = np.array(img.crop((left, top, left + 8 * step, top + 2 * step)))
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            readings = []
            passes = []
            for threshold in (235, 245, 225):
                text, score = recognize(self.engine, np.where(gray > threshold, 0, 255).astype(np.uint8), (arr.shape[1] * 4, arr.shape[0] * 4))
                match = TIME_ONLY.search(text)
                value = ":".join(match.groups()) if match else None
                if value and score >= .9:
                    readings.append(value)
                passes.append({"text": text, "score": score, "clock": value, "white_threshold": threshold})
            report["hik_evidence"]["whole_clock_passes"] = passes
            votes = Counter(readings)
            if len(votes) == 1 and votes.most_common(1)[0][1] >= 2:
                clock = votes.most_common(1)[0][0]
        if day and clock:
            stamp = f"{day} {clock}"
            report.update(success=True, extracted_text=stamp, timestamp=stamp, wall_ms=wall_ms(stamp),
                          confidence="medium", roi=[.01, .05, .62 if img.width == 1280 else .42, .11])
            report["metadata"]["layout"] = profile["name"]
            report["warnings"].append("像素字体专用识别，需通过多帧推进核查")
            return True
        return False
