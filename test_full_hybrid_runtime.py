from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch


RUNTIME = Path(__file__).resolve().parent / "model_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from imu_behavior.full_features import segment_features  # noqa: E402
from imu_behavior.full_inference import (  # noqa: E402
    ALGORITHM_VERSION,
    FULL_EVENT_CODES,
    REQUIRED_GBDT_TASKS,
    _find_guide_cache,
    _load_guide_cache,
    inspect_full_model_package,
)
from imu_behavior.inference import predict_imu  # noqa: E402
from imu_behavior.offline_model import OfflineMultiTaskTCN  # noqa: E402
from imu_behavior.postprocess import (  # noqa: E402
    postprocess_hierarchical_predictions,
    postprocess_predict_full_guide,
)
from imu_behavior.schema import EVENT_CLASSES  # noqa: E402


FRAME_DTYPE = np.dtype(
    [("elapsed_ms", "<u4"), ("values", "<i2", (9,))], align=False
)


class ConstantProbabilityModel:
    def __init__(self, probability: float) -> None:
        self.probability = float(probability)

    def predict_proba(self, values: np.ndarray) -> np.ndarray:
        return np.full(len(values), self.probability, dtype=np.float32)


def feature_names() -> list[str]:
    array = np.zeros((300, 13), dtype=np.float32)
    array[:, 2] = 1.0
    array[:, 9] = 1.0
    return list(
        segment_features(
            array,
            np.arange(0, len(array), 25),
            causal=False,
        ).columns
    )


