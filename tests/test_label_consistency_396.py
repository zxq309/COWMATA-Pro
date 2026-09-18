# ruff: noqa: F811 -- pytest fixture injection
from copy import deepcopy

from test_annotation_pipeline_v330 import case, window  # noqa: F401

from cowmata_tailring.annotation.core import Label
from cowmata_tailring.annotation.defaults import LEGACY_DEFAULT_LABELS
from cowmata_tailring.workspace.work import SessionWork


def legacy_work():
    work = SessionWork("a" * 64)
    work.project.labels = [Label.from_dict(x) for x in LEGACY_DEFAULT_LABELS]
    work.project.extras["dataset_category"] = "calving"
    work.project.add_event(9, 100, 200)
    work.project.add_event(0, 300, 400)
    return work


def test_history_loading_maps_tail_labels_without_changing_ids_times_or_input():
    old = legacy_work().to_dict()
    before = deepcopy(old)
    new = SessionWork.from_dict(old)
    assert old == before
    assert [(e.id, e.t0, e.t1) for e in new.project.events] == [(1, 100, 200), (2, 300, 400)]
    assert new.project.labels[new.project.events[0].li].code == "LYING_TAIL_RAISED"
    historical = new.project.labels[new.project.events[1].li]
    assert historical.code == "STANDING" and not historical.key and not historical.trainable
    assert SessionWork.from_dict(new.to_dict()).to_dict() == new.to_dict()


def test_old_unused_definitions_do_not_reappear_as_new_annotation_choices(window):
    window.work = legacy_work()
    window.refresh_events()
    assert [window.labels.itemText(i) for i in range(window.labels.count())] == [
        "[1] 起立过程",
        "[2] 卧倒过程",
        "[3] 站立抬尾",
        "[4] 站立甩尾",
        "[5] 躺卧抬尾",
        "[6] 躺卧甩尾",
        "[7] 努责",
        "[8] 胎膜囊（水囊）首次可见",
        "[9] 胎儿首个部位首次可见",
        "[A] 犊牛完全娩出",
        "[B] 胎膜完全排出",
        "[C] 排尿",
        "[D] 排便",
        "[E] 爬跨",
        "[F] 助产",
    ]
    assert all(x.key == "" for x in window.work.project.labels[15:])


def test_posture_specific_algorithms_do_not_use_generic_tail_model():
    from cowmata_tailring.workspace.algorithm_catalog import Algorithm, bindings

    pack = {"models": [{"code": "TAIL_RAISED"}, {"code": "LYING_TAIL_RAISED"}]}
    assert bindings(Algorithm("STANDING_TAIL_RAISED", "站立抬尾"), [pack]) == []
    assert len(bindings(Algorithm("LYING_TAIL_RAISED", "躺卧抬尾"), [pack])) == 1


def test_algorithm_reader_maps_historical_tail_and_reports_removed_labels(tmp_path):
    import json

    from test_algorithm_workbench import pair

    from cowmata_tailring.algorithms.dataset import scan_dataset

    label = pair(
        tmp_path,
        "legacy",
        [
            {"id": 1, "label_code": "TAIL_RAISED", "t0": 1000, "t1": 2000},
            {"id": 2, "label_code": "WALKING", "t0": 3000, "t1": 4000},
        ],
    )
    document = json.loads(label.read_text())
    document["dataset_category"] = "calving"
    label.write_text(json.dumps(document), encoding="utf-8")
    before = label.read_bytes()
    result = scan_dataset(tmp_path)
    assert [e["code"] for e in result["records"][0]["events"]] == ["LYING_TAIL_RAISED"]
    assert any(
        i["reason"] == "historical_label_review" and i["event_id"] == 2 for i in result["issues"]
    )
    assert label.read_bytes() == before
