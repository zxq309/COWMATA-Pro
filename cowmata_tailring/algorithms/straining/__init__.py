"""努责 (STRAINING_BOUT) detection for tail-mounted rings: contraction-pulse-train algorithm.

Stage 1 scores every second from pulse-train and motion-context features, stage 2 re-ranks each
candidate bout with bout-level evidence. Inference needs numpy/scipy only; training uses
scikit-learn and exports numeric trees (no pickle).
"""

# Constants live in .constants so submodules never import this package while it initialises.
from .constants import ALGORITHM, BUNDLE_SCHEMA, CODE
from .model import detect, detect_file, load_bundle

__all__ = ["ALGORITHM", "BUNDLE_SCHEMA", "CODE", "detect", "detect_file", "load_bundle"]
