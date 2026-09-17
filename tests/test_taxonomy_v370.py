from copy import deepcopy


def old_document(category="calving"):
    specs = [
        ("站立", "STANDING"),
        ("抬尾", "TAIL_RAISED"),
        ("甩尾", "TAIL_WAGGING"),
        ("努责区间", "STRAINING_BOUT"),
        ("人工辅助产犊", "人工辅助产犊"),
        ("采食", "FEEDING"),
    ]
    return dict(
        format="cowmata-annotation",
        dataset_category=category,
        source={"path": "Motion/a.json"},
        work=dict(
            asset_id="a" * 64,
            project=dict(
                labels=[
                    dict(name=n, code=c, key=str(i), type="interval")
                    for i, (n, c) in enumerate(specs)
                ],
                events=[
                    dict(id=i + 10, li=i, label_code=c, t0=100 + i * 100, t1=150 + i * 100)
                    for i, (_, c) in enumerate(specs)
                ],
            ),
            drafts=[],
            clock={"anchors": [{"source_ms": 0, "reference_ms": 1000}]},
        ),
    )


def test_current_labels_have_requested_names_and_unique_keys():
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    names = [
        "起立过程",
        "卧倒过程",
        "站立抬尾",
        "站立甩尾",
        "躺卧抬尾",
        "躺卧甩尾",
        "努责",
        "胎膜囊（水囊）首次可见",
        "胎儿首个部位首次可见",
        "犊牛完全娩出",
        "胎膜完全排出",
        "排尿",
        "排便",
        "爬跨",
        "助产",
    ]
    assert [r["name"] for r in DEFAULT_LABELS] == names
    assert [r["key"] for r in DEFAULT_LABELS] == list("123456789ABCDEF")


def test_migration_preserves_event_identity_times_and_history():
    from cowmata_tailring.annotation import taxonomy

    before = old_document()
    original = deepcopy(before)
    after = taxonomy.upgrade_document(before, category="calving")
    project = after["work"]["project"]
    assert before == original
    assert [e["id"] for e in project["events"]] == [11, 12, 13, 14, 15]
    codes = [project["labels"][e["li"]]["code"] for e in project["events"]]
    assert codes == [
        "LYING_TAIL_RAISED",
        "LYING_TAIL_WAGGING",
        "STRAINING_BOUT",
        "MANUAL_CALVING_ASSISTANCE",
        "FEEDING",
    ]
    assert [(e["t0"], e["t1"]) for e in project["events"]] == [
        (e["t0"], e["t1"]) for e in original["work"]["project"]["events"][1:]
    ]
    assert after["work"]["clock"] == original["work"]["clock"]
    assert project["labels"][-1]["code"] == "FEEDING" and not project["labels"][-1]["key"]
    assert taxonomy.upgrade_document(after, category="calving") == after


def test_non_calving_tail_labels_use_standing_context():
    from cowmata_tailring.annotation import taxonomy

    project = taxonomy.upgrade_document(old_document("pregnancy_late"), category="pregnancy_late")[
        "work"
    ]["project"]
    assert [e["label_code"] for e in project["events"]][:2] == [
        "STANDING_TAIL_RAISED",
        "STANDING_TAIL_WAGGING",
    ]


def test_an_existing_onset_is_not_silently_deleted():
    from cowmata_tailring.annotation import taxonomy

    data = old_document()
    p = data["work"]["project"]
    p["labels"].append(dict(name="努责首次出现", code="STRAINING_ONSET", type="point", key="4"))
    p["events"].append(dict(id=99, li=6, label_code="STRAINING_ONSET", t0=999, t1=None))
    result = taxonomy.upgrade_document(data, category="calving")["work"]["project"]
    assert any(e["id"] == 99 and e["t1"] is None for e in result["events"])
    assert next(label for label in result["labels"] if label["code"] == "STRAINING_ONSET")["key"] == ""
