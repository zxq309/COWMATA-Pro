from __future__ import annotations

import ctypes

from cowmata_tailring.media.safe_engine import SafeMediaEngine


class StableMediaEngine(SafeMediaEngine):
    """Avoids VLC 3's blocking/crashing shutdown path for Hikvision streams.

    Windows reclaims the process-owned decoder resources at application exit.
    During normal use the same player instance is reused when files change.
    """

    def close(self) -> None:
        if self._closed:
            return
        self._poll_timer.stop()
        if self._player:
            try:
                self._lib.libvlc_media_player_set_pause(self._player, 1)
                self._lib.libvlc_media_player_set_hwnd(
                    self._player, ctypes.c_void_p(0)
                )
            except Exception:
                pass
        self._closed = True
        self._last_status = "closed"

    def __del__(self) -> None:
        # Do not enter libvlc_media_player_stop while Python/Qt are shutting
        # down; real Hikvision files can block there for several seconds.
        pass

