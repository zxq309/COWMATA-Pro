"""Fit a research model from a verified mother dataset's training-cow fold."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from cowmata_tailring.media.subprocess_tools import run_cancellable
from cowmata_tailring.workspace.event_models import available_packs, verify_model
from cowmata_tailring.workspace.mother_dataset import read_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--head',choices=['起立','卧倒','排尿','抬尾','甩尾'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    read_dataset(args.dataset)
    pack = available_packs(root)[0]
    model = next(m for m in pack['models'] if m['title']==args.head)
    runtime = verify_model(pack,model)
    with tempfile.TemporaryDirectory(prefix='cowmata-train-') as temp:
        request = Path(temp)/'request.json'
        request.write_text(json.dumps(dict(dataset=str(args.dataset.resolve()),head=args.head,
            output=str(args.output.resolve()),pack=str(pack['root'])),ensure_ascii=False),encoding='utf-8')
        command = [str(runtime),'-I','-B',str(root/'cowmata_tailring/workspace/event_worker.py'),
                   str(root/'cowmata_tailring/workspace/dataset_train_worker.py'),str(request)]
        result = run_cancellable(command,timeout=43200)
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
