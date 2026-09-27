"""Behaviour recognition facade for the headless engine.

The six reviewed behaviour algorithms stay in ``cowmata_tailring.algorithms`` (their research
owners keep maintaining them there); this module is the only entry the external front-end and
the decision engine use, with JSON-serialisable inputs and outputs.
"""
from __future__ import annotations

from pathlib import Path


def catalog():
    from cowmata_tailring.algorithms.behavior_library import capability_matrix

    return capability_matrix()


def predict(code, source, model_dir, *, threshold=None):
    """Review candidates for one raw Motion JSON (never labels, never probabilities)."""
    from cowmata_tailring.algorithms.behavior_dispatch import predict_behavior

    return predict_behavior(code, Path(source), Path(model_dir), threshold=threshold)


def train(dataset, output, *, codes=None, modality="motion", cache=None, progress=lambda *_: None):
    """Train the generic event suite on a paired Raw/Label dataset (record-held-out evaluation)."""
    from cowmata_tailring.algorithms.dataset import scan_dataset
    from cowmata_tailring.algorithms.training import train_suite

    index = scan_dataset(dataset, pool_general_events=True, progress=progress)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    return train_suite(index, cache or output / "cache", output, progress=progress, codes=codes, modality=modality)


__all__ = ["catalog", "predict", "train"]
