
import pytest
from PIL import Image
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.frame_cache import FrameCache, recommended_budget
from cowmata_tailring.workspace.playback import VideoBoard


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_frame_cache_bounded_lru_and_oversize():
    cache = FrameCache(32)
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    for key in ("a", "b"):
        cache.put(key, 123, image)
    assert cache.get("a")[0] == 123
    cache.put("c", 456, image)
    assert cache.get("b") is None
    assert cache.get("c")[0] == 456
    cache.put("huge", 0, QImage(100, 100, QImage.Format.Format_RGB32))
    assert cache.stats()["bytes"] == 32 and cache.stats()["frames"] == 2
    cache.clear()
    assert cache.stats()["bytes"] == 0


def test_memory_budget_is_bounded():
    assert 32 * 1024 ** 2 <= recommended_budget() <= 512 * 1024 ** 2


def test_precise_cache_never_confuses_source_identity_or_request(app, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import probe
    path = tmp_path / "same-name.mp4"
    path.write_bytes(b"TEST ONLY")
    board = VideoBoard()
    board.timer.stop()
    board.select(["A"])
    tile = board.tiles["A"]
    tile.asset_id = "identity-one"
    calls, received = [], []
    board.preciseReady.disconnect()
    board.preciseReady.connect(received.append)

    def extract(path, target, timeline, **options):
        assert options["image_codec"] == "bmp"
        calls.append(target)
        return Image.new("RGB", (2, 2)), target

    monkeypatch.setattr(probe, "extract_frame", extract)

    def request(target):
        board._precise_request(tile, path, target)
        for job in board.frame_jobs:
            job.result(timeout=5)
        app.processEvents()

    try:
        request(1000)
        request(1000)
        assert calls == [1000] and len(received) == 2
        request(1001)
        assert calls == [1000, 1001]
        tile.asset_id = "identity-two"
        request(1000)
        path.write_bytes(b"CHANGED TEST ONLY CONTENT")
        request(1000)
        assert calls == [1000, 1001, 1000, 1000]
        assert board.frame_cache.stats()["hits"] == 1
    finally:
        board.close()


def test_source_changed_during_decode_never_enters_cache(app, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import probe
    path = tmp_path / "changing.mp4"
    path.write_bytes(b"before")
    board = VideoBoard()
    board.timer.stop()
    board.select(["A"])
    board.tiles["A"].asset_id = "test-only"
    results = []
    board.preciseReady.disconnect()
    board.preciseReady.connect(results.append)

    def extract(*args, **kwargs):
        path.write_bytes(b"after replacement")
        return Image.new("RGB", (2, 2)), 0

    monkeypatch.setattr(probe, "extract_frame", extract)
    board._precise_request(board.tiles["A"], path, 0)
    board.frame_jobs[-1].result(timeout=5)
    app.processEvents()
    assert results[0][-1] and board.frame_cache.stats()["frames"] == 0
    board.close()


def test_repeat_main_selection_does_not_relayout_native_surfaces(app, monkeypatch):
    board = VideoBoard()
    board.timer.stop()
    board.select(["A", "B"])
    calls = []
    monkeypatch.setattr(board, "relayout", lambda: calls.append(True))
    board.set_main("A")
    assert not calls
    board.set_main("B")
    board.set_main("B")
    assert calls == [True]
    board.close()
