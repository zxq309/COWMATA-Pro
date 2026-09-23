"""Bounded preparation across views; destination commits stay on the owner thread."""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def fair_records(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["group"], deque()).append(row)
    while groups:
        for group in list(groups):
            yield groups[group].popleft()
            if not groups[group]:
                del groups[group]


def prepared_records(rows, prepare, workers, check_cancel):
    """At most workers media caches exist; drain fast views without head-of-line wait."""
    ordered = iter(fair_records(rows) if workers > 1 else rows)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dahua-view")
    pending = {}
    from .dahua_media import decoder_budget, verification_threads
    per_worker_threads = max(1, verification_threads() // workers)
    def run(row):
        with decoder_budget(per_worker_threads):
            return prepare(row)
    try:
        def fill():
            while len(pending) < workers:
                row = next(ordered, None)
                if row is None:
                    break
                check_cancel()
                pending[pool.submit(run, row)] = row
        fill()
        while pending:
            check_cancel()
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                row = pending.pop(future)
                yield row, future
                fill()
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def pipelined_records(rows, stage_one, finish_one, workers, check_cancel, *, ahead=8, order_key=None):
    """Single platter-order reader + parallel converters, always fed.

    A dispatcher thread walks `rows` in `order_key` order, keeping `ahead`
    segments staged and submitting every staged segment to the converter
    pool. Completed results return through a queue, so the consumer can
    block on any single conversion without stalling the reader or the
    other converters (the generator form starved the pipeline after
    `ahead` segments and capped throughput near 4 segments per conversion
    time).
    """
    import queue as queue_mod
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    ordered = sorted(rows, key=order_key) if order_key else list(rows)
    source = iter(ordered)
    results = queue_mod.Queue()
    stop_event = threading.Event()
    lock = threading.Lock()
    inflight = {"stage": 0, "finish": 0}

    import functools

    def tracked(pool, fn, row):
        kind = "stage" if fn is stage_one else "finish"

        def done(future):
            with lock:
                inflight[kind] -= 1
            results.put((row, future))

        with lock:
            inflight[kind] += 1
        return pool.submit(fn, row).add_done_callback(done)

    def dispatcher():
        stage_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dahua-read")
        finish_pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="dahua-view")
        try:
            staged = {}
            exhausted = False
            while not stop_event.is_set():
                check_cancel()
                for future in [f for f in list(staged) if f.done()]:
                    row = staged.pop(future)
                    def run_finish(_row, _f=future, _row_key=row):
                        return finish_wrapper(_row_key, _f)

                    tracked(finish_pool, run_finish, row)
                while len(staged) < ahead and not exhausted and not stop_event.is_set():
                    row = next(source, None)
                    if row is None:
                        exhausted = True
                        break
                    staged[stage_pool.submit(stage_one, row)] = row
                if exhausted and not staged:
                    break
                time.sleep(0.03)
        except Exception:
            pass  # cancel/close: consumer sees pending futures resolve or the stop flag
        finally:
            stage_pool.shutdown(wait=False)
            finish_pool.shutdown(wait=False)

    def finish_wrapper(row, staged_future):
        staged_future.result()  # surface staging errors through the finish future
        return finish_one(row)

    thread = threading.Thread(target=dispatcher, daemon=True, name="dahua-dispatch")
    thread.start()
    try:
        while True:
            check_cancel()
            try:
                row, future = results.get(timeout=0.1)
            except queue_mod.Empty:
                if not thread.is_alive():
                    return
                continue
            yield row, future
    finally:
        stop_event.set()
        # Give in-flight conversions a grace period to land their results.
        deadline = time.monotonic() + 5
        while not results.empty() and time.monotonic() < deadline:
            time.sleep(0.05)


