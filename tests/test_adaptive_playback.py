from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.adaptive_playback import AdaptiveVideoBoard
from cowmata_tailring.workspace.materials import FrostedCanvas, apply_mica


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_preview_is_excluded_from_evidence_and_strict_sync(app):
    b = AdaptiveVideoBoard()
    b.timer.stop()
    b.select(["A", "B"])
    b.playing = True
    b.playback_policy = "balanced"
    interval = SimpleNamespace(verified=True)
    for tile in b.tiles.values():
        tile.interval = interval
        tile.actual_ms = 10000
        tile.ready = True
    assert b.synchronised_tiles() == [b.tiles["A"]]
    assert [e["camera"] for e in b.evidence()] == ["A"]
    assert [c for c, _ in b.prewarm_candidates()] == ["A"]
    b.playing = False
    assert len(b.evidence()) == 2
    b.close()


def test_stale_preview_callback_releases_slot_without_painting(app):
    b = AdaptiveVideoBoard()
    b.timer.stop()
    b.select(["A", "B"])
    token = (b.generation - 1, "old", 0, id(b.tiles["B"]))
    b.preview_tasks["B"] = token
    b._preview_ready(("B", token, 100, None, None))
    assert not b.preview_tasks and b.preview_stats["stale"] == 1
    assert b.tiles["B"].frame_image is None
    b.close()


def test_paused_board_does_not_allocate_vlc(app, tmp_path, monkeypatch):
    from cowmata_tailring.workspace.catalog import file_stamp
    b = AdaptiveVideoBoard(engine_factory=lambda *a, **kw: pytest.fail("Paused review allocated VLC"))
    b.timer.stop()
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"test-only")
    b.catalog = SimpleNamespace(source_path=lambda _: path)
    b.source_stamps = {"fake.mp4": file_stamp(path)}
    b.select(["A"])
    calls = []
    monkeypatch.setattr(b, "_precise_request", lambda tile, path, target: calls.append(target))
    interval = SimpleNamespace(asset_id="a", path="fake.mp4")
    b._request(b.tiles["A"], interval, 1234)
    assert calls == [1234] and b.tiles["A"].engine is None
    b.close()


def test_glass_background_cache_and_fallback(app):
    w = FrostedCanvas()
    w.resize(800, 500)
    w.show()
    app.processEvents()
    w.grab()
    assert w.cache is not None
    first = w.cache.cacheKey()
    w.grab()
    assert w.cache.cacheKey() == first
    w.set_effects(False)
    w.grab()
    assert not w.effects_enabled and w.cache.cacheKey() != first
    result = apply_mica(int(w.winId()), False)
    assert result["enabled"] is False
    w.close()


def test_cold_full_decoders_are_bounded_and_main_first(app, tmp_path, monkeypatch):
    from cowmata_tailring.workspace.playback import VideoBoard
    b = AdaptiveVideoBoard()
    b.timer.stop()
    b.decoder_timer.stop()
    b.select(["A", "B"])
    b.catalog = SimpleNamespace(source_path=lambda p: tmp_path / p)
    b.playing = True
    calls = []
    monkeypatch.setattr(VideoBoard, "_request", lambda self, tile, *a, **kw: calls.append(tile.camera))
    for camera in ("B", "A"):
        b._request(b.tiles[camera], SimpleNamespace(asset_id=camera, path=camera + ".mp4"), 1234)
    assert calls == [] and len(b.decoder_queue) == 2
    assert all(t.pending["phase"] == "decoder_queue" for t in b.tiles.values())
    b._start_queued_decoder()
    assert calls == ["A"] and len(b.decoder_queue) == 1
    b.generation += 1
    b._start_queued_decoder()
    assert calls == ["A"] and not b.decoder_queue
    b.close()


def test_preview_rejects_same_asset_after_timestamp_break(app):
    b = AdaptiveVideoBoard()
    b.timer.stop()
    b.select(["A", "B"])
    b.playing = True
    b.playback_policy = "balanced"
    tile = b.tiles["B"]
    tile.asset_id, tile.interval = "same", object()
    token = (b.generation, "same", 1000, id(tile), object())
    b.preview_tasks["B"] = token
    b._preview_ready(("B", token, 1000, None, None))
    assert not tile.ready and tile.frame_image is None
    assert not b.preview_tasks and b.preview_stats["stale"] == 1
    b.close()
