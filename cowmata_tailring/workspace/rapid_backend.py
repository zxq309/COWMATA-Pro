"""Pinned, local-only PP-OCRv6 medium with the existing evidence API."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

MODEL_ROOT = Path(__file__).resolve().parents[2] / "assets" / "ocr" / "ppocrv6_medium"
OCR_SIGNATURE = "rapidocr-3.9.2/PP-OCRv6_medium"
TIMESTAMP_SIGNATURE = OCR_SIGNATURE + "/osd-light-v2-20260907"


@lru_cache(maxsize=12)
def _validate_model(path_string, size, mtime_ns, expected):
    digest = hashlib.sha256()
    with Path(path_string).open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("离线 OCR 模型校验失败，请重新完整解压软件包")


def local_models(root=MODEL_ROOT):
    registry = json.loads((root / "models.json").read_text(encoding="utf-8"))
    paths = {}
    for task, item in registry["files"].items():
        path = root / item["name"]
        if not path.is_file():
            raise FileNotFoundError("便携包缺少离线 OCR 模型，请完整解压软件包：" + path.name)
        stat = path.stat()
        _validate_model(str(path), stat.st_size, stat.st_mtime_ns, item["sha256"])
        paths[task] = str(path)
    return paths


class RapidV6Adapter:
    """Normalize RapidOCR 3 outputs; explicitly reset per-call mutable switches."""

    def __init__(self, model_root=MODEL_ROOT):
        from rapidocr import ModelType, OCRVersion, RapidOCR
        models = local_models(model_root)
        self.engine = RapidOCR(params={
            "Global.log_level": "error", "Global.use_cls": False,
            "Global.model_root_dir": str(model_root),
            # Thin timestamp crops must not be expanded to 736 pixels high:
            # that default can create enormous images from a long OSD strip.
            "Det.limit_type": "max", "Det.limit_side_len": 1280,
            "Det.model_path": models["det"], "Det.model_type": ModelType.MEDIUM, "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_path": models["rec"], "Rec.model_type": ModelType.MEDIUM, "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Cls.model_path": models["cls"],
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
        })
        # Our ONNX file embeds the matching character dictionary. Do not permit
        # a generic fallback dictionary or any model/font downloader in the app.
        if not self.engine.text_rec.session.have_key():
            raise ValueError("OCR 模型缺少匹配字典，不能建立可信识别")

    def __call__(self, image, *, use_det=True, use_cls=False):
        if getattr(self, "cancelled", None) and self.cancelled():
            raise InterruptedError("OCR request cancelled")
        output = self.engine(image, use_det=use_det, use_cls=use_cls, use_rec=True)
        texts = output.txts if output.txts is not None else ()
        scores = output.scores if output.scores is not None else ()
        if not use_det:
            return [[text, float(score)] for text, score in zip(texts, scores)], None
        boxes = output.boxes if output.boxes is not None else ()
        return [[box.tolist(), text, float(score)] for box, text, score in zip(boxes, texts, scores)], None
