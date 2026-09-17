"""Native media playback engine for the cattle-tail annotation desktop app.

This module deliberately talks to libVLC through ``ctypes`` instead of using
python-vlc, so the desktop tool only needs PySide6 and a normal VLC
installation.  On Windows, video is rendered directly into a QWidget by
passing its ``winId()`` to ``libvlc_media_player_set_hwnd``.

The Hikvision files used by this project have names such as ``hiv00000.mp4``
but are MPEG-PS streams containing HEVC.  ``:demux=avformat`` is therefore
added as a per-media option.  This changes only how the file is read; it does
not transcode, copy, or modify the source file.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal


class MediaEngineError(RuntimeError):
    """Raised when libVLC cannot be loaded or initialized."""


class _VlcState:
    NOTHING_SPECIAL = 0
    OPENING = 1
    BUFFERING = 2
    PLAYING = 3
    PAUSED = 4
    STOPPED = 5
    ENDED = 6
    ERROR = 7

    NAMES = {
        NOTHING_SPECIAL: "idle",
        OPENING: "opening",
        BUFFERING: "buffering",
        PLAYING: "playing",
        PAUSED: "paused",
        STOPPED: "stopped",
        ENDED: "ended",
        ERROR: "error",
    }


class MediaEngine(QObject):
    """Small Qt-friendly wrapper around the libVLC 3.x media player API.

    Parameters
    ----------
    video_widget:
        QWidget that will receive native video output.  It may be omitted for
        metadata probing or supplied later with :meth:`set_video_widget`.
    vlc_path:
        VLC directory, ``libvlc.dll`` path, or ``vlc.exe`` path.  If omitted,
        common Windows locations (including ``F:\\Applications\\VLC``) and the
        ``VLC_HOME``/``VLC_PATH`` environment variables are checked.
    force_avformat:
        Force VLC's avformat demuxer.  This is enabled by default because it
        correctly detects the project's disguised MPEG-PS ``hiv*.mp4`` files.
    headless:
        Use dummy audio/video outputs.  Intended only for automated probes.
    """

    status_changed = Signal(str)
    error_occurred = Signal(str)
    time_changed = Signal(int)
    duration_changed = Signal(int)
    playing_changed = Signal(bool)
    seekable_changed = Signal(bool)
    media_opened = Signal(str)
    ended = Signal()

    def __init__(
        self,
        video_widget: Any | None = None,
        *,
        vlc_path: str | os.PathLike[str] | None = None,
        force_avformat: bool = True,
        headless: bool = False,
        poll_interval_ms: int = 50,
        instance_options: Iterable[str] = (),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if sys.platform != "win32":
            raise MediaEngineError("MediaEngine currently supports Windows only")

        self._vlc_dir = self.find_vlc_directory(vlc_path)
        self._dll_directory_handle: Any | None = None
        self._lib = self._load_libvlc(self._vlc_dir)
        self._bind_api()

        options = [
            "--intf=dummy",
            "--no-media-library",
            "--no-video-title-show",
            "--no-snapshot-preview",
            "--file-caching=1000",
            "--avcodec-hw=any",
        ]
        if headless:
            options.extend(("--vout=dummy", "--aout=dummy"))
        options.extend(str(option) for option in instance_options)

        self._instance = self._new_instance(options)
        self._player = self._lib.libvlc_media_player_new(self._instance)
        if not self._player:
            self._lib.libvlc_release(self._instance)
            self._instance = None
            raise MediaEngineError(self._format_error("cannot create media player"))

        self._media: int | None = None
        self._video_widget: Any | None = None
        self._path = ""
        self._force_avformat = bool(force_avformat)
        self._closed = False
        self._last_error = ""
        self._last_status = "idle"
        self._last_time = -1
        self._last_duration = -1
        self._last_playing = False
        self._last_seekable = False

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(max(15, int(poll_interval_ms)))
        self._poll_timer.timeout.connect(self.poll)
        if QCoreApplication.instance() is not None:
            self._poll_timer.start()

        if video_widget is not None:
            self.set_video_widget(video_widget)

    # ------------------------------------------------------------------
    # VLC discovery and C API setup
    # ------------------------------------------------------------------

    @staticmethod
    def find_vlc_directory(
        configured_path: str | os.PathLike[str] | None = None,
    ) -> Path:
        """Return a VLC directory containing libvlc.dll and plugins."""

        candidates: list[Path] = []
        if configured_path:
            candidates.append(Path(configured_path))
        candidates.append(Path(__file__).resolve().parents[2] / "vendor" / "vlc")

        for name in ("VLC_HOME", "VLC_PATH"):
            value = os.environ.get(name)
            if value:
                candidates.append(Path(value))

        candidates.extend(
            (
                Path(r"F:\Applications\VLC"),
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "VideoLAN"
                / "VLC",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
                / "VideoLAN"
                / "VLC",
            )
        )

        checked: list[str] = []
        for candidate in candidates:
            candidate = candidate.expanduser()
            if candidate.name.lower() in {"libvlc.dll", "vlc.exe"}:
                candidate = candidate.parent
            checked.append(str(candidate))
            if (
                (candidate / "libvlc.dll").is_file()
                and (candidate / "plugins").is_dir()
            ):
                return candidate.resolve()

        raise MediaEngineError(
            "VLC was not found. Configure vlc_path or VLC_HOME. Checked: "
            + ", ".join(checked)
        )

    def _load_libvlc(self, vlc_dir: Path) -> ctypes.CDLL:
        os.environ.setdefault("VLC_PLUGIN_PATH", str(vlc_dir / "plugins"))
        if hasattr(os, "add_dll_directory"):
            self._dll_directory_handle = os.add_dll_directory(str(vlc_dir))
        try:
            return ctypes.CDLL(str(vlc_dir / "libvlc.dll"))
        except OSError as exc:
            raise MediaEngineError(
                f"Unable to load {vlc_dir / 'libvlc.dll'}: {exc}"
            ) from exc

    def _bind_api(self) -> None:
        lib = self._lib
        void_p = ctypes.c_void_p
        char_p = ctypes.c_char_p
        int64 = ctypes.c_int64

        lib.libvlc_new.argtypes = [ctypes.c_int, ctypes.POINTER(char_p)]
        lib.libvlc_new.restype = void_p
        lib.libvlc_release.argtypes = [void_p]
        lib.libvlc_release.restype = None
        lib.libvlc_get_version.argtypes = []
        lib.libvlc_get_version.restype = char_p
        lib.libvlc_errmsg.argtypes = []
        lib.libvlc_errmsg.restype = char_p

        lib.libvlc_media_new_path.argtypes = [void_p, char_p]
        lib.libvlc_media_new_path.restype = void_p
        lib.libvlc_media_add_option.argtypes = [void_p, char_p]
        lib.libvlc_media_add_option.restype = None
        lib.libvlc_media_release.argtypes = [void_p]
        lib.libvlc_media_release.restype = None
        lib.libvlc_media_get_duration.argtypes = [void_p]
        lib.libvlc_media_get_duration.restype = int64

        lib.libvlc_media_player_new.argtypes = [void_p]
        lib.libvlc_media_player_new.restype = void_p
        lib.libvlc_media_player_release.argtypes = [void_p]
        lib.libvlc_media_player_release.restype = None
        lib.libvlc_media_player_set_media.argtypes = [void_p, void_p]
        lib.libvlc_media_player_set_media.restype = None
        lib.libvlc_media_player_set_hwnd.argtypes = [void_p, void_p]
        lib.libvlc_media_player_set_hwnd.restype = None
        lib.libvlc_media_player_play.argtypes = [void_p]
        lib.libvlc_media_player_play.restype = ctypes.c_int
        lib.libvlc_media_player_set_pause.argtypes = [void_p, ctypes.c_int]
        lib.libvlc_media_player_set_pause.restype = None
        lib.libvlc_media_player_stop.argtypes = [void_p]
        lib.libvlc_media_player_stop.restype = None
        lib.libvlc_media_player_is_playing.argtypes = [void_p]
        lib.libvlc_media_player_is_playing.restype = ctypes.c_int
        lib.libvlc_media_player_has_vout.argtypes = [void_p]
        lib.libvlc_media_player_has_vout.restype = ctypes.c_uint
        lib.libvlc_media_player_get_state.argtypes = [void_p]
        lib.libvlc_media_player_get_state.restype = ctypes.c_int
        lib.libvlc_media_player_get_time.argtypes = [void_p]
        lib.libvlc_media_player_get_time.restype = int64
        lib.libvlc_media_player_set_time.argtypes = [void_p, int64]
        lib.libvlc_media_player_set_time.restype = ctypes.c_int
        lib.libvlc_media_player_get_length.argtypes = [void_p]
        lib.libvlc_media_player_get_length.restype = int64
        lib.libvlc_media_player_set_position.argtypes = [
            void_p,
            ctypes.c_float,
        ]
        lib.libvlc_media_player_set_position.restype = None
        lib.libvlc_media_player_is_seekable.argtypes = [void_p]
        lib.libvlc_media_player_is_seekable.restype = ctypes.c_int
        lib.libvlc_media_player_set_rate.argtypes = [void_p, ctypes.c_float]
        lib.libvlc_media_player_set_rate.restype = ctypes.c_int
        lib.libvlc_media_player_get_rate.argtypes = [void_p]
        lib.libvlc_media_player_get_rate.restype = ctypes.c_float
        lib.libvlc_media_player_next_frame.argtypes = [void_p]
        lib.libvlc_media_player_next_frame.restype = None

        lib.libvlc_audio_set_mute.argtypes = [void_p, ctypes.c_int]
        lib.libvlc_audio_set_mute.restype = None
        lib.libvlc_audio_get_mute.argtypes = [void_p]
        lib.libvlc_audio_get_mute.restype = ctypes.c_int
        lib.libvlc_audio_set_volume.argtypes = [void_p, ctypes.c_int]
        lib.libvlc_audio_set_volume.restype = ctypes.c_int
        lib.libvlc_audio_get_volume.argtypes = [void_p]
        lib.libvlc_audio_get_volume.restype = ctypes.c_int

    def _new_instance(self, options: list[str]) -> int:
        encoded = [option.encode("utf-8") for option in options]
        argv = (ctypes.c_char_p * len(encoded))(*encoded)
        instance = self._lib.libvlc_new(len(encoded), argv)
        if not instance:
            raise MediaEngineError(self._format_error("cannot initialize libVLC"))
        return instance

    # ------------------------------------------------------------------
    # Public information
    # ------------------------------------------------------------------

    @property
    def vlc_directory(self) -> Path:
        return self._vlc_dir

    @property
    def vlc_version(self) -> str:
        value = self._lib.libvlc_get_version()
        return value.decode("utf-8", errors="replace") if value else "unknown"

    @property
    def current_path(self) -> str:
        return self._path

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def current_status(self) -> str:
        """Return the most recently observed libVLC playback state."""

        return self._last_status

    def set_video_widget(self, widget: Any | None) -> None:
        """Attach native video to a QWidget (or detach when passed None)."""

        self._ensure_open()
        self._video_widget = widget
        hwnd = 0 if widget is None else int(widget.winId())
        self._lib.libvlc_media_player_set_hwnd(
            self._player, ctypes.c_void_p(hwnd)
        )

    # ------------------------------------------------------------------
    # Media controls
    # ------------------------------------------------------------------

    def open(self, path: str | os.PathLike[str]) -> bool:
        """Open a local media file without starting playback."""

        self._ensure_open()
        normalized = os.path.abspath(os.fspath(path))
        if not os.path.isfile(normalized):
            return self._fail(f"Media file does not exist: {normalized}")

        self.stop()
        self._release_media()

        media = self._lib.libvlc_media_new_path(
            self._instance, normalized.encode("utf-8")
        )
        if not media:
            return self._fail(self._format_error(f"cannot open {normalized}"))

        if self._force_avformat:
            self._lib.libvlc_media_add_option(media, b":demux=avformat")
        self._lib.libvlc_media_add_option(media, b":file-caching=1000")

        self._lib.libvlc_media_player_set_media(self._player, media)
        self._media = media
        self._path = normalized
        self._last_error = ""
        self._reset_poll_cache()
        self._set_status("ready")
        self.media_opened.emit(normalized)
        return True

    def play(self) -> bool:
        self._ensure_media()
        result = self._lib.libvlc_media_player_play(self._player)
        if result != 0:
            return self._fail(self._format_error("playback could not start"))
        self._set_status("opening")
        return True

    def pause(self, paused: bool = True) -> None:
        self._ensure_media()
        self._lib.libvlc_media_player_set_pause(self._player, int(paused))
        self.poll()

    def toggle_pause(self) -> None:
        self.pause(self.is_playing())

    def stop(self) -> None:
        if self._closed or not self._player:
            return
        self._lib.libvlc_media_player_stop(self._player)
        self.poll()

    def set_time_ms(self, value: int | float) -> bool:
        self._ensure_media()
        target = max(0, int(round(value)))
        duration = self.duration_ms()
        if duration > 0:
            target = min(target, duration)
        result = self._lib.libvlc_media_player_set_time(self._player, target)
        if result != 0:
            return self._fail(
                self._format_error(f"could not seek to {target} ms")
            )
        return True

    def get_time_ms(self) -> int:
        if self._closed or not self._player:
            return 0
        return max(0, int(self._lib.libvlc_media_player_get_time(self._player)))

    def duration_ms(self) -> int:
        if self._closed or not self._player:
            return 0
        duration = int(self._lib.libvlc_media_player_get_length(self._player))
        if duration <= 0 and self._media:
            duration = int(self._lib.libvlc_media_get_duration(self._media))
        return max(0, duration)

    def set_rate(self, rate: float) -> bool:
        self._ensure_media()
        value = float(rate)
        if not 0.05 <= value <= 32.0:
            return self._fail("Playback rate must be between 0.05x and 32x")
        result = self._lib.libvlc_media_player_set_rate(
            self._player, ctypes.c_float(value)
        )
        if result != 0:
            return self._fail(
                self._format_error(f"playback rate {value:g}x is unsupported")
            )
        return True

    def get_rate(self) -> float:
        if self._closed or not self._player:
            return 1.0
        return float(self._lib.libvlc_media_player_get_rate(self._player))

    def frame_step(self) -> None:
        self._ensure_media()
        if self.is_playing():
            self._lib.libvlc_media_player_set_pause(self._player, 1)
        self._lib.libvlc_media_player_next_frame(self._player)
        self.poll()

    def mute(self, muted: bool = True) -> None:
        self._ensure_open()
        self._lib.libvlc_audio_set_mute(self._player, int(bool(muted)))

    def is_muted(self) -> bool:
        if self._closed or not self._player:
            return False
        return bool(self._lib.libvlc_audio_get_mute(self._player))

    def set_volume(self, volume: int | float) -> bool:
        self._ensure_open()
        value = max(0, min(100, int(round(volume))))
        result = self._lib.libvlc_audio_set_volume(self._player, value)
        if result != 0:
            return self._fail(self._format_error("could not set volume"))
        return True

    def volume(self) -> int:
        if self._closed or not self._player:
            return 0
        return max(0, int(self._lib.libvlc_audio_get_volume(self._player)))

    def is_playing(self) -> bool:
        if self._closed or not self._player:
            return False
        return bool(self._lib.libvlc_media_player_is_playing(self._player))

    def video_output_count(self) -> int:
        """Return the number of video outputs created for the current media."""

        if self._closed or not self._player:
            return 0
        return max(
            0,
            int(self._lib.libvlc_media_player_has_vout(self._player)),
        )

    def is_seekable(self) -> bool:
        if self._closed or not self._player:
            return False
        return bool(self._lib.libvlc_media_player_is_seekable(self._player))

    # ------------------------------------------------------------------
    # Polling/signals and lifecycle
    # ------------------------------------------------------------------

    def poll(self) -> dict[str, Any]:
        """Refresh signals and return a snapshot suitable for direct polling."""

        if self._closed or not self._player:
            return {
                "status": "closed",
                "time_ms": 0,
                "duration_ms": 0,
                "playing": False,
                "seekable": False,
                "rate": 1.0,
                "error": self._last_error,
                "path": self._path,
            }

        state_code = int(self._lib.libvlc_media_player_get_state(self._player))
        status = _VlcState.NAMES.get(state_code, f"unknown:{state_code}")
        current_time = self.get_time_ms()
        duration = self.duration_ms()
        playing = self.is_playing()
        seekable = self.is_seekable()

        if status != self._last_status:
            previous = self._last_status
            self._last_status = status
            self.status_changed.emit(status)
            if status == "ended" and previous != "ended":
                self.ended.emit()
            elif status == "error":
                self._fail(self._format_error("libVLC playback error"))

        if (
            current_time != self._last_time
            and self._accept_time_update(current_time, playing)
        ):
            self._last_time = current_time
            self.time_changed.emit(current_time)
        if duration != self._last_duration:
            self._last_duration = duration
            self.duration_changed.emit(duration)
        if playing != self._last_playing:
            self._last_playing = playing
            self.playing_changed.emit(playing)
        if seekable != self._last_seekable:
            self._last_seekable = seekable
            self.seekable_changed.emit(seekable)

        return {
            "status": status,
            "time_ms": current_time,
            "duration_ms": duration,
            "playing": playing,
            "seekable": seekable,
            "rate": self.get_rate(),
            "muted": self.is_muted(),
            "volume": self.volume(),
            "error": self._last_error,
            "path": self._path,
        }

    def _accept_time_update(
        self, _current_time_ms: int, _playing: bool
    ) -> bool:
        """Hook for subclasses that suppress stale post-seek timestamps."""

        return True

    def start_polling(self) -> None:
        if not self._closed:
            self._poll_timer.start()

    def stop_polling(self) -> None:
        self._poll_timer.stop()

    def close(self) -> None:
        if self._closed:
            return
        self._poll_timer.stop()
        if self._player:
            self._lib.libvlc_media_player_stop(self._player)
            self._lib.libvlc_media_player_set_media(self._player, None)
        self._release_media()
        if self._player:
            self._lib.libvlc_media_player_release(self._player)
            self._player = None
        if self._instance:
            self._lib.libvlc_release(self._instance)
            self._instance = None
        if self._dll_directory_handle is not None:
            self._dll_directory_handle.close()
            self._dll_directory_handle = None
        self._closed = True
        self._set_status("closed")

    def __enter__(self) -> MediaEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Interpreter shutdown may have already released Qt/ctypes objects.
            pass

    def _release_media(self) -> None:
        if self._media:
            self._lib.libvlc_media_release(self._media)
            self._media = None

    def _reset_poll_cache(self) -> None:
        self._last_time = -1
        self._last_duration = -1
        self._last_playing = False
        self._last_seekable = False

    def _set_status(self, status: str) -> None:
        if status != self._last_status:
            self._last_status = status
            self.status_changed.emit(status)

    def _fail(self, message: str) -> bool:
        self._last_error = message
        self.error_occurred.emit(message)
        self._set_status("error")
        return False

    def _format_error(self, prefix: str) -> str:
        raw = self._lib.libvlc_errmsg()
        detail = raw.decode("utf-8", errors="replace") if raw else ""
        return f"{prefix}: {detail}" if detail else prefix

    def _ensure_open(self) -> None:
        if self._closed or not self._player:
            raise MediaEngineError("MediaEngine is closed")

    def _ensure_media(self) -> None:
        self._ensure_open()
        if not self._media:
            raise MediaEngineError("No media file is open")


__all__ = ["MediaEngine", "MediaEngineError"]
