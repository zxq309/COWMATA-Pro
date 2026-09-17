"""Complete mother labels -> native event tables and continuous decision inputs."""
from __future__ import annotations

import json
import os
from pathlib import Path

from cowmata_tailring.media.subprocess_tools import run_cancellable

from .catalog import digest_file
from .event_models import available_packs, verify_model
from .mother_dataset import export_dataset
from .storage import atomic_json


def build_native_features(target,*,app_root=None,selected_codes=None,behavior_folders=None,progress=lambda *_:None,cancelled=lambda:False):
    app_root = Path(app_root or Path(__file__).resolve().parents[2])
    packs = available_packs(app_root)
    if not packs:
        raise ValueError('未安装已核验的事件算法包，请使用完整便携包')
    pack = packs[0]
    models=[m for m in pack['models'] if selected_codes is None or m['code'] in selected_codes]
    if not models:
        return []
    for model in models:
        runtime = verify_model(pack,model)
    target = Path(target)
    result=[]
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    for model in models:
        progress(0,5,'计算原生特征：'+model['title'])
        if behavior_folders:
            destination=target/'行为数据集'/behavior_folders[model['code']]
        else:
            destination=target/'事件识别'/model['title']
        request = destination/'build-request.json'
        atomic_json(request,dict(dataset=str(target.resolve()),head=model['title'],pack=str(pack['root']),output=str(destination.resolve())))
        command = [str(runtime),'-I','-B',str(Path(__file__).with_name('event_worker.py')),
                   str(Path(__file__).with_name('algorithm_dataset.py')),str(request)]
        run = run_cancellable(command,timeout=43200,cancelled=cancelled,env=env)
        (request.parent/'build.log').write_bytes(run.stdout+run.stderr)
        if run.returncode:
            raise ValueError(model['title']+' 特征导出失败，请查看 '+str(request.parent/'build.log'))
        result.append(json.loads((request.parent/'readiness.json').read_text(encoding='utf-8')))
    return result


def build_algorithm_datasets(sources,target,*,app_root=None,historical=None,allow_proxy=False,progress=lambda *_:None,cancelled=lambda:False):
    result = export_dataset(sources,target,progress=progress,cancelled=cancelled)
    target=Path(target)
    initial=json.loads((target/'dataset-manifest.json').read_text(encoding='utf-8'))
    initial['pipeline_complete']=False
    atomic_json(target/'dataset-manifest.json',initial,backup=False)
    result['algorithms']=build_native_features(target,app_root=app_root,progress=progress,cancelled=cancelled)
    if historical:
        pack=available_packs(app_root or Path(__file__).resolve().parents[2])[0]
        runtime=verify_model(pack,pack['models'][0])
        env={**os.environ,'PYTHONIOENCODING':'utf-8'}
        request = target/'历史算法对齐/build-request.json'
        atomic_json(request,dict(dataset=str(target.resolve()),historical=str(historical),pack=str(pack['root']),allow_proxy=allow_proxy))
        command = [str(runtime),'-I','-B',str(Path(__file__).with_name('event_worker.py')),
                   str(Path(__file__).with_name('legacy_dataset.py')),str(request)]
        run = run_cancellable(command,timeout=3600,cancelled=cancelled,env=env)
        (request.parent/'build.log').write_bytes(run.stdout+run.stderr)
        if run.returncode:
            raise ValueError('旧算法数据集对齐失败，请查看 '+str(request.parent/'build.log'))
        result['historical'] = json.loads((request.parent/'归一化结果.json').read_text(encoding='utf-8'))
        result['algorithms'] = [json.loads((target/'事件识别'/m['title']/'readiness.json').read_text(encoding='utf-8'))
                                for m in pack['models']]
    from .decision_dataset import export_decision
    result['decision'] = export_decision(target,progress=progress,cancelled=cancelled)
    manifest = json.loads((target/'dataset-manifest.json').read_text(encoding='utf-8'))
    manifest['files'] = [{'path':p.relative_to(target).as_posix(),'sha256':digest_file(p),'size':p.stat().st_size}
                         for p in sorted(target.rglob('*')) if p.is_file() and p.name not in {'dataset-manifest.json','dataset-manifest.json.bak'}]
    manifest.update(algorithm_results=result['algorithms'],decision_result=result['decision'],pipeline_complete=True)
    atomic_json(target/'dataset-manifest.json',manifest,backup=False)
    return result