class FullHybridRuntimeTests(unittest.TestCase):
    def test_offline_feature_contract_contains_exactly_104_columns(self) -> None:
        names = feature_names()
        self.assertEqual(len(names), 104)
        self.assertEqual(len(set(names)), 104)
        self.assertEqual(names[0], "tilt_mean_1s")
        self.assertEqual(names[-2:], ["segment_position", "segment_length"])

    def test_package_accepts_gbdt_fallback_then_offline_best_pt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gbdt_full.joblib").write_bytes(b"placeholder")
            waiting = inspect_full_model_package(root)
            self.assertEqual(waiting.deep_status, "waiting_for_training")
            self.assertIsNone(waiting.deep_path)

            model = OfflineMultiTaskTCN(
                in_channels=8,
                event_codes=EVENT_CLASSES,
                sample_rate_hz=50,
                width=4,
                dropout=0.0,
            )
            torch.save(
                {
                    "model_class": "OfflineMultiTaskTCN",
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
                    "context_samples": 256,
                    "thresholds": {"WALKING": 0.5},
                },
                root / "best.pt",
            )
            ready = inspect_full_model_package(root)
            self.assertEqual(ready.deep_status, "ready")
            self.assertEqual(ready.deep_model_class, "OfflineMultiTaskTCN")

    def test_raw_json_runs_gbdt_fallback_without_training_cache(self) -> None:
        frames = np.zeros(400, dtype=FRAME_DTYPE)
        frames["elapsed_ms"] = np.arange(len(frames), dtype=np.uint32) * 20
        # Shared production scale with roughly +1 g on Z.
        frames["values"][:, 2] = 4096
        names = feature_names()
        models = {
            task: ConstantProbabilityModel(
                0.20
                if task == "POSTURE_LYING"
                else 0.05
                if task == "WALKING"
                else 0.0
            )
            for task in REQUIRED_GBDT_TASKS
        }
        bundle = {"features": names, "models": models}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gbdt_full.joblib").write_bytes(b"trusted-placeholder")
            imu_path = root / "future-session.json"
            imu_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "device": "546C50CA07DE",
                        "create_time": 1_700_000_000_000,
                        "imu": base64.b64encode(frames.tobytes()).decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "imu_behavior.full_inference._load_gbdt_bundle",
                return_value=bundle,
            ):
                result = predict_imu(root, imu_path, device="cpu")

        self.assertEqual(result["algorithm"], ALGORITHM_VERSION)
        self.assertEqual(
            result["model_status"], "gbdt_fallback_waiting_for_best_pt"
        )
        self.assertEqual(result["preprocessing"]["segments"], 1)
        self.assertEqual(
            result["preprocessing"]["calibration_source"],
            "tool:unified_sensor_defaults",
        )
        self.assertEqual(
            result["preprocessing"]["sensor_calibration"]["acc_bias_counts"],
            [0.0, 0.0, 0.0],
        )
        self.assertEqual(result["postprocess"]["event_threshold"], 0.5)
        self.assertFalse(result["postprocess"]["posture_state_machine"])
        self.assertEqual(set(result["thresholds"].values()), {0.5})
        codes = {row["code"] for row in result["prediction_intervals"]}
        self.assertEqual(codes, {"STANDING"})

    def test_postprocess_never_bridges_a_recording_gap(self) -> None:
        times = np.asarray([0, 500, 1000, 10_000, 10_500, 11_000])
        posture = np.tile(np.asarray([[0.05, 0.95]]), (len(times), 1))
        walking = np.zeros(len(times))
        events = np.zeros((len(times), len(FULL_EVENT_CODES)))
        rows = postprocess_hierarchical_predictions(
            times,
            posture,
            walking,
            events,
            FULL_EVENT_CODES,
            {code: 0.5 for code in FULL_EVENT_CODES},
            output_hz=2.0,
            settings={
                "state_machine": {
                    "initial_state": "AUTO",
                    "max_sequence_gap_ms": 750,
                }
            },
        )
        lying = [row for row in rows if row["code"] == "LYING"]
        self.assertEqual(len(lying), 2)
        self.assertLessEqual(lying[0]["end_ms"], 1250)
        self.assertGreaterEqual(lying[1]["start_ms"], 9750)

    def test_predict_full_guide_candidates_use_exact_half_threshold_and_gap(self) -> None:
        times = np.arange(0, 11_000, 500, dtype=np.int64)
        posture = np.tile(np.asarray([[0.9, 0.1]]), (len(times), 1))
        walking = np.zeros(len(times))
        events = np.zeros((len(times), len(FULL_EVENT_CODES)))
        urination_index = FULL_EVENT_CODES.index("URINATION")
        events[0, urination_index] = 0.5
        events[10, urination_index] = 0.8
        events[21, urination_index] = 0.9

        rows = postprocess_predict_full_guide(
            times,
            posture,
            walking,
            events,
            FULL_EVENT_CODES,
        )

        urination = [row for row in rows if row["code"] == "URINATION"]
        self.assertEqual(
            [(row["start_ms"], row["end_ms"]) for row in urination],
            [(0, 5_500), (10_500, 11_000)],
        )
        self.assertEqual(urination[0]["decision_threshold"], 0.5)
        self.assertEqual(urination[1]["duration_s"], 0.5)

    def test_predict_full_guide_body_adapter_has_no_state_machine(self) -> None:
        times = np.asarray([0, 500, 1_000], dtype=np.int64)
        posture = np.asarray([[0.9, 0.1], [0.1, 0.9], [0.9, 0.1]])
        walking = np.asarray([0.0, 0.9, 0.6])
        events = np.zeros((len(times), len(FULL_EVENT_CODES)))

        rows = postprocess_predict_full_guide(
            times,
            posture,
            walking,
            events,
            FULL_EVENT_CODES,
        )

        body = [row for row in rows if row["layer"] == "body_state"]
        self.assertEqual(
            [
                (row["code"], row["start_ms"], row["end_ms"])
                for row in body
            ],
            [
                ("STANDING", 0, 500),
                ("LYING", 500, 1_000),
                ("WALKING", 1_000, 1_500),
            ],
        )

    def test_predict_full_cache_lookup_prefers_supervised_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20260816"
            model_dir = root / "复现实验" / "final_model"
            model_dir.mkdir(parents=True)
            key = "AABBCC_2026_08_16_12_00_00_1234567890"
            cache_dir = (
                root
                / "01_关键训练数据"
                / "supervised_cache"
                / "session_cache"
                / key
            )
            cache_dir.mkdir(parents=True)
            np.save(cache_dir / "features.npy", np.zeros((300, 13), np.float32))
            (cache_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "start_index": 0,
                                "stop_index": 300,
                                "start_ms": 160,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            source = Path(directory) / "2026-08-16 12_00_00.json"
            document = {"version": 2, "device": "AABBCC"}

            found = _find_guide_cache(model_dir, source, document)
            self.assertEqual(found, cache_dir)
            times, features, segments, metadata = _load_guide_cache(
                found, document
            )
            self.assertEqual(features.shape, (300, 13))
            self.assertEqual(segments, [(0, 300)])
            self.assertEqual((times[0], times[-1]), (160, 6_140))
            self.assertEqual(metadata["guide_cache_key"], key)
            mmap = getattr(features, "_mmap", None)
            if mmap is not None:
                mmap.close()

    def test_unknown_device_uses_same_global_calibration(self) -> None:
        frames = np.zeros(400, dtype=FRAME_DTYPE)
        frames["elapsed_ms"] = np.arange(len(frames), dtype=np.uint32) * 20
        frames["values"][:, 2] = 4096
        bundle = {
            "features": feature_names(),
            "models": {
                task: ConstantProbabilityModel(0.0)
                for task in REQUIRED_GBDT_TASKS
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gbdt_full.joblib").write_bytes(b"trusted-placeholder")
            imu_path = root / "new-device.json"
            imu_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "device": "UNREGISTERED_MAC",
                        "imu": base64.b64encode(frames.tobytes()).decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "imu_behavior.full_inference._load_gbdt_bundle",
                return_value=bundle,
            ):
                unconfigured = predict_imu(root, imu_path, device="cpu")
            self.assertEqual(
                unconfigured["preprocessing"]["calibration_source"],
                "tool:unified_sensor_defaults",
            )
            self.assertEqual(
                unconfigured["preprocessing"]["sensor_calibration"],
                {
                    "acc_divisor": 4096.0,
                    "acc_bias_counts": [0.0, 0.0, 0.0],
                    "gyro_divisor": 32.0,
                    "gyro_bias_counts": [0.0, 0.0, 0.0],
                    "mag_divisor": 1000.0,
                },
            )
            (root / "inference_config.json").write_text(
                json.dumps(
                    {
                        "sensor_calibration": {
                            "acc_divisor": 2048.0,
                            "acc_bias_counts": [1.0, 2.0, 3.0],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "imu_behavior.full_inference._load_gbdt_bundle",
                return_value=bundle,
            ):
                configured = predict_imu(root, imu_path, device="cpu")
            self.assertEqual(
                configured["preprocessing"]["calibration_source"],
                "inference_config:sensor_calibration",
            )
            self.assertEqual(
                configured["preprocessing"]["sensor_calibration"]["acc_divisor"],
                2048.0,
            )
            self.assertEqual(
                configured["preprocessing"]["sensor_calibration"]["acc_bias_counts"],
                [1.0, 2.0, 3.0],
            )
            self.assertNotEqual(
                unconfigured["model_fingerprint"], configured["model_fingerprint"]
            )


if __name__ == "__main__":
    unittest.main()
