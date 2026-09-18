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
