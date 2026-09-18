"""Durable download checkpoints consumed by collaboration dispatch checks."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .core import CHINA
from .csv_targets import CsvPlan
from .settings import atomic_json


def merged_ranges(values):
    result = []
    for row in sorted(values, key=lambda r: (r['device'], r['start'], r['end'])):
        if result and row['device'] == result[-1]['device'] and row['start'] <= result[-1]['end']:
            result[-1]['end'] = max(result[-1]['end'], row['end'])
        else:
            result.append(dict(row))
    return result


@contextmanager
def record_cycle(job, plan, result):
    path = Path(job.farm) / '.edge-download/csv-cycle.json'
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    state = dict(schema='cowmata-download-cycle-v1', cycle_id=uuid4().hex, status='running',
                 ledger_directory=str(Path(job.ledger_directory).resolve()), sources=plan.sources,
                 start=job.start.isoformat(), end=job.end.isoformat(),
                 started_at=datetime.now(CHINA).isoformat(),
                 verified_ranges=previous.get('verified_ranges', []))
    atomic_json(path, state)
    success = False
    try:
        yield
        success = True
    finally:
        current = None
        try:
            current = CsvPlan(job.ledger_directory)
        except (OSError, ValueError):
            pass
        unchanged = current is not None and current.ready and current.sources == plan.sources
        state.update(saved=result.saved, skipped=result.skipped, failed=result.failed,
                     canceled=result.canceled, finished_at=datetime.now(CHINA).isoformat())
        state['status'] = ('canceled' if result.canceled else 'failed' if not success or result.failed
                           else 'outdated' if not unchanged else 'complete')
        if state['status'] == 'complete':
            ranges = [dict(device=device, start=lo.isoformat(), end=hi.isoformat())
                      for device, lo, hi in plan.bounds(job.start, job.end)]
            state['verified_ranges'] = merged_ranges([*state['verified_ranges'], *ranges])
        atomic_json(path, state)
