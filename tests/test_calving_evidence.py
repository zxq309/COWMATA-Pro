import json

import numpy as np
import pytest


def feature():
    return dict(duration_ms=1200000, seconds=np.arange(1200)+.5,
                valid=np.ones(1200, dtype=bool), dynamic=np.ones(1200),
                segments=[[0, 1200000]], temperature_ms=np.array([]), temperature_c=np.array([]),
                epoch_offset_ms=1700000000000)


def test_evidence_carries_posture_between_windows_but_keeps_missing_temperature_unknown():
    from cowmata_tailring.algorithms.evidence import evidence_rows
    rows = evidence_rows(dict(cow_id="001", asset_id="one", raw="one.json"), feature(),
                         [dict(code="LYING_DOWN", start_ms=10000, end_ms=15000)])
    assert len(rows) == 2
    assert rows[1]["lying_seconds"] == 600
    assert rows[1]["lying_fraction_known"] == 1
    assert all(r["temperature_c"] is None and r["warning_level"] is None for r in rows)
    assert rows[0]["unknown_seconds"] == 10


def test_evidence_never_connects_animals_or_separate_records():
    from cowmata_tailring.algorithms.evidence import evidence_rows
    first = evidence_rows(dict(cow_id="001", asset_id="one", raw="one.json"), feature(),
                          [dict(code="LYING_DOWN", start_ms=10000, end_ms=15000)])
    second = evidence_rows(dict(cow_id="002", asset_id="two", raw="two.json"), feature(), [])
    assert first[1]["lying_fraction_known"] == 1
    assert second[1]["lying_fraction_known"] is None
    assert second[1]["unknown_seconds"] == 600


def test_baseline_uses_only_prior_windows_for_same_animal():
    from cowmata_tailring.algorithms.evidence import add_baselines
    rows = [dict(cow_id="001", asset_id=str(i), start_epoch_ms=i*600000, end_epoch_ms=(i+1)*600000,
                 activity_index=float(i), temperature_c=38.) for i in range(8)]
    rows += [dict(cow_id="other", asset_id="x", start_epoch_ms=0, end_epoch_ms=600000,
                  activity_index=1000., temperature_c=1000.)]
    add_baselines(rows)
    assert rows[0]["activity_index_baseline"] is None
    assert rows[6]["activity_index_baseline"] == 2.5
    assert rows[7]["temperature_c_change"] == 0


def test_duplicate_straining_bouts_do_not_double_count_duration():
    from cowmata_tailring.algorithms.evidence import evidence_rows
    events = [dict(code="STRAINING_BOUT", start_ms=10000, end_ms=20000),
              dict(code="STRAINING_BOUT", start_ms=15000, end_ms=25000)]
    row = evidence_rows(dict(cow_id="001", asset_id="one", raw="one.json"), feature(), events)[0]
    assert row["straining_seconds"] == 15


def test_evidence_task_outputs_no_health_probability_or_alert(tmp_path, monkeypatch):
    from cowmata_tailring.algorithms import evidence
    monkeypatch.setattr(evidence, "read_suite", lambda _: dict(version="test"))
    monkeypatch.setattr(evidence, "load_features", lambda *_: feature())
    monkeypatch.setattr(evidence, "infer_features", lambda *_: [])
    record = dict(cow_id="001", asset_id="a", raw="a.json", identity_eligible=True)
    report = evidence.build_evidence(dict(records=[record, record], issues=[dict(path="conflict.json", reason="conflicting_cow_identity")]), tmp_path, tmp_path, tmp_path/"out")
    assert len(report["rows"]) == 2
    assert len(report["issues"]) == 1
    assert report["evaluation"][-1]["value"] is None
    assert (tmp_path/"out/产犊监测评价.csv").is_file()


def test_selected_suite_missing_never_falls_back_silently(tmp_path):
    from cowmata_tailring.algorithms.registry import active_suite
    (tmp_path/"active.json").write_text(json.dumps(dict(version="missing")))
    with pytest.raises(ValueError, match="missing"):
        active_suite(tmp_path)


def test_posture_window_partition_conserves_duration():
    from cowmata_tailring.algorithms.posture import occupancy
    events = [dict(code="LYING_DOWN", start_ms=10000, end_ms=15000),
              dict(code="STANDING_UP", start_ms=650000, end_ms=660000)]
    observed = [[0, 1200000]]
    full = occupancy(events, 0, 1200000, observed_intervals=observed)
    parts = [occupancy(events, a, a+600000, observed_intervals=observed) for a in (0, 600000)]
    for key in ("lying_seconds", "standing_seconds", "transition_seconds", "unknown_seconds"):
        assert full[key] == sum(p[key] for p in parts)


def test_short_signal_has_no_valid_context_without_shape_failure():
    from cowmata_tailring.algorithms.features import second_features
    f = second_features([0, 20], [[0,0,1],[0,0,1]], [[0,0,0],[0,0,0]])
    assert f["X"].shape[0] == 1
    assert not f["valid_context"].any()
