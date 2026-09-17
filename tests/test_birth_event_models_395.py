import numpy as np
import pytest

from cowmata_tailring.algorithms import EVENT_CODES, EVENT_TITLES, validation_unit
from cowmata_tailring.algorithms.models import score_events


@pytest.mark.parametrize("code", ["FETAL_PART_FIRST_VISIBLE", "CALF_FULLY_EXPELLED"])
def test_birth_milestones_registered_as_cow_validated_point_candidates(code):
    assert code in EVENT_CODES and code in EVENT_TITLES
    assert validation_unit(code) == "cow"
    scores = np.zeros(60)
    scores[20:24] = 0.8
    candidates = score_events(
        scores, np.ones(60, dtype=bool), code=code, threshold=0.5, duration_ms=60000
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c["time_semantics"] == "approximate_point"
    assert c["review_status"] == "pending"
    assert c["requires_video_confirmation"] is True


def test_unreviewed_birth_time_cannot_be_used_as_positive_or_background():
    from cowmata_tailring.algorithms.training import training_rows

    code = "CALF_FULLY_EXPELLED"
    record = dict(
        events=[
            dict(code=code, start_ms=10000, end_ms=None, confirmation="confirmed"),
            dict(code=code, start_ms=40000, end_ms=None, confirmation="needs_review"),
        ]
    )
    feature = dict(
        X=np.arange(70, dtype=np.float32).reshape(-1, 1),
        seconds=np.arange(70) + 0.5,
        valid_context=np.ones(70, dtype=bool),
    )
    x, y, _ = training_rows([record], [feature], code)
    assert not np.any(x[y == 1] > 20)
    assert not np.any((x >= 30) & (x < 50))
