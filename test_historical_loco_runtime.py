from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import torch


APP_DIR = Path(__file__).resolve().parent
WORKSPACE = APP_DIR.parent
RUNTIME = APP_DIR / "model_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from imu_behavior.checkpoint import inspect_checkpoint  # noqa: E402
from imu_behavior.historical_model import HistoricalMultiTaskResTCN  # noqa: E402
from imu_behavior.inference import predict_imu  # noqa: E402


MODELS = WORKSPACE / "03_训练结果" / "models_codex"


class HistoricalLocoRuntimeTests(unittest.TestCase):
    def test_all_delivered_supervised_weights_safe_load_strictly(self) -> None:
        paths = sorted(MODELS.glob("loco_*/**/best.pt"))
        self.assertEqual(len(paths), 7)
        for path in paths:
            with self.subTest(path=path):
                _, payload = inspect_checkpoint(path)
                self.assertEqual(payload["model_class"], "MultiTaskResTCN")
                state = payload["model_state"]
                in_channels = int(state["encoder.0.weight"].shape[1])
                model = HistoricalMultiTaskResTCN(in_channels)
                model.load_state_dict(state, strict=True)

    def test_reconstructed_architecture_matches_delivered_predictions(self) -> None:
        run = (
            MODELS
            / "loco_v2_domain_robust"
            / "fold_3_23335-7_20260813_225720"
        )
        _, checkpoint = inspect_checkpoint(run / "best.pt")
        folds = json.loads(
            (
                WORKSPACE
                / "01_关键训练数据"
                / "loco_splits"
                / "loco_splits.json"
            ).read_text(encoding="utf-8")
        )
        fold = next(item for item in folds["folds"] if int(item["fold"]) == 3)
        sample = None
        with (
            WORKSPACE / "01_关键训练数据" / "supervised_cache" / "samples.csv"
        ).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["sample_id"] == "SMP-0000001":
                    sample = row
                    break
        self.assertIsNotNone(sample)
        features = np.load(
            WORKSPACE
            / "01_关键训练数据"
            / "supervised_cache"
            / "session_cache"
            / sample["cache_key"]
            / "features.npy",
            mmap_mode="r",
        )
        center = int(sample["center_index"])
        raw = np.asarray(features[center - 128 : center + 128], dtype=np.float32)
        mean = np.asarray(fold["normalization"]["mean"], dtype=np.float32)
        std = np.asarray(fold["normalization"]["std"], dtype=np.float32)
        indices = np.asarray(checkpoint["feature_indices"], dtype=np.int64)
        inputs = torch.from_numpy(
            np.ascontiguousarray((((raw - mean) / std)[:, indices]).T)
        ).unsqueeze(0)
        model = HistoricalMultiTaskResTCN(len(indices))
        model.load_state_dict(checkpoint["model_state"], strict=True)
        model.eval()
        with torch.inference_mode():
            output = model(inputs)
        actual = np.concatenate(
            (
                torch.softmax(output["body_logits"], 1).numpy()[0],
                torch.sigmoid(output["event_logits"]).numpy()[0],
            )
        )
        expected = np.asarray(
            [
                6.475602e-10,
                1.0,
                1.5716068e-08,
                1.09139364e-10,
                1.0,
                1.0,
                2.4318695e-05,
                1.0408341e-15,
                6.565415e-12,
                5.699694e-07,
            ],
            dtype=np.float32,
        )
        np.testing.assert_allclose(actual, expected, rtol=0.05, atol=1e-5)

    def test_ssl_weight_reports_that_it_has_no_prediction_head(self) -> None:
        ssl = MODELS / "ssl_v1" / "20260813_224414" / "best.pt"
        with self.assertRaisesRegex(ValueError, "只有编码器"):
            predict_imu(ssl, Path("unused.json"), device="cpu")


if __name__ == "__main__":
    unittest.main()
