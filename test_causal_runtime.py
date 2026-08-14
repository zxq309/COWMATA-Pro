from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch


RUNTIME = Path(__file__).resolve().parent / "model_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from imu_behavior.causal_model import CausalMultiTaskTCN  # noqa: E402
from imu_behavior.inference import predict_imu  # noqa: E402
from imu_behavior.schema import EVENT_CLASSES  # noqa: E402


class CausalRuntimeIntegrationTests(unittest.TestCase):
    def test_dispatches_current_checkpoint_and_runs_gap_safe_v2_inference(self) -> None:
        model = CausalMultiTaskTCN(in_channels=8, width=4, dropout=0.0)
        frame_dtype = np.dtype(
            [("elapsed_ms", "<u4"), ("values", "<i2", (9,))], align=False
        )
        frames = np.zeros(300, dtype=frame_dtype)
        frames["elapsed_ms"] = np.arange(len(frames), dtype=np.uint32) * 20
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_path = root / "causal.pt"
            imu_path = root / "sample.json"
            torch.save(
                {
                    "model_class": "CausalMultiTaskTCN",
                    "model_kwargs": {
                        "in_channels": 8,
                        "event_codes": list(EVENT_CLASSES),
                        "sample_rate_hz": 50,
                        "width": 4,
                        "dropout": 0.0,
                    },
                    "model_state": model.state_dict(),
                    "feature_indices": [0, 1, 2, 3, 4, 5, 9, 10],
                    "feature_statistics": {
                        "mean": [0.0] * 13,
                        "std": [1.0] * 13,
                    },
                    "thresholds": {
                        "WALKING": 0.5,
                        **{code: 0.5 for code in EVENT_CLASSES},
                    },
                    "sensor_calibration": {
                        "acc_divisor": 4096.0,
                        "acc_bias_counts": [0.0, 0.0, 0.0],
                        "gyro_divisor": 32.0,
                        "gyro_bias_counts": [0.0, 0.0, 0.0],
                        "mag_divisor": 1000.0,
                    },
                    "output_stride": 25,
                },
                checkpoint_path,
            )
            imu_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "device": "TEST",
                        "create_time": 1_700_000_000_000,
                        "imu": base64.b64encode(frames.tobytes()).decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )

            result = predict_imu(checkpoint_path, imu_path, device="cpu")

        self.assertEqual(result["algorithm"], "causal_multitask_tcn_v2")
        self.assertEqual(result["preprocessing"]["calibration_source"], "checkpoint:sensor_calibration")
        self.assertFalse(result["warnings"])
        self.assertNotIn(
            "FEEDING", {row["code"] for row in result["prediction_intervals"]}
        )


if __name__ == "__main__":
    unittest.main()
