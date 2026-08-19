# COWMATA Tail-Ring Annotator

Offline desktop annotation tool for tail-worn nine-axis (accelerometer / gyroscope /
magnetometer) + temperature sensor data: video and waveforms share one playback head,
with interval/point event annotation, CSV/BORIS export, two-annotator blind IRR
consistency checks, and 20260816 model-assisted prediction (GBDT + deep model).

> The tool only reads raw nine-axis JSON, video and model files; it never modifies
> training data or training results. Every model output must pass human review before
> it can enter the annotation table.

## Launch and dependencies

Double-click `正式桌面版入口\【双击启动】模型辅助标注工具.bat` (model-assisted build).
Run `【安装依赖】模型辅助标注工具.bat` before the first use of the 20260816 model;
the GBDT bundle requires `xgboost==3.2.0`. Full operation and UI details are in
[`正式桌面版入口/使用说明.md`](正式桌面版入口/使用说明.md) (Chinese).

## 20260816 model bundle

"Model assist → Model bundle settings…" selects a directory with this contract:

- `gbdt_full.joblib`: required. Produces the six event classes and provides a
  posture/walking fallback while the deep model is unavailable.
- `best.pt`: optional. Drop the trained checkpoint into the same directory once
  available; it must be an `OfflineMultiTaskTCN`. The tool picks it up on the next
  prediction for standing / lying / walking with no further configuration.
- `inference_config.json`: optional, a global sensor-conversion override for raw JSON
  outside the bundle; event thresholds and candidate rules always follow the guide
  and are not overridable here.

The default directory is `预测\20260816\复现实验\final_model`. With only the GBDT
present the tool still predicts normally and the review window explicitly shows
"waiting for best.pt"; the GBDT fallback is never disguised as a finished deep model.

## Algorithm and label conventions

1. The deep model produces the mutually exclusive posture `UPRIGHT/LYING` and an
   independent walking probability; the six event classes always use the 104-dim
   offline hand-crafted GBDT.
2. While the deep model is unavailable, `POSTURE_LYING/WALKING` from
   `gbdt_full.joblib` take over body behaviour automatically.
3. No state machine is added: posture takes the higher of `UPRIGHT/LYING`
   probabilities; `WALKING >= 0.5` only overrides the upright state, yielding
   mutually exclusive standing / lying / walking display intervals.
4. The six event classes are tail-raised, tail-wagging, standing-up, lying-down,
   urination and defecation, all strictly `probability >= 0.5`; adjacent positive
   2 Hz points split into two candidates only when more than 5000 ms apart. No
   minimum duration, hysteresis or per-class rules are added.
5. The 50 Hz cache, 104-dim features and deep context respect the cache `segments`
   strictly; candidate boundaries use the guide script's `first positive centre` to
   `last positive centre + 500 ms`.
6. Event thresholds inside `best.pt` and the original tool's post-processing
   configuration do not participate in the six-class GBDT candidate generation.
7. Per the guide, tail-wagging has only 4 events on 1 cow; it is a research candidate
   and unchecked by default. All candidates still require video confirmation.
8. Feeding and other behaviours are not model labels; straining and calving nodes
   remain manual-only annotations.

## Arbitrary raw JSON

Prediction accepts the currently opened V2 raw JSON directly. The tool first maps the
device MAC and session name to the guide's `cache_key`: when found it reads the
corresponding `features.npy/metadata.json` directly, so the input is byte-for-byte the
official `predict_full.py` input; only when not found does it build the equivalent
50 Hz, 13-channel input for a new session per guide section 6.

Processing reuses the reproducible interfaces of the current algorithm: 22-byte frame
phase recovery, time-reset/gap detection, per-segment 50 Hz resampling, magnetometer
coordinate rotation, 13-channel physical values, per-session mounting-pose
self-calibration, 104-dim offline features and 2 Hz decision points. Sensor conversion
uses the fixed unified parameters below, independent of any calibration manifest not
included in the delivery material.

Production devices share one firmware and one sensor parameter set; no per-device bias
is stored. The tool therefore no longer branches on MAC — every existing device,
future device, and out-of-bundle V2 JSON without a MAC uses:

- accelerometer divisor `4096`, bias `[0, 0, 0]`;
- gyroscope divisor `32`, bias `[0, 0, 0]`;
- magnetometer divisor `1000`, with the coordinate rotation defined by the current
  algorithm.

MAC serves only as a data-source identifier. Mounting angle continues to be handled by
the algorithm's low-motion session self-calibration. A bundle may override sensor
parameters only via one global `sensor_calibration` block in `inference_config.json`,
applied to all devices alike.

## Human review workflow

1. Open a real nine-axis JSON, then open and calibrate the synchronised video if needed.
2. Click "Model assist → Start model prediction".
3. In the review window, sort by confidence and locate the video; adjust labels or
   boundaries first if necessary.
4. Import only trustworthy suggestions; imported items are uniformly marked
   "pending human review".
5. Select a prediction in the event table to drag its left/right boundary on the real
   waveform; `Ctrl+E` refocuses.
6. Correct or delete false positives against the video, then click "confirm reviewed".
7. Save the project and export training samples as usual. Unconfirmed candidates must
   never serve as ground truth.

Each prediction also writes the guide-mandated `*_dense.csv` and `*_candidates.csv`
into `prediction_cache`, together with an annotation-tool prediction summary. The
record includes the source model directory, model state, GBDT/deep weight hashes,
algorithm version, fixed thresholds, input cache_key, confidence and the history of
manual adjustments. Adding or replacing `best.pt`, or changing the global sensor
parameters, changes the model fingerprint and never overwrites older results.

## Rollback

The pre-migration Git tag is `before-20260816-hybrid-interface`. The legacy 20260815
causal runtime remains under `model_runtime/imu_behavior`, but the current interface
selects only the 20260816 bundle by default.
