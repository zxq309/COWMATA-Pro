"""Bounded auxiliary previews: one live main camera plus two frame workers.

Preview images are never evidence. Pausing restores original exact frames for
all selected views; switching the main camera promotes the original source.
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QImage

from cowmata_tailring.media.timeline import MediaTimelineIndex

from .catalog import file_stamp
from .clocks import wall_text
from .event_models import inference_active
from .playback import VideoBoard


class AdaptiveVideoBoard(VideoBoard):
    previewReady = Signal(object)
    policyChanged = Signal(str)

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.playback_policy = "full"
        self.preview_tasks = {}
        self.preview_last = {}
        self.preview_stats = {"submitted": 0, "completed": 0, "stale": 0, "errors": 0, "max_inflight": 0}
        self.previewReady.connect(self._preview_ready)
        self.decoder_queue = {}
        self._full_wait_since = None
        self.frozen_previews = set()
        self.precise_queue = {}
        self.precise_active = None
        self.decoder_timer = QTimer(self)
        self.decoder_timer.timeout.connect(self._start_queued_decoder)
        self.decoder_timer.timeout.connect(self._start_queued_frame)
        self.decoder_timer.start(40)

    def _request(self, tile, interval, target, *, queued_start=None):
        # Yield to input/painting between cold starts, instead of opening eight
        # native decoders synchronously in one mouse/keyboard callback.
        cold = bool(self.catalog and (tile.engine is None or not tile.engine._path
                    or Path(tile.engine._path) != self.catalog.source_path(interval.path)))
        if self.playing and self.playback_policy == "full" and tile is not self.prewarm and cold:
            started = queued_start or time.perf_counter()
            tile.asset_id = interval.asset_id
            tile.ready = False
            tile.stack.hide()
            tile.pending = {"phase": "decoder_queue", "generation": self.generation, "asset": interval.asset_id,
                            "target": target, "start": started}
            self.decoder_queue[tile] = (self.generation, interval, target, started)
            return
        super()._request(tile, interval, target, queued_start=queued_start)

    def _start_queued_decoder(self):
        if self._closing or not self.decoder_queue:
            return
        tile = next((t for t in self.decoder_queue if t.camera == self.main_camera), next(iter(self.decoder_queue)))
        generation, interval, target, started = self.decoder_queue.pop(tile)
        if (generation != self.generation or not self.playing or self.is_preview(tile.camera)
                or tile not in self.tiles.values() or tile.asset_id != interval.asset_id):
            return
        super()._request(tile, interval, target, queued_start=started)

    def set_policy(self, policy):
        if policy not in {"full", "balanced", "focus"}:
            raise ValueError("Unknown playback policy")
        requested = policy
        if policy == "full" and self.playing and len(self.selected) >= 4 and inference_active():
            policy = "balanced"
            self.notice.emit("后台模型正在运行：多路播放已启用主路优先保护；暂停仍核验各路原片，推理结束后可手动切回全速。")
        if self.playback_policy == policy:
            if requested != policy:
                self.policyChanged.emit(policy)
            return
        self.playback_policy = policy
        self._full_wait_since = None
        self.preview_last.clear()
        if self.prewarm:
            self._pause_tile(self.prewarm)
            self.prewarm.pending = None
        self.seek(self.reference_ms)
        self.policyChanged.emit(policy)

    def tick(self):
        if not self._closing and self.playing and self.playback_policy == "full" and len(self.selected) >= 4 and inference_active():
            self.set_policy("full")  # guarded policy; no automatic oscillation back
        result = super().tick()
        main = self.tiles.get(self.main_camera)
        if self.playing and self.playback_policy == "focus" and main and main.interval is None:
            self.play(False)
            self.notice.emit("主视角到达录像缺口，已暂停；可切换其他有覆盖的视角继续。")
        waiting = (self.playing and self.playback_policy == "full" and len(self.selected) >= 4
                   and (self._held or any(t.interval and (not t.ready or t.pending) for t in self.tiles.values())))
        if not waiting:
            self._full_wait_since = None
        elif self._full_wait_since is None:
            self._full_wait_since = time.perf_counter()
        elif time.perf_counter() - self._full_wait_since >= 6:
            self.set_policy("balanced")
            self.notice.emit("多路解码等待较久，已切换流畅优先：主路播放、辅路预览。暂停仍读取各路原片精确帧。")
        return result

    def is_preview(self, camera):
        return self.playing and self.playback_policy in {"balanced", "focus"} and camera != self.main_camera

    def seek(self, reference_ms):
        self.frozen_previews.clear()
        self.precise_queue.clear()
        self.precise_active = None
        return super().seek(reference_ms)

    def _precise_request(self, tile, path, target):
        if self.playback_policy == "focus" and tile.camera != self.main_camera:
            self.precise_queue[tile] = (self.generation, path, target)
            tile.status("等待主视角 · 随后逐个读取暂停帧")
            return
        super()._precise_request(tile, path, target)

    def _start_queued_frame(self):
        if self._closing or self.playing or not self.precise_queue:
            return
        if self.precise_active and self.precise_active.pending:
            return
        main = self.tiles.get(self.main_camera)
        if main and main.pending:
            return
        tile = next(iter(self.precise_queue))
        generation, path, target = self.precise_queue.pop(tile)
        if generation == self.generation and tile in self.tiles.values() and tile.pending:
            self.precise_active = tile
            super()._precise_request(tile, path, target)

    def synchronised_tiles(self):
        return [t for c, t in self.tiles.items() if t.interval and not self.is_preview(c)]

    def prewarm_candidates(self):
        return [(c, t) for c, t in self.tiles.items() if not self.is_preview(c)]

    def set_main(self, camera):
        previous = self.main_camera
        super().set_main(camera)
        if previous != self.main_camera and self.playback_policy == "focus" and not self.playing:
            tile = self.tiles[self.main_camera]
            queued = self.precise_queue.pop(tile, None)
            if queued and queued[0] == self.generation and tile.pending:
                super()._precise_request(tile, queued[1], queued[2])
        elif previous != self.main_camera and self.playing and self.playback_policy in {"balanced", "focus"}:
            self.frozen_previews = {v for v in self.frozen_previews if v[1] != previous}
            old = self.tiles.get(previous)
            if old:
                self._preview_position(previous, old)
            tile = self.tiles[self.main_camera]
            tile._preview_only = False
            tile.precise_ms = None
            tile.ready = False
            tile.pending = None
            self._position(self.main_camera, tile, force=True)

    def _position(self, camera, tile, *, force=False):
        if self.is_preview(camera):
            self._preview_position(camera, tile)
        else:
            tile._preview_only = False
            super()._position(camera, tile, force=force)

    def _preview_position(self, camera, tile):
        if not getattr(tile, "_preview_only", False):
            if tile.engine:
                # VLC 3 stop can block on these surveillance streams. Pausing
                # releases decode load while keeping the bounded warm player.
                tile.engine.pause(True)
            tile._preview_only = True
            tile.pending = None
            tile.ready = False
            tile.precise_ms = None
            tile.actual_ms = None
            tile.stack.hide()
        # Focus mode keeps one dated still per view. It is not a live frame
        # and must never become evidence merely because the main clock moves.
        frozen = (self.generation, camera, tile.asset_id)
        if self.playback_policy == "focus" and frozen in self.frozen_previews:
            if tile.asset_id in self.blocked_assets:
                tile.stack.hide()
                tile.status("源文件变化，请刷新索引")
                return
            if not self.timeline.locate(camera, self.reference_ms, prefer=tile.asset_id):
                tile.interval = None
                tile.stack.hide()
                tile.status("此时刻无录像覆盖 · 不显示旧预览")
                return
            self._preview_status(tile)
            return
        match = self.timeline.locate(camera, self.reference_ms, prefer=tile.asset_id)
        if not match or match[0].asset_id in self.blocked_assets:
            tile.interval = None
            tile.ready = False
            tile.stack.hide()
            tile.status("此时刻无录像覆盖 · 不显示旧预览")
            return
        interval, target = match
        changed = tile.asset_id != interval.asset_id
        tile.interval, tile.asset_id = interval, interval.asset_id
        if changed:
            tile.actual_ms = None
            tile.stack.hide()
        now = time.perf_counter()
        self._preview_status(tile)
        limit = 1 if self.playback_policy == "focus" else 2
        main = self.tiles.get(self.main_camera)
        if self.playback_policy == "focus" and main and (main.pending or not main.ready):
            return
        if not self.catalog or camera in self.preview_tasks or len(self.preview_tasks) >= limit:
            return
        # Round-robin fairness, with at most two tasks total (not two per view).
        available = [c for c, t in self.tiles.items() if self.is_preview(c) and t.interval and c not in self.preview_tasks
                     and (self.playback_policy != "focus" or (self.generation,c,t.asset_id) not in self.frozen_previews)]
        if available and camera != min(available, key=lambda c: self.preview_last.get(c, -1)):
            return
        if now - self.preview_last.get(camera, -1e9) < 1.0:
            return
        path = self.catalog.source_path(interval.path)
        try:
            if file_stamp(path) != self.source_stamps.get(interval.path):
                self.blocked_assets.add(interval.asset_id)
                raise OSError("源文件变化，请刷新索引")
        except OSError as exc:
            tile.stack.hide()
            tile.status(str(exc))
            return
        generation, asset = self.generation, interval.asset_id
        token = (generation, asset, target, id(tile), interval)
        self.preview_tasks[camera] = token
        self.preview_last[camera] = now
        self.preview_stats["submitted"] += 1
        self.preview_stats["max_inflight"] = max(self.preview_stats["max_inflight"], len(self.preview_tasks))
        raw = self.metadata.get(str(path), {}).get("timeline")
        timeline = MediaTimelineIndex.from_dict(raw) if raw else None

        def cancelled():
            return self._closing or generation != self.generation or not self.is_preview(camera)

        def read():
            try:
                from .probe import extract_frame
                frame, actual = extract_frame(path, target, timeline, preroll=2, cancelled=cancelled, preview_width=480,
                                              image_codec="bmp")
                data = frame.tobytes("raw", "RGB")
                image = QImage(data, frame.width, frame.height, frame.width * 3, QImage.Format.Format_RGB888).copy()
                result = (camera, token, actual, image, None)
            except Exception as exc:
                result = (camera, token, None, None, str(exc))
            if not self._closing:
                self.previewReady.emit(result)

        # Running/cancelled callbacks always release tokens; no growing queue.
        self.frame_pool.submit(read)

    def _preview_status(self, tile):
        if self.playback_policy == "focus":
            if tile.actual_ms is None:
                tile.status("暂停 · 等待后台截图，点击播放此视角")
            else:
                tile.status(f"已暂停 · {wall_text(tile.actual_ms)} · 点击播放对齐当前时间")
            return
        if tile.actual_ms is None:
            tile.status("辅视角预览准备中 · 点击切为原片全速主视角")
            return
        age = abs(self.reference_ms - tile.actual_ms) / 1000
        tile.status(f"预览 {wall_text(tile.actual_ms)} · 相差 {age:.1f}s · 非当前真值", good=False)
        if age > 10:
            tile.stack.hide()

    def _preview_ready(self, result):
        camera, token, actual, image, error = result
        if self.preview_tasks.get(camera) == token:
            self.preview_tasks.pop(camera)
        tile = self.tiles.get(camera)
        if (self._closing or token[0] != self.generation or not self.is_preview(camera) or not tile
                or token[1] != tile.asset_id or token[3] != id(tile) or token[4] != tile.interval):
            self.preview_stats["stale"] += 1
            return
        if error:
            self.preview_stats["errors"] += 1
            tile.status("预览暂不可用 · 点击该视角使用原片播放")
            return
        tile.actual_ms = self.timeline.reference_time(camera, tile.interval.wall_at(actual))
        tile.frame_image = image
        tile.stack.setCurrentWidget(tile.frame_view)
        tile.stack.show()
        tile.scale_frame()
        tile.ready = False  # Deliberate: a preview cannot confirm ground truth.
        tile.precise_ms = None
        self.preview_stats["completed"] += 1
        self.frozen_previews.add((self.generation, camera, tile.asset_id))
        self._preview_status(tile)

    def _observe(self, tile, now):
        if tile.pending and tile.pending["phase"] == "decoder_queue":
            return
        if tile in self.tiles.values() and self.is_preview(tile.camera):
            self._preview_status(tile)
            return
        super()._observe(tile, now)

    def evidence(self):
        return [item for item in super().evidence() if not self.is_preview(item["camera"])]

    def close(self):
        self.decoder_timer.stop()
        self.decoder_queue.clear()
        self.precise_queue.clear()
        self.frame_cache.clear()
        return super().close()
