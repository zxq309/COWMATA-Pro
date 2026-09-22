"""4.2.1: pipelined disk-mode scheduler (single reader + parallel converters)."""
import threading
import time

from cowmata_tailring.workspace.dahua_parallel import pipelined_records


def test_pipeline_overlaps_reader_with_converters():
    lock = threading.Lock()
    state = {"reading": 0, "max_reads": 0, "converting_while_reading": 0}
    rows = [dict(id=f"{v}{i:02}", partition=0, descriptor=i) for v in "ABCD" for i in range(1, 6)]

    def stage_one(row):
        with lock:
            state["reading"] += 1
            state["max_reads"] = max(state["max_reads"], state["reading"])
        time.sleep(0.05)  # platter time
        with lock:
            state["reading"] -= 1
        return row["id"]

    def finish_one(row):
        started = time.perf_counter()
        with lock:
            if state["reading"] > 0:
                state["converting_while_reading"] += 1
        time.sleep(0.10)  # converter time
        return dict(id=row["id"], done=True, waited=time.perf_counter() - started)

    results = []
    for row, future in pipelined_records(rows, stage_one, finish_one, workers=6,
                                         check_cancel=lambda: False, ahead=4):
        results.append(future.result()["id"])
    assert sorted(results) == sorted(r["id"] for r in rows)
    assert state["max_reads"] == 1, "platter reads must stay serialized"
    assert state["converting_while_reading"] > 0, "converters must overlap the reader"


def test_pipeline_stages_in_disk_order_and_bounds_ahead():
    started = []
    lock = threading.Lock()
    rows = [dict(id=f"r{i:02}", partition=0, descriptor=i) for i in range(12)]

    def stage_one(row):
        with lock:
            started.append(row["id"])
        time.sleep(0.02)
        return row["id"]

    def finish_one(row):
        time.sleep(0.01)
        return row["id"]

    consumed = 0
    for row, future in pipelined_records(rows, stage_one, finish_one, workers=4,
                                         check_cancel=lambda: False, ahead=3,
                                         order_key=lambda r: r["descriptor"]):
        future.result()
        consumed += 1
        if consumed == 1:
            # reader stays only `ahead` segments ahead of the consumer
            with lock:
                assert len(started) <= 5, f"reader ran too far ahead: {started}"
    assert consumed == len(rows)
    assert started == [r["id"] for r in rows], "staging must follow disk order"


def test_pipeline_surfaces_staging_errors():
    rows = [dict(id="good1", partition=0, descriptor=0),
            dict(id="bad", partition=0, descriptor=1),
            dict(id="good2", partition=0, descriptor=2)]

    def stage_one(row):
        if row["id"] == "bad":
            raise ValueError("原盘链路损坏")
        return row["id"]

    def finish_one(row):
        return dict(id=row["id"], ok=True)

    outcomes = {}
    errors = {}
    for row, future in pipelined_records(rows, stage_one, finish_one, workers=2,
                                         check_cancel=lambda: False):
        try:
            outcomes[row["id"]] = future.result()
        except ValueError as exc:
            errors[row["id"]] = str(exc)
    assert outcomes["good1"] == dict(id="good1", ok=True)
    assert outcomes["good2"] == dict(id="good2", ok=True)
    assert "原盘链路损坏" in errors["bad"]
