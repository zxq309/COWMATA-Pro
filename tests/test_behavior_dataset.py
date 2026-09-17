import json


def test_common_behaviors_cross_categories_but_specific_behaviors_are_gated():
    from cowmata_tailring.workspace.behavior_dataset import behavior_allowed

    for category in (
        "estrus",
        "calving",
        "pregnancy_early",
        "pregnancy_mid",
        "pregnancy_late",
        "disease",
        "healthy",
    ):
        assert behavior_allowed("WALKING", category)
        assert behavior_allowed("LYING", category)
        assert behavior_allowed("MOUNTING", category) == (category == "estrus")
        assert behavior_allowed("STRAINING_BOUT", category) == (category == "calving")


def test_behavior_samples_have_ear_tags_exact_counts_and_cow_splits(tmp_path, monkeypatch):
    import numpy as np
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.behavior_dataset import build_behavior_dataset
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    raw, label = sources(tmp_path)
    project = tmp_path / "project"
    execute_migration(plan_migration([label], [raw], project, category="pregnancy_late"))
    output = tmp_path / "dataset"
    result = build_behavior_dataset([project], output, behaviors=["URINATION"])
    from cowmata_tailring.workspace.mother_dataset import read_dataset
    assert read_dataset(output)["manifest"]["pipeline_complete"]
    assert result["behavior_samples"] == 1
    samples = list((output / "行为数据集/排尿/21100").glob("*.npz"))
    assert len(samples) == 1 and samples[0].name.startswith("21100_")
    with np.load(samples[0], allow_pickle=False) as sample:
        assert sample["raw_counts"].shape == (3, 9)
        assert sample["parent_ms"].tolist() == [0, 20, 40]
        assert sample["cow_id"].item() == "21100"
    manifest = json.loads((output / "dataset-manifest.json").read_text(encoding="utf-8"))
    assert manifest["unknown_is_negative"] is False
    assert not (output / "行为数据集/排尿/孕晚期").exists()


def test_one_conflicted_record_does_not_disqualify_entire_device(tmp_path, monkeypatch):
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    bad_raw, bad_label = sources(tmp_path / "bad")
    bad_label.write_text(
        bad_label.read_text(encoding="utf-8-sig").replace("21100-10", "24178-11"),
        encoding="utf-8-sig",
    )
    good_raw, good_label = sources(tmp_path / "good")
    value = json.loads(good_raw.read_text(encoding="utf-8"))
    value["uid"] = "different_record"
    value['create_time']+=1000  # A second recording, not two versions of the same starting instant.
    good_raw.write_text(json.dumps(value), encoding="utf-8")
    projects = []
    for name, raw, label in [("bad", bad_raw, bad_label), ("good", good_raw, good_label)]:
        project = tmp_path / (name + "-project")
        execute_migration(plan_migration([label], [raw], project, category="pregnancy_late"))
        projects.append(project)
    export_dataset(projects, tmp_path / "dataset")
    import hashlib

    rows = {r["asset_id"]: r for r in read_dataset(tmp_path / "dataset")["sources"]}
    assert not rows[hashlib.sha256(bad_raw.read_bytes()).hexdigest()]["identity_eligible"]
    assert rows[hashlib.sha256(good_raw.read_bytes()).hexdigest()]["identity_eligible"]


def test_explicit_cow_correction_is_used_in_dataset_source_names(tmp_path, monkeypatch):
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.behavior_dataset import build_behavior_dataset
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.work import SessionWork

    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    raw, label = sources(tmp_path)
    project = tmp_path / "farm/怀孕/孕晚期"
    execute_migration(plan_migration([label], [raw], project, category="pregnancy_late"))
    saved = next((project / "标注工程/annotations").glob("*.json"))
    work = SessionWork.from_dict(json.loads(saved.read_text(encoding="utf-8")))
    work.confirm_cow("99999")
    saved.write_text(json.dumps(work.to_dict()), encoding="utf-8")
    build_behavior_dataset([tmp_path / "farm"], tmp_path / "data", behaviors=["URINATION"])
    source = json.loads((tmp_path / "data/sources.jsonl").read_text(encoding="utf-8"))
    assert source["cow_id"] == "99999"
    assert source["path"].split("/")[-1].startswith("99999_")


def test_single_exported_label_retains_explicit_source_binding(tmp_path, monkeypatch):
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset
    monkeypatch.setenv('COWMATA_ACCESS_DIR',str(tmp_path/'access'))
    raw,label=sources(tmp_path)
    project=tmp_path/'project'
    execute_migration(plan_migration([label],[raw],project,category='pregnancy_late'))
    exported=next(project.rglob('*.标注.json'))
    export_dataset([exported],tmp_path/'dataset')
    source=read_dataset(tmp_path/'dataset')['sources'][0]
    assert source['cow_id']=='21100' and source['device_id']=='546C50CA07D5'
    assert source['identity_eligible']
