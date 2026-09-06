# Recognition quick start

## Quick start

### 1. Clone and create the environment

```bash
git clone https://github.com/zxq309/cowmata-tailring.git
cd cowmata-tailring
conda env create -f environment.yml
conda activate cowmata
```

Install the PyTorch build that matches the target machine from the [official PyTorch selector](https://pytorch.org/get-started/locally/), then install this project without replacing that build:

```bash
python -m pip install -e . --no-deps
# deep-learning host additionally:
python -m pip install -e ".[deep]"
```

### 2. Verify the clone

```bash
pytest tests/test_contracts.py tests/test_pipelines.py   # 48 tests, no torch needed
cowmata check-env --device cpu                            # works without torch
pytest tests/test_torch_contracts.py                      # model contracts, needs torch
```

### 3. Run the bundled 60-second demo

The demo does not require the private supervised cache:

```bash
cowmata predict \
  --cache-key demo_session_60s \
  --data-root examples/demo_data \
  --out runs/demo
```

Expected behavior:

- 120 dense prediction points at 2 Hz;
- two CSV files under `runs/demo/`;
- zero or more merged event candidates depending on the configured threshold.

## Python API

```python
from cowmata import COWMATA

model = COWMATA("weights/deploy/gbdt_full.joblib")
result = model.predict(
    "<cache_key>",
    project="runs/predict",
    threshold=0.5,
)

print(result.dense.head())
print(result.candidates)
print(result.dense_path)
```

The model object loads once and can predict multiple cached sessions without reloading the serialized bundle.

## CLI workflow

```bash
# Validate session metadata, labels, cache contracts, and cow-level splits.
cowmata check-data --root .

# Read every local cache array as a stronger integrity check.
cowmata check-data --full-cache-scan

# Write a structured dataset diagnostic report.
cowmata diagnose --out runs/diagnostics

# Cache footprint before you collect.
cowmata plan-storage --cows 200 --days 7

# Cow-grouped k-fold splits, cow-disjoint validation.
cowmata make-splits --folds 5

# Rebuild the schema-2 cache from raw JSON + labels.
cowmata build-cache --annotations ... --calibration-manifest ... --output-root ...

# Hand-crafted feature table (offline or causal windows).
cowmata build-features --samples ... --session-cache ... --out ... --offline

# GBDT bundle with per-event thresholds on a cow-disjoint split.
cowmata train-gbdt --feature-table ... --backend xgboost --device cuda

# Train the multi-stage temporal model on one fold.
cowmata train --labels ... --cache-root ... --splits ... --fold 1 --out runs/fold1

# Build a human review queue from dense predictions.
cowmata mine --predictions runs/... --events URINATION,MOUNTED_BY --out runs/review_01

# Check CPU or CUDA execution.
cowmata check-env --device cpu --precision fp32
```
