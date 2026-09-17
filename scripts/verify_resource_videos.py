"""Inspect all supplied recordings with the same native clock/OCR code as GUI."""
import argparse
import json
import os
import shutil
import hashlib
import tempfile
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_tailring.workspace.catalog import digest_file, VIDEO_SUFFIXES
from cowmata_tailring.workspace.organization import identity
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.storage import atomic_json

_ocr = None
def inspect(args):
    global _ocr
    value, cache_path, original, sha, original_identity, recheck = args
    path, cache = Path(value), Path(cache_path)
    saved = cache / (sha + '.probe.json')
    try:
        if saved.exists() and not recheck:
            metadata = json.loads(saved.read_text(encoding='utf-8'))
        else:
            meta = cache / ('worker-' + str(os.getpid()))
            meta.mkdir(exist_ok=True)
            inspector = SourceInspector(path.parent, meta)
            inspector.ocr = _ocr
            old = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else {}
            native = old.get('timeline', {}).get('native')
            if recheck and native:
                metadata = inspector.native_video(path, sha, native,
                    {'width':old['width'],'height':old['height'],'codec_name':old['codec']},
                    {'format':{'format_name':old.get('format'),'duration':old.get('header_duration')}})
            else:
                metadata = inspector.video(path, sha)
            _ocr = inspector.ocr
            if identity(Path(original)) != original_identity:
                raise ValueError('Source changed during video verification')
            atomic_json(saved, metadata)
        record = {'source': original, 'sha256': sha, 'verified': not metadata.get('needs_review'),
                'intervals': metadata.get('intervals'), 'samples': len(metadata.get('samples', [])),
                'time_basis': metadata.get('time_basis'), 'warnings': metadata.get('warnings')}
        if identity(Path(original)) == original_identity:
            atomic_json(cache / (hashlib.sha256(original.encode()).hexdigest()+'.source.json'),
                        {'identity': original_identity, 'sha256': sha})
        return record
    except Exception as exc:
        return {'source': original, 'sha256': sha, 'error': str(exc), 'verified': False}
    finally:
        if str(path) != original:
            path.unlink(missing_ok=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('roots', nargs='+')
    parser.add_argument('--cache', required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--recheck', action='store_true', help='Recheck failed cached OCR; preserve the complete first-pass inventory')
    args = parser.parse_args()
    cache = Path(args.cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    files = [p for root in args.roots for p in Path(root).resolve().rglob('*') if p.is_file()
             and p.suffix.lower() in VIDEO_SUFFIXES and '标注工程' not in p.parts]
    results = []
    prior = {}
    old_report = cache / 'verification.jsonl'
    if old_report.exists():
        for line in old_report.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
                if row.get('verified') and (cache/(row['sha256']+'.probe.json')).exists():
                    prior[row['source']] = row
            except ValueError:
                pass
    if args.recheck:
        inventory = {r['source']:r for r in (json.loads(x) for x in old_report.read_text(encoding='utf-8').splitlines())}
        files = [p for p in files if str(p) in inventory and
                 (cache/(inventory[str(p)]['sha256']+'.probe.json')).exists() and
                 json.loads((cache/(inventory[str(p)]['sha256']+'.probe.json')).read_text(encoding='utf-8')).get('needs_review')]
        prior = {}
    print(f'videos={len(files)} workers={args.workers}', flush=True)
    with (cache / ('verification-recheck.jsonl' if args.recheck else 'verification.jsonl')).open('w', encoding='utf-8') as report:
        def emit(value):
            results.append(value)
            report.write(json.dumps(value, ensure_ascii=False)+'\n')
            report.flush()
            if len(results) % 10 == 0 or not value['verified']:
                print(f"{len(results)}/{len(files)} verified={sum(r['verified'] for r in results)} last={Path(value['source']).name} {value.get('error','')}", flush=True)
        with tempfile.TemporaryDirectory(prefix='cowmata-ocr-', dir=os.environ.get('TEMP')) as staging, ProcessPoolExecutor(max_workers=args.workers) as pool:
            pending = set()
            for number, path in enumerate(files):
                if str(path) in prior:
                    emit(prior[str(path)])
                    continue
                before = identity(path)
                snapshot = cache / (hashlib.sha256(str(path).encode()).hexdigest()+'.source.json')
                known = json.loads(snapshot.read_text(encoding='utf-8')) if snapshot.exists() else {}
                direct = (args.recheck and known.get('identity') == before and
                          known.get('sha256') == inventory[str(path)]['sha256'])
                if direct:
                    # Full bytes were already hashed and the native byte index
                    # is content-bound. Read only the exact frame windows.
                    pending.add(pool.submit(inspect, (str(path), str(cache), str(path), known['sha256'], before, True)))
                    if len(pending) >= args.workers*2:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            emit(future.result())
                    continue
                staged = Path(staging) / f'{number:06d}{path.suffix}'
                sha = hashlib.sha256()
                with path.open('rb') as inp, staged.open('wb') as out:
                    while block := inp.read(4*1024*1024):
                        sha.update(block)
                        out.write(block)
                if identity(path) != before:
                    raise ValueError('Source changed: '+str(path))
                shutil.copystat(path, staged)
                pending.add(pool.submit(inspect, (str(staged), str(cache), str(path), sha.hexdigest(), before, args.recheck)))
                if len(pending) >= args.workers*2:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        emit(future.result())
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    emit(future.result())
    atomic_json(cache / ('verification-recheck.json' if args.recheck else 'verification.json'), results)

if __name__ == '__main__':
    main()
