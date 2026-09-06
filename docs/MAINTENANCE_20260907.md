# Four-repository maintenance · 2026-09-07

The recognition repository retains its name, history, package API and model contents. The bilingual READMEs now identify the system hub and private decision repository. Existing `cowmata/daily.py` remains a legacy research interface pending an explicit migration.

## Windows checkout repair

The baseline Git tree contained `weights\r` (an actual trailing carriage-return character). Windows rejected this path during clone checkout. The three entries were renamed to the documented `weights/` directory without changing their blob contents.

Both binary SHA-256 hashes match `weights/MANIFEST.json`. This is a directory repair, not model retraining or checkpoint promotion.

## Local verification

Windows; isolated Python 3.13.3 environment installed from the repository's declared dependencies. PyTorch 2.14.0+cpu; CUDA was unavailable in this environment.

- `python tests/test_contracts.py`: 36/36 passed.
- `python tests/test_torch_contracts.py`: 8/8 passed on CPU, none skipped.
- `python -m pytest tests/test_pipelines.py -q`: all 12 tests passed.
- `python -m cowmata predict --cache-key demo_session_60s --data-root examples/demo_data --out runs/maintenance-demo`: succeeded; 120 dense points, model loaded from repaired path.
- `git diff --check`: passed.

The installed XGBoost emitted its legacy-serialization compatibility warning while loading the unchanged artifact; the demo completed. No GPU validation, full dataset retraining or new predictive-performance evaluation was performed. These checks establish the scope of this maintenance change only.
