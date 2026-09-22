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


def pipelined_records(rows, stage_one, finish_one, workers, check_cancel, *, ahead=4, order_key=None):
    """One platter reader stages ahead while `workers` converters run.

    The reader walks `rows` in `order_key` order (disk-friendly) and stages
    each segment; finished stagings are handed to the converter pool. Yielded
    futures raise the stage error when staging failed, matching the
    prepared_records contract.
    """
    import time
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    ordered = sorted(rows, key=order_key) if order_key else list(rows)
    source = iter(ordered)
    reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dahua-read")
    finish = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="dahua-view")

    def finish_wrapper(row, staged):
        staged.result()  # surface staging errors through the finish future
        return finish_one(row)

    staged = {}
    finishing = {}
    try:
        def fill_stage():
            while len(staged) < ahead:
                row = next(source, None)
                if row is None:
                    break
                check_cancel()
                staged[reader.submit(stage_one, row)] = row

        fill_stage()
        while staged or finishing:
            check_cancel()
            progressed = False
            for future in [f for f in list(staged) if f.done()]:
                row = staged.pop(future)
                finishing[finish.submit(finish_wrapper, row, future)] = row
                progressed = True
            fill_stage()
            done, _ = wait(finishing, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                row = finishing.pop(future)
                yield row, future
                progressed = True
            if not progressed:
                time.sleep(0.05)
    finally:
        for future in list(staged) + list(finishing):
            future.cancel()
        reader.shutdown(wait=False, cancel_futures=True)
        finish.shutdown(wait=False, cancel_futures=True)
