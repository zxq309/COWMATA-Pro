"""Second pass on real OCR/segment failures; preserve the first observations."""
import json
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_tailring.workspace.probe import SourceInspector
from cowmata_tailring.workspace.resource_import import archive_bounds
from cowmata_tailring.workspace.storage import atomic_json

def main():
    cache = Path(sys.argv[1]).resolve()
    rows = [json.loads(line) for line in (cache/'verification.jsonl').read_text(encoding='utf-8').splitlines()]
    ocr = None
    results = []
    for row in rows:
        if len(sys.argv) > 2 and not any(row['sha256'].startswith(prefix) for prefix in sys.argv[2:]):
            continue
        saved = cache/(row['sha256']+'.probe.json')
        metadata = json.loads(saved.read_text(encoding='utf-8')) if saved.exists() else {}
        if metadata and not metadata.get('needs_review'):
            continue
        path = Path(row['source'])
        backup = cache/'first-pass'/saved.name
        if metadata and not backup.exists():
            atomic_json(backup, metadata)
        with tempfile.TemporaryDirectory(prefix='cowmata-recheck-') as folder:
            staged = Path(folder)/path.name
            shutil.copy2(path, staged)
            inspector = SourceInspector(staged.parent, Path(folder))
            inspector.ocr = ocr
            try:
                metadata = inspector.video(staged, row['sha256'])
                ocr = inspector.ocr
                atomic_json(saved, metadata)
                lo, hi, full = archive_bounds(staged, metadata)
                atomic_json(saved, metadata)
                result = {'source':str(path),'sha256':row['sha256'],'archive_start_verified':True,
                          'full_timeline_verified':full,'start_ms':lo,'end_ms':hi}
            except Exception as exc:
                result = {'source':str(path),'sha256':row['sha256'],'error':str(exc)}
        results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    atomic_json(cache/'second-pass.json',results)

if __name__=='__main__':
    main()
