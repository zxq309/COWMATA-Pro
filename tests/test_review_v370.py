import base64
import hashlib
import json

import numpy as np
import pytest


def test_ppg_review_uses_configured_clock_and_preserves_samples(tmp_path):
    from cowmata_tailring.workspace import sensor_records as s

    path = tmp_path / "ppg.json"
    values = np.array([100000, 100100, 100080], dtype="<u4")
    doc = dict(
        device="ABC",
        create_time=1787245607000,
        data=base64.b64encode(values.tobytes()).decode(),
        configs={"pulse_led_sr": 0, "pulse_led_avr": 0},
    )
    path.write_text(json.dumps(doc), encoding="utf-8")
    data = s.load_sensor_json(path, kind="ppg")
    assert data.kind == "ppg"
    assert np.array_equal(data.times_ms, [0, 20, 40])
    assert np.array_equal(data.plot_series()[0]["values"], values)
    assert data.epoch_at(20) == 1787245607020


def test_review_edits_existing_label_file_without_new_ids_or_raw_changes(tmp_path):
    from test_paired_dataset_v370 import fixture_farm

    from cowmata_tailring.workspace import review_store as r

    raw, label = fixture_farm(tmp_path / "farm")
    original_raw = raw.read_bytes()
    revision = hashlib.sha256(label.read_bytes()).hexdigest()
    doc = json.loads(label.read_text(encoding="utf-8"))
    doc["work"]["project"]["events"][0]["t0"] = 20
    r.save_review(label, doc["work"], expected_sha=revision)
    saved = json.loads(label.read_text(encoding="utf-8"))
    assert saved["work"]["project"]["events"][0]["id"] == 11
    assert saved["work"]["project"]["events"][0]["t0"] == 20
    assert raw.read_bytes() == original_raw
    assert r.resolve_label_path(raw) == label
    assert len(list(label.parent.glob("*.标注.json"))) == 1
    with pytest.raises(ValueError, match="变化|修改"):
        r.save_review(label, doc["work"], expected_sha=revision)


def test_review_rejects_creating_new_annotations(tmp_path):
    from test_paired_dataset_v370 import fixture_farm

    from cowmata_tailring.workspace import review_store as r

    _, label = fixture_farm(tmp_path / "farm")
    revision = hashlib.sha256(label.read_bytes()).hexdigest()
    doc = json.loads(label.read_text(encoding="utf-8"))
    doc["work"]["project"]["events"].append(dict(id=999, li=0, t0=1, t1=2))
    with pytest.raises(ValueError, match="新建|新增"):
        r.save_review(label, doc["work"], expected_sha=revision)


def test_paused_classification_allows_review_but_active_moves_do_not(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.dataset_access import DatasetLease

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    root = tmp_path / "farm"
    root.mkdir()
    owner = DatasetLease([root], kind="organize")
    owner.mark_pending("a" * 32, tmp_path / "job")
    with pytest.raises(OSError):
        DatasetLease([root], kind="review")
    owner.close()
    with DatasetLease([root], kind="review"):
        pass


def test_classified_videos_can_be_reviewed_without_claiming_sync(tmp_path, monkeypatch):
    import hashlib

    from test_paired_dataset_v370 import fixture_farm

    from cowmata_tailring.workspace import label_file
    from cowmata_tailring.workspace.catalog import file_stamp
    from cowmata_tailring.workspace.clocks import Anchor, ClockMap

    raw, label = fixture_farm(tmp_path / "farm")
    root = tmp_path / "farm/产犊"
    video = root / "Video/view/a.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"example-video-bytes")
    document = json.loads(label.read_text(encoding="utf-8"))
    document["work"]["clock"] = ClockMap([Anchor(0, 10000), Anchor(180, 10180)]).to_dict()
    label.write_text(json.dumps(document), encoding="utf-8")
    registry = {
        "records": [
            dict(
                kind="video",
                path=video.relative_to(root).as_posix(),
                sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
                size=video.stat().st_size,
                verified_stamp=file_stamp(video),
                metadata=dict(
                    camera="view",
                    naming_only=True,
                    needs_review=True,
                    intervals=[],
                    duration_ms=500,
                    archive_time={"start_ms": 10000},
                ),
            )
        ]
    }
    index = root / "资源索引.json"
    index.write_text(json.dumps(registry), encoding="utf-8")
    original = index.read_bytes()
    digest = label_file.digest_file

    def checked(path):
        assert path != video, "Unchanged verified video was hashed again"
        return digest(path)

    monkeypatch.setattr(label_file, "digest_file", checked)
    history = label_file.load_history(label)
    assert len(history.timeline.intervals) == 1
    assert not history.timeline.intervals[0].verified
    assert history.timeline.intervals[0].media_start == 0
    assert index.read_bytes() == original
