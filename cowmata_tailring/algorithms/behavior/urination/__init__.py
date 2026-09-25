"""COWMATA tail-ring urination detector (Lift-Hold-Return + numeric forest).

Pattern learned from the COWMATA_Behavior_Dataset (see README):

1. Lift   - a gyro burst at the tail base while the gravity vector seen by the
            ring leaves its hanging reference (median |delta a| ~0.35 g).
2. Hold   - the tail stays lifted and almost motionless for 17-60 s
            (gyro ~3.5 dps, the same as quiet standing).
3. Return - the gravity vector comes back to within a few degrees of the
            pre-event reference (tail hangs again, posture unchanged).

Candidates come from a label-free state machine that follows this pattern;
a numeric forest (JSON, no pickle) scores each candidate against defecation,
tail raising, straining, posture changes and background motion.
"""

SCHEMA = "cowmata-urination-2"
ALGORITHM_VERSION = "2.1.0"
EVENT_CODE = "URINATION"
EVENT_TITLE = "排尿"

from .detector import detect_file, detect_recording, load_bundle
from .train import train
