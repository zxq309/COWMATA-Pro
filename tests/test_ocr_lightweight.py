import copy

import numpy as np
import pytest
from PIL import Image

from cowmata_tailring.workspace.ocr import TimestampOCR


def algorithm():
    obj = TimestampOCR.__new__(TimestampOCR)
    obj.signature = "test-only"
    return obj


def report(stamp=None, **extra):
    return dict(success=stamp is not None, timestamp=stamp, metadata={}, warnings=[], passes=[], **extra)


def test_ordinary_success_does_not_enhance(monkeypatch):
    obj = algorithm()
    calls = []
    expected = report("2026-08-03 12:44:58")
    monkeypatch.setattr(obj, "_recognize_once", lambda *a, **k: calls.append(k) or expected)
    assert obj.recognize(Image.new("RGB", (640, 360))) is expected
    assert len(calls) == 1


@pytest.mark.parametrize("same", [True, False])
def test_enhancement_requires_two_successful_equal_readings(monkeypatch, same):
    obj = algorithm()
    first = report("2026-08-03 12:44:58")
    second = report("2026-08-03 12:44:58" if same else "2026-08-03 12:44:59")
    original = report()
    outputs = iter([original, first, second])
    monkeypatch.setattr(obj, "_recognize_once", lambda *a, **k: next(outputs))
    pixels = np.full((360, 640, 3), 80, np.uint8)
    pixels[:, 320:] = 140
    result = obj.recognize(Image.fromarray(pixels))
    assert result["success"] == same
    assert len(result["enhancement_attempts"]) == 2
    if same:
        assert result["confidence"] == "medium"
        assert result["metadata"]["width"] == 640
        assert "enhancement_attempts" not in first  # No self-referential evidence.


def test_resize_retry_preserves_normalized_roi_and_source_size(monkeypatch):
    obj = algorithm()
    original = report()
    original["passes"] = [{"region": "top_left", "text": "2026年08月03日 星期一"}]
    recovered = report("2026-08-03 12:44:58", roi=[.01, .05, .62, .11])
    calls = []
    def recognize(image, **kwargs):
        calls.append((image.size, kwargs))
        return copy.deepcopy(original if len(calls) == 1 else recovered)
    monkeypatch.setattr(obj, "_recognize_once", recognize)
    pixels = np.zeros((360, 640, 3), np.uint8)
    pixels[:, 320:] = 255
    hint = [.01, .05, .62, .11]
    result = obj.recognize(Image.fromarray(pixels), hint=hint)
    assert [c[0] for c in calls] == [(640, 360), (1280, 720), (1280, 720)]
    assert all(c[1]["hint"] == hint for c in calls)
    assert result["metadata"]["width"] == 640
    assert result["roi"] == hint


def test_tiny_digit_conflict_cannot_be_accepted():
    obj = algorithm()
    def rows(stamp, height):
        return [[[[0, 0], [230, 0], [230, height], [0, height]], stamp, .99]], None
    answers = iter([rows("2024-01-08 05:57:45", 10), rows("2024-01-08 05:57:45", 14),
                    rows("2024-01-08 05:57:46", 20)])
    obj.engine = lambda *a, **k: next(answers)
    result = obj.recognize(Image.new("RGB", (640, 360)))
    assert not result["success"]
    assert result["enhancement_conflict"]
    assert result["timestamp"] is None


def test_profile_hint_cannot_bypass_date_conflict(monkeypatch):
    from cowmata_tailring.workspace import hik_osd
    obj = algorithm()
    obj.engine = lambda *a, **k: pytest.fail("Conflicting profile must stop before generic votes")
    monkeypatch.setattr(hik_osd, "recognize_date", lambda *a: ("2021-08-06", [
        {"date": "2021-08-06", "score": .99}, {"date": "2026-08-06", "score": .76}]))
    result = obj.recognize(Image.new("RGB", (2560, 1440)), profile_hint="hd_no_weekday")
    assert not result["success"]
    assert result["enhancement_conflict"]


def test_ocr_upgrade_rechecks_once_without_losing_manual_work(tmp_path):
    from cowmata_tailring.workspace.catalog import Catalog
    with_source = tmp_path / "001.mp4"
    with_source.write_bytes(b"test fixture, not footage")
    cat = Catalog(tmp_path, stability_seconds=0)
    try:
        cat.scan(now=100)
        old = cat.index_one("001.mp4", lambda *a: {"ocr_engine": "old"}, now=101)
        work = cat.work_path(old["asset_id"])
        work.parent.mkdir(exist_ok=True)
        work.write_bytes(b"human-labels-must-stay")
        assert cat.queue_ocr_upgrade("new") == 1
        assert cat.rows()[0]["asset_id"] == old["asset_id"]
        new = cat.index_one("001.mp4", lambda *a: {"ocr_engine": "new"}, now=105)
        assert new["metadata"]["ocr_engine"] == "new"
        assert cat.queue_ocr_upgrade("new") == 0
        assert work.read_bytes() == b"human-labels-must-stay"
        cat.update_metadata(old["asset_id"], {"ocr_engine": "old", "manual_readings": [{}, {}]})
        assert cat.queue_ocr_upgrade("new") == 0
        assert cat.rows()[0]["state"] == "ready"
    finally:
        cat.close()
