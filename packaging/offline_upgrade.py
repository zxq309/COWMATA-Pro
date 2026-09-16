"""Embedded installer bridge for clients whose original update feed is obsolete.

Runs under the installer's private stdlib, outside the existing application.
Uses exactly the same manifest checks, process checks and rollback transaction
as online updates. No dependency on the installed client's Python modules.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path

try:
    import update_worker as worker
except ImportError:  # Source-tree tests.
    from cowmata_tailring.app import update_worker as worker


def prepare(root, setup, cache, metadata, desktop):
    root, setup = worker.safe_path(root), worker.safe_path(setup, allow_file=True)
    if setup.is_relative_to(root):
        raise ValueError('请把安装包放在软件目录之外，再运行安装包。')
    if not worker.is_product_installation(root):
        raise ValueError('不是有效的 COWMATA 安装目录，未覆盖任何文件。')
    portable = not (root/'COWMATA.install-id').exists()
    old = (worker.installation_version(root) if portable
           else (root/'COWMATA.install-id').read_text(encoding='utf-8').strip().removeprefix('COWMATA-'))
    if not portable and not worker.registered(root, old):
        raise ValueError('所选安装版的注册信息与目录不一致，请选择原安装位置。')
    if worker.version_key(metadata['version']) <= worker.version_key(old):
        raise ValueError('已安装相同或更新版本，无需重复安装。')
    worker.inventory(root)
    cache = Path(cache).resolve()
    if cache.is_relative_to(root):
        raise ValueError('更新记录必须保存在软件目录之外。')
    job_dir = cache/('offline-'+uuid.uuid4().hex)
    job_dir.mkdir(parents=True)
    update = dict(metadata, size=setup.stat().st_size, sha256=worker.digest(setup))
    job = dict(root=str(root), setup=str(setup), job_dir=str(job_dir), update=update, desktop=bool(desktop),
               portable_origin=portable)
    worker.write_json(job_dir/'job.json', job)
    return job


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--setup', required=True)
    parser.add_argument('--desktop', choices=('0', '1'), default='1')
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    cache = (Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'COWMATA Annotator/updates').resolve()
    job = None
    try:
        metadata = json.loads((bundle/'upgrade-metadata.json').read_text(encoding='utf-8'))
        job = prepare(args.root, args.setup, cache, metadata, args.desktop == '1')
        state = Path(job['job_dir'])/'result.json'
        worker.write_json(state, dict(phase='preparing'))
        progress = bundle/'COWMATA-Progress.exe'
        if os.name == 'nt' and progress.is_file():
            subprocess.Popen([str(progress), '--update-progress', str(state), str(os.getpid())],
                             cwd=bundle, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        worker.install(job, restart=False)
        print('升级完成。', flush=True)
        return 0
    except Exception as exc:
        message = str(exc)
        if job:
            state = Path(job['job_dir'])/'result.json'
            try:
                result = json.loads(state.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                result = {}
            result.update(phase='failed', error=message)
            worker.write_json(state, result)
        print(message, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
