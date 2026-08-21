# Model-assisted annotation

The tool reads source nine-axis JSON, video and model artefacts. It never modifies training data or
training results. **Every model output must be confirmed by a human before it enters the annotation
table.**

Training, evaluation, and the current algorithm engineering baseline are maintained in the
[COWMATA Tail-Sensor Intelligence repository](https://github.com/zxq309/cowmata-tailring).

## Model package contract

Point the application at a directory via **Model assist → Model package settings…**:

| File | Required | Role |
| --- | --- | --- |
| `gbdt_full.joblib` | yes | Six event classes; also the posture/walking fallback |
| `best.pt` | no | `OfflineMultiTaskTCN` taking over standing, lying and walking |
| `inference_config.json` | no | Global sensor scaling overrides for JSON outside the package |

`xgboost` must be exactly `3.2.0`; the GBDT artefact will not deserialise otherwise.

With only the GBDT present, prediction still runs and the review panel states plainly that it is
waiting for `best.pt`. The fallback is never presented as a finished deep model.

## How labels are produced

1. The deep model handles the mutually exclusive postures `UPRIGHT` / `LYING` and an independent walking probability. The six event classes always come from the 104-dimension offline hand-crafted feature GBDT.
2. When the deep model is unavailable, `POSTURE_LYING` / `WALKING` from `gbdt_full.joblib` take over body behaviour.
3. No extra state machine is introduced: posture is whichever of `UPRIGHT` / `LYING` scores higher, and `WALKING >= 0.5` overrides only the upright state, producing mutually exclusive standing, lying and walking spans.
4. The six event classes are tail-raised, tail-wagging, standing-up, lying-down, urination and defecation, all strictly at probability `>= 0.5`. Adjacent positive 2 Hz points split into separate candidates only when more than 5000 ms apart. No minimum duration, hysteresis or per-class rules are added.
5. The 50 Hz cache, 104-dimension features and deep context follow the segments in the cache exactly. Candidate boundaries run from the first positive centre to the last positive centre plus 500 ms.
6. Event thresholds inside `best.pt` and the legacy post-processing configuration take no part in generating the six GBDT candidates.
7. Tail-wagging has very few training events and is treated as a research candidate: unchecked by default, and still requiring video confirmation.
8. Feeding and "other" are not model labels. Straining and calving markers remain manual only.

## Review workflow

Predictions land in a review queue, never directly in the annotation table.

1. Run **Model assist → Run model prediction** on the currently open source JSON.
2. Tick the suggestions worth keeping and choose **Import selected suggestions**. Imported rows are marked **Pending review**.
3. Double-click a row to seek to its start time. Select a row to edit its label and boundaries, either through the input fields or by dragging the edges on the waveform.
4. Approve each entry once the video confirms it. Edited entries return to **Modified** and must be approved again.

Original model classes and boundaries are preserved in traceability fields whenever an entry is
edited, so a human correction never erases what the model actually predicted.
