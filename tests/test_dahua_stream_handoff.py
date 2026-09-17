from __future__ import annotations

from cowmata_tailring.media.safe_engine import SafeMediaEngine


class _Lib:
    def __init__(self, play_result: int = 0, state_code: int = 3) -> None:
        self.play_result = play_result
        self.state_code = state_code
        self.play_calls = 0
        self.pause_calls = 0

    def libvlc_media_player_play(self, _player) -> int:
        self.play_calls += 1
        return self.play_result

    def libvlc_media_player_set_pause(self, _player, _paused: int) -> None:
        self.pause_calls += 1

    def libvlc_media_player_get_state(self, _player) -> int:
        return self.state_code


class _Stream:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Harness:
    _prepare_dahua_stream_release = (
        SafeMediaEngine._prepare_dahua_stream_release
    )
    _prepare_standard_media_release = (
        SafeMediaEngine._prepare_standard_media_release
    )

    def __init__(self, lib: _Lib, *, player: object | None = object()) -> None:
        self._lib = lib
        self._player = player


def test_callback_stream_handoff_closes_and_drains_decoder(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "cowmata_tailring.media.safe_engine.time.sleep",
        sleeps.append,
    )
    lib = _Lib()
    stream = _Stream()
    engine = _Harness(lib)

    assert engine._prepare_dahua_stream_release(stream)
    assert stream.close_calls == 1
    assert lib.play_calls == 1
    assert sleeps


def test_callback_stream_handoff_rejects_failed_decoder_resume(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cowmata_tailring.media.safe_engine.time.sleep",
        lambda _seconds: None,
    )
    lib = _Lib(play_result=-1)
    stream = _Stream()
    engine = _Harness(lib)

    assert not engine._prepare_dahua_stream_release(stream)
    assert stream.close_calls == 1
    assert lib.play_calls == 1


def test_callback_stream_handoff_without_player_closes_source(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cowmata_tailring.media.safe_engine.time.sleep",
        lambda _seconds: None,
    )
    lib = _Lib()
    stream = _Stream()
    engine = _Harness(lib, player=None)

    assert engine._prepare_dahua_stream_release(stream)
    assert stream.close_calls == 1
    assert lib.play_calls == 0


def test_standard_media_handoff_pauses_before_grace_period(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "cowmata_tailring.media.safe_engine.time.sleep",
        sleeps.append,
    )
    lib = _Lib()
    engine = _Harness(lib)

    engine._prepare_standard_media_release()

    assert lib.pause_calls == 1
    assert sleeps


def test_idle_standard_media_needs_no_release_delay(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "cowmata_tailring.media.safe_engine.time.sleep",
        sleeps.append,
    )
    lib = _Lib(state_code=0)
    engine = _Harness(lib)

    engine._prepare_standard_media_release()

    assert lib.pause_calls == 0
    assert sleeps == []
