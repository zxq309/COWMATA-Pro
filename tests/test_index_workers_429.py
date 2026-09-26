from concurrent.futures import ThreadPoolExecutor


def test_full_index_has_sixteen_bounded_read_lanes(monkeypatch):
    from cowmata_tailring.workspace.worker import IndexWorker

    monkeypatch.delenv("COWMATA_INDEX_WORKERS", raising=False)
    worker = IndexWorker.__new__(IndexWorker)
    worker._pool_shutdown = False
    worker.max_inflight = 16
    worker.inspect_pool = ThreadPoolExecutor(max_workers=1)
    worker.inspect_pool.shutdown(wait=True)
    worker._pool_shutdown = True
    pool = worker._ensure_pool()
    try:
        assert pool._max_workers == 16
        assert worker.max_inflight == 16
    finally:
        pool.shutdown(wait=True)


def test_full_index_worker_override_is_bounded(monkeypatch):
    from cowmata_tailring.workspace.worker import _index_workers

    monkeypatch.setenv("COWMATA_INDEX_WORKERS", "99")
    assert _index_workers() == 16
    monkeypatch.setenv("COWMATA_INDEX_WORKERS", "0")
    assert _index_workers() == 1
    monkeypatch.setenv("COWMATA_INDEX_WORKERS", "bad")
    assert _index_workers() == 16
