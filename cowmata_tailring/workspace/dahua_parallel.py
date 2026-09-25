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


def pipelined_records(rows, stage_one, finish_one, workers, check_cancel, *, ahead=8, order_key=None,
                      stage_batch=None, batch_size=None):
    """Ordered source staging plus parallel converters, with real backpressure.

    At most ``workers + ahead`` rows are admitted at once, counting rows
    that are staging, staged but not converted, converting, and finished but
    not yet consumed. The previous scheduler only bounded rows *being* read,
    so the reader copied hundreds of GB ahead of the converters, evicted the
    OS file cache and starved conversions of disk bandwidth.

    ``stage_batch(rows, ready)`` (disk mode) stages several rows with one
    sequential sweep and calls ``ready(row)`` as each row becomes available;
    otherwise ``stage_one(row)`` runs per row on ``workers`` threads.
    """
    import queue as queue_mod
    import threading
    from concurrent.futures import ThreadPoolExecutor

    ordered = sorted(rows, key=order_key) if order_key else list(rows)
    total = len(ordered)
    # Converters plus a read-ahead margin, so staging overlaps conversion.
    window = max(1, int(workers)) + max(0, int(ahead))
    results = queue_mod.Queue()
    stop_event = threading.Event()
    slots = threading.Semaphore(window)
    finish_pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="dahua-view")

    def deliver(row, future):
        results.put((row, future))

    def submit_finish(row, staged_error=None):
        def run():
            if staged_error is not None:
                raise staged_error
            return finish_one(row)
        try:
            future = finish_pool.submit(run)
        except RuntimeError as exc:  # pool shut down while stopping
            results.put(("dispatcher", None, InterruptedError(str(exc))))
            return
        future.add_done_callback(lambda f, _row=row: deliver(_row, f))

    def admit():
        while not stop_event.is_set():
            if slots.acquire(timeout=0.1):
                return True
        return False

    def dispatcher():
        stage_pool = None
        try:
            if stage_batch is None:
                stage_pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="dahua-read")

                def staged(future, row):
                    if future.cancelled():
                        return
                    submit_finish(row, future.exception())

                for row in ordered:
                    if not admit():
                        return
                    check_cancel()
                    future = stage_pool.submit(stage_one, row)
                    future.add_done_callback(lambda f, _row=row: staged(f, _row))
            else:
                size = max(1, int(batch_size or workers))
                position = 0
                while position < total:
                    batch = []
                    while len(batch) < size and position < total:
                        if not admit():
                            return
                        batch.append(ordered[position])
                        position += 1
                    check_cancel()
                    submitted = set()
                    guard = threading.Lock()

                    def ready(row, _submitted=submitted, _guard=guard):
                        # Called from staging threads the moment a row is
                        # whole; every row is still submitted exactly once.
                        with _guard:
                            if id(row) in _submitted:
                                return
                            _submitted.add(id(row))
                        submit_finish(row)

                    stage_batch(batch, ready)
                    for row in batch:
                        ready(row)
        except BaseException as exc:
            results.put(("dispatcher", None, exc))
        finally:
            if stage_pool is not None:
                stage_pool.shutdown(wait=not stop_event.is_set(), cancel_futures=stop_event.is_set())

    thread = threading.Thread(target=dispatcher, daemon=True, name="dahua-dispatch")
    thread.start()
    yielded = 0
    try:
        while yielded < total:
            check_cancel()
            try:
                item = results.get(timeout=0.1)
            except queue_mod.Empty:
                continue
            if len(item) == 3 and item[0] == "dispatcher":
                raise item[2]
            row, future = item
            slots.release()
            yielded += 1
            yield row, future
    finally:
        stop_event.set()
        # Queued conversions must not start after a pause; running ones stop
        # through the shared cancellation callback. Join them so no converter
        # outlives the pause (the caller clears its stop flag afterwards).
        finish_pool.shutdown(wait=True, cancel_futures=True)
        thread.join(timeout=60)

