"""Isolated dataset jobs; raw sources and existing training sets are never edited."""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cowmata_tailring.workspace.storage import atomic_json


def main():
    job = Path(sys.argv[1]).resolve(strict=True)
    request = json.loads((job/'request.json').read_text(encoding='utf-8'))
    def cancelled():
        return (job/'cancel').exists()
    def progress(current, total, path):
        print(json.dumps({'current':current,'total':total,'path':path},ensure_ascii=True),flush=True)
    try:
        if request['action'] in {'dataset_audit','behavior_build','decision_build'}:
            from cowmata_tailring.workspace.behavior_dataset import (
                audit_annotations,
                build_behavior_dataset,
                build_decision_dataset,
            )
            if request['action']=='dataset_audit':
                result=audit_annotations(request['sources'],progress=progress,cancelled=cancelled)
            elif request['action']=='behavior_build':
                result=build_behavior_dataset(request['sources'],request['target'],behaviors=request.get('behaviors'),
                    native_features=request.get('native_features',False),progress=progress,cancelled=cancelled)
            else:
                result=build_decision_dataset(request['sources'],request['target'],progress=progress,cancelled=cancelled)
        elif request['action'] == 'legacy_preview':
            from cowmata_tailring.workspace.legacy_migration import plan_migration
            result = plan_migration(request['labels'],request['raw'],request['target'],category=request['category'],
                identity_policy='review',progress=progress,cancelled=cancelled)
            atomic_json(job/'plan.json',result)
            result = {'action':'legacy_preview','plan':str(job/'plan.json'),'summary':result['summary']}
        elif request['action'] == 'legacy_execute':
            from cowmata_tailring.workspace.legacy_migration import execute_migration
            result = execute_migration(json.loads(Path(request['plan']).read_text(encoding='utf-8')),
                                       progress=progress,cancelled=cancelled)
        elif request['action'] == 'dataset_export':
            from cowmata_tailring.workspace.dataset_pipeline import build_algorithm_datasets
            result = build_algorithm_datasets(request['sources'],request['target'],historical=request.get('historical'),
                allow_proxy=request.get('allow_proxy',False),progress=progress,cancelled=cancelled)
        else:
            raise ValueError('Unknown dataset operation')
        atomic_json(job/'result.json',result)
        return 0
    except Exception as exc:
        atomic_json(job/'error.json',{'error':str(exc),'paused':isinstance(exc,InterruptedError)})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
