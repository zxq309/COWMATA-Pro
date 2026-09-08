"""GUI-safe proxy for native decoding. All libVLC work stays in one owner thread.

Keep HWND rendering/audio/GPU paths unchanged. Commands describe the latest
desired state, so rapid seeks do not form a queue of obsolete decoder opens.
Cached observations are tagged by source generation and seek serial.
"""
from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal, Slot

# Native callback owners must outlive VLC's asynchronous decoder shutdown.
# This is the same conservative lifetime as StableMediaEngine, not a frame cache.
_OWNERS = []
_EXIT_CONNECTED = False


def finish_decoders():
    for owner in _OWNERS:
        owner.close()
    deadline = time.monotonic() + 5
    for owner in _OWNERS:
        remaining = max(1, int((deadline-time.monotonic())*1000))
        owner.thread_owner.wait(remaining)


class _Decoder(QObject):
    failed = Signal(str)

    def __init__(self, shared, hwnd, software, no_audio, factory=None):
        super().__init__()
        self.shared, self.hwnd = shared, hwnd
        self.software, self.no_audio, self.factory = software, no_audio, factory
        self.backend = None
        self.generation = self.seek_serial = -1
        self.last_play = self.last_rate = self.last_volume = None
        self.timer = None
        self.metadata = {}

    @Slot()
    def apply(self):
        self.shared['queued'] = False
        desired = self.shared['desired']
        if desired.get('closed'):
            if self.timer:
                self.timer.stop()
            if self.backend:
                self.backend.close()
            self.thread().quit()
            return
        if not desired.get('path'):
            return
        try:
            if self.backend is None:
                self.backend = self.factory(SimpleNamespace(winId=lambda:self.hwnd),
                    metadata=lambda path:self.metadata, cache=desired['cache'], parent=self,
                    software=self.software, no_audio=self.no_audio)
                self.backend.error_occurred.connect(self.failed)
                self.timer = QTimer(self)
                self.timer.setInterval(20)
                self.timer.timeout.connect(self.observe)
                self.timer.start()
            engine = self.backend
            engine.project_cache = desired['cache']
            if desired['generation'] != self.generation:
                self.metadata = desired['metadata']
                if not engine.open(desired['path']):
                    raise RuntimeError(engine.last_error)
                self.generation = desired['generation']
                self.seek_serial = -1
                self.last_play = self.last_rate = self.last_volume = None
            if desired['seek_serial'] != self.seek_serial:
                if desired['target'] is None:
                    engine.clear_pending_seek()
                else:
                    engine.pause(False)
                    engine.play()
                    if not engine.set_time_ms(desired['target']):
                        raise RuntimeError(engine.last_error)
                self.seek_serial = desired['seek_serial']
                self.last_play = None
            if desired['rate'] != self.last_rate:
                engine.set_rate(desired['rate'])
                self.last_rate = desired['rate']
            if desired['volume'] != self.last_volume:
                engine.set_volume(desired['volume'])
                self.last_volume = desired['volume']
            if desired['playing'] != self.last_play:
                if desired['playing']:
                    engine.play()
                else:
                    engine.pause(True)
                self.last_play = desired['playing']
            self.observe()
        except Exception as exc:
            self.shared['snapshot'] = {'generation':desired['generation'], 'seek_serial':desired['seek_serial'],
                                       'status':'error','error':str(exc)}
            self.failed.emit(str(exc))

    @Slot()
    def observe(self):
        if not self.backend or self.shared['desired'].get('closed'):
            return
        try:
            engine = self.backend
            stats = engine.stats()
            self.shared['snapshot'] = {'generation':self.generation, 'seek_serial':self.seek_serial,
                'status':engine.current_status, 'error':engine.last_error, 'time':engine.get_time_ms(),
                'duration':engine.duration_ms(), 'seekable':engine.is_seekable(), 'vout':engine.video_output_count(),
                'indexed_seek':bool(engine._dahua_duration_index),
                'stats':{name:getattr(stats,name) for name,_ in stats._fields_} if stats else None}
        except Exception as exc:
            self.failed.emit(str(exc))


class ThreadedWorkspaceEngine(QObject):
    error_occurred = Signal(str)
    wake = Signal()

    def __init__(self, widget, *, metadata, cache, backend_factory, parent=None, software=False, no_audio=False):
        super().__init__(parent)
        self.metadata_provider, self.project_cache = metadata, cache
        self._path, self._closed = '', False
        self._force_avformat, self._native_hint = True, None
        self.shared = {'queued':False,'snapshot':{},'desired':{'path':'','metadata':{},'cache':cache,
            'generation':0,'seek_serial':0,'target':None,'playing':False,'rate':1.0,'volume':0}}
        # Resolve the real QWidget handle here; the decoder thread sees only an int.
        self._surface_owner = widget
        self.thread_owner = QThread()
        self.decoder = _Decoder(self.shared,int(widget.winId()),software,no_audio,backend_factory)
        self.decoder.moveToThread(self.thread_owner)
        self.decoder.failed.connect(self.error_occurred)
        self.wake.connect(self.decoder.apply)
        self.thread_owner.start()
        _OWNERS.append(self)
        global _EXIT_CONNECTED
        app = QCoreApplication.instance()
        if app and not _EXIT_CONNECTED:
            app.aboutToQuit.connect(finish_decoders)
            _EXIT_CONNECTED = True

    def _send(self, **changes):
        if self._closed and not changes.get('closed'):
            return False
        self.shared['desired'] = {**self.shared['desired'], **changes, 'cache':self.project_cache}
        if not self.shared['queued']:
            self.shared['queued'] = True
            self.wake.emit()
        return True

    def _snapshot(self):
        value, desired = self.shared['snapshot'], self.shared['desired']
        return value if (value.get('generation'),value.get('seek_serial')) == (desired['generation'],desired['seek_serial']) else {}

    @property
    def current_status(self):
        return 'closed' if self._closed else self._snapshot().get('status','opening')

    @property
    def last_error(self):
        return self._snapshot().get('error','')

    @property
    def _dahua_duration_index(self):
        return self._native_hint or self._snapshot().get('indexed_seek')

    def open(self, path):
        self._path = str(Path(path).resolve())
        metadata = copy.deepcopy(self.metadata_provider(Path(self._path)) or {})
        self._native_hint = metadata.get('timeline',{}).get('native')
        return self._send(path=self._path, metadata=metadata,
            generation=self.shared['desired']['generation']+1, target=None, seek_serial=0,playing=False)

    def set_time_ms(self, value):
        return self._send(target=float(value), seek_serial=self.shared['desired']['seek_serial']+1)

    def clear_pending_seek(self):
        return self._send(target=None, seek_serial=self.shared['desired']['seek_serial']+1)

    def play(self):
        return self._send(playing=True)

    def pause(self, paused=True):
        return self._send(playing=not paused)

    def set_rate(self, rate):
        return self._send(rate=float(rate))

    def set_volume(self, volume):
        return self._send(volume=int(volume))

    def get_time_ms(self):
        return self._snapshot().get('time',0)

    def duration_ms(self):
        return self._snapshot().get('duration',0)

    def video_output_count(self):
        return self._snapshot().get('vout',0)

    def is_seekable(self):
        return self._snapshot().get('seekable',False)

    def stats(self):
        value=self._snapshot().get('stats')
        return SimpleNamespace(**value) if value else None

    def close(self):
        if not self._closed:
            self._closed=True
            self._send(closed=True)
