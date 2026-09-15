import hashlib
import json

import pytest


def suite(path, version="test-1"):
    from cowmata_tailring.algorithms import EVENT_CODES

    path.mkdir()
    models = []
    for code in EVENT_CODES:
        model = dict(
            schema="numeric-forest-1",
            features=["x"],
            median=[0],
            trees=[dict(left=[-1], right=[-1], feature=[-2], threshold=[-2], probability=[0.8])],
            code=code,
            feature_version="event-shape-1",
            threshold=0.5,
        )
        name = code.lower() + ".json"
        data = json.dumps(model).encode()
        (path / name).write_bytes(data)
        models.append(
            dict(
                code=code,
                title=code,
                file=name,
                sha256=hashlib.sha256(data).hexdigest(),
                threshold=0.5,
            )
        )
    (path / "评估报告.json").write_text('{"models":[]}', encoding="utf-8")
    (path / "suite.json").write_text(
        json.dumps(
            dict(
                schema="cowmata-event-suite-1",
                complete=True,
                version=version,
                feature_version="event-shape-1",
                models=models,
                report="评估报告.json",
                dataset_fingerprint="test",
            )
        ),
        encoding="utf-8",
    )
    return path


def test_model_versions_are_separate_and_activation_is_reversible(tmp_path):
    from cowmata_tailring.algorithms.registry import activate, active_suite, install_suite

    home = tmp_path / "algorithms"
    one = install_suite(suite(tmp_path / "one"), home)
    two = install_suite(suite(tmp_path / "two", "test-2"), home)
    assert active_suite(home)["version"] == "test-2"
    activate(home, "test-1")
    assert active_suite(home)["version"] == "test-1"
    assert one.is_dir() and two.is_dir()
    assert not (one / "training-inputs.json").exists()


@pytest.mark.parametrize("fault", ["changed_model", "escape", "incomplete"])
def test_invalid_model_never_replaces_active_version(tmp_path, fault):
    from cowmata_tailring.algorithms.registry import active_suite, install_suite

    home = tmp_path / "algorithms"
    install_suite(suite(tmp_path / "one"), home)
    incoming = suite(tmp_path / "two", "test-2")
    doc = json.loads((incoming / "suite.json").read_text(encoding="utf-8"))
    if fault == "changed_model":
        (incoming / doc["models"][0]["file"]).write_text("{}")
    if fault == "escape":
        doc["models"][0]["file"] = "../outside.json"
    if fault == "incomplete":
        doc["complete"] = False
    (incoming / "suite.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        install_suite(incoming, home)
    assert active_suite(home)["version"] == "test-1"


def test_import_for_comparison_keeps_bundled_version_active(tmp_path, monkeypatch):
    from cowmata_tailring.algorithms import registry
    app = tmp_path / "application"
    bundled = app / "assets/algorithms"
    bundled.mkdir(parents=True)
    suite(bundled / "bundled", "bundled-1")
    monkeypatch.setattr(registry, "APP_ROOT", app)
    home = tmp_path / "home"
    registry.install_suite(suite(tmp_path / "new", "new-2"), home, make_active=False)
    assert registry.active_suite(home)["version"] == "bundled-1"
    registry.activate(home, "new-2")
    assert registry.active_suite(home)["version"] == "new-2"
