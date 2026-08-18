from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import annotation_core
from annotation_review_core import (
    build_json_index,
    export_review_results,
    import_csv_files,
    infer_json_root,
    load_events_csv,
    load_workspace,
    new_workspace,
    resolve_session_json,
    resolve_workspace_jsons,
    review_counts,
    save_workspace,
    source_file_fingerprint,
)
from annotation_review_core import ReviewImportError


EVENT_COLUMNS = [
    "index",
    "session_id",
    "cow_id",
    "layer",
    "label",
    "code",
    "en",
    "type",
    "evidence",
    "annotator",
    "protocol",
    "t_start_rel_ms",
    "t_end_rel_ms",
    "reviewed_start_ms",
    "reviewed_end_ms",
    "duration_ms",
    "frame_start",
    "frame_end",
    "t_start_wall_bj",
    "t_end_wall_bj",
    "note",
]


def write_events_csv(path: Path, *, session_id: str = "session-001") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "index": 1,
            "session_id": session_id,
            "cow_id": "cow-1",
            "layer": "body_state",
            "label": "站立",
            "code": "STANDING",
            "en": "standing",
            "type": "interval",
            "evidence": "两者",
            "annotator": "tester",
            "protocol": "v4",
            "t_start_rel_ms": 1000,
            "t_end_rel_ms": 2000,
            "reviewed_start_ms": 1000,
            "reviewed_end_ms": 2000,
            "duration_ms": 1000,
            "frame_start": 50,
            "frame_end": 100,
            "t_start_wall_bj": "",
            "t_end_wall_bj": "",
            "note": "original",
        },
        {
            "index": 2,
            "session_id": session_id,
            "cow_id": "cow-1",
            "layer": "legacy_annotation",
            "label": "采食",
            "code": "FEEDING",
            "en": "feeding",
            "type": "interval",
            "evidence": "仅曲线",
            "annotator": "tester",
            "protocol": "v3",
            "t_start_rel_ms": 3000,
            "t_end_rel_ms": 4000,
            "reviewed_start_ms": 3000,
            "reviewed_end_ms": 4000,
            "duration_ms": 1000,
            "frame_start": 150,
            "frame_end": 200,
            "t_start_wall_bj": "",
            "t_end_wall_bj": "",
            "note": "legacy",
        },
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


class AnnotationReviewCoreTests(unittest.TestCase):
    def test_import_preserves_known_and_feeding_legacy_labels(self) -> None:
        with TemporaryDirectory() as directory:
            csv_path = Path(directory) / "session-001.events.csv"
            write_events_csv(csv_path)

            session = load_events_csv(csv_path)

        self.assertEqual(len(session["events"]), 2)
        self.assertEqual(session["events"][0]["label_code"], "STANDING")
        legacy = session["events"][1]
        self.assertEqual(legacy["label_code"], "FEEDING")
        self.assertTrue(legacy["review_legacy_label"])
        self.assertTrue(
            session["labels"][legacy["li"]]["legacy"]
        )
        self.assertEqual(legacy["review_original"]["code"], "FEEDING")
        self.assertEqual(legacy["ev"], "curve")

    def test_import_uses_reviewed_range_when_export_contains_one(self) -> None:
        with TemporaryDirectory() as directory:
            csv_path = Path(directory) / "session-001.events.csv"
            write_events_csv(csv_path)
            text = csv_path.read_text(encoding="utf-8-sig")
            text = text.replace(
                "1000,2000,1000,2000,1000",
                "1000,2000,1200,1800,1000",
                1,
            )
            csv_path.write_text(text, encoding="utf-8-sig")
            session = load_events_csv(csv_path)

        event = session["events"][0]
        self.assertEqual((event["t0"], event["t1"]), (1200.0, 1800.0))
        self.assertEqual(event["review_original"]["exported_start_ms"], 1000.0)
        self.assertEqual(event["review_original"]["exported_end_ms"], 2000.0)

    def test_import_deduplicates_and_reports_bad_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.events.csv"
            bad = root / "bad.events.csv"
            write_events_csv(good, session_id="good")
            bad.write_text("session_id,label\ngood,站立\n", encoding="utf-8")
            workspace = new_workspace()

            imported, skipped, errors = import_csv_files(
                workspace, [good, good, bad]
            )

        self.assertEqual(imported, 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(workspace["sessions"]), 1)

    def test_json_resolution_uses_device_and_reports_ambiguity_or_missing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "exports" / "session-001.events.csv"
            write_events_csv(csv_path)
            session = load_events_csv(csv_path)
            session["device"] = "DEV-A"
            session["cow_id"] = "cow-1"
            first = root / "九轴数据" / "DEV-A" / "cow-1" / "session-001.json"
            second = root / "九轴数据" / "DEV-B" / "cow-2" / "session-001.json"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")
            index = build_json_index(root / "九轴数据")

            self.assertEqual(resolve_session_json(session, index), "resolved")
            self.assertEqual(Path(session["json_path"]), first.resolve())

            ambiguous = load_events_csv(csv_path)
            ambiguous["device"] = ""
            ambiguous["cow_id"] = ""
            self.assertEqual(
                resolve_session_json(ambiguous, index), "ambiguous"
            )
            self.assertEqual(ambiguous["json_path"], "")

            missing_path = root / "exports" / "missing.events.csv"
            write_events_csv(missing_path, session_id="missing")
            missing = load_events_csv(missing_path)
            self.assertEqual(resolve_session_json(missing, index), "missing")

    def test_json_resolution_rejects_a_fingerprint_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "session-001.events.csv"
            write_events_csv(csv_path)
            source = root / "json" / "session-001.json"
            source.parent.mkdir()
            source.write_text("new content", encoding="utf-8")
            session = load_events_csv(csv_path)
            session["source_meta"] = {
                "source_json_fingerprint": "sha256-partial:not-the-file"
            }
            index = build_json_index(source.parent)

            status = resolve_session_json(session, index)

        self.assertEqual(status, "fingerprint_mismatch")
        self.assertEqual(session["json_path"], "")
        self.assertEqual(session["json_candidates"], [str(source.resolve())])

    def test_json_resolution_can_use_saved_absolute_source_path_without_index(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "session-001.events.csv"
            write_events_csv(csv_path)
            source = root / "elsewhere" / "session-001.json"
            source.parent.mkdir()
            source.write_text("source", encoding="utf-8")
            session = load_events_csv(csv_path)
            session["source_meta"] = {"source_json_path": str(source)}

            status = resolve_session_json(session, {})

        self.assertEqual(status, "resolved")
        self.assertEqual(session["json_path"], str(source.resolve()))

    def test_infer_root_and_workspace_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "标注" / "session-001.events.csv"
            write_events_csv(csv_path)
            json_root = root / "九轴数据"
            source = json_root / "DEV-A" / "session-001.json"
            source.parent.mkdir(parents=True)
            source.write_bytes(b'{"samples": []}')
            self.assertEqual(infer_json_root([csv_path]), json_root.resolve())

            workspace = new_workspace()
            imported, _skipped, errors = import_csv_files(workspace, [csv_path])
            self.assertEqual((imported, errors), (1, []))
            counts = resolve_workspace_jsons(workspace, json_root)
            self.assertEqual(counts["resolved"], 1)
            self.assertEqual(review_counts(workspace)["pending"], 2)

            workspace_path = root / "review.review.json"
            save_workspace(workspace, workspace_path)
            restored = load_workspace(workspace_path)

        self.assertEqual(restored["schema"], 1)
        self.assertEqual(len(restored["sessions"]), 1)
        self.assertEqual(restored["sessions"][0]["json_match_status"], "resolved")

    def test_infer_root_supports_json_beside_csv(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "session-001.events.csv"
            write_events_csv(csv_path)
            (root / "session-001.json").write_text("{}", encoding="utf-8")
            self.assertEqual(infer_json_root([csv_path]), root.resolve())

    def test_export_is_non_destructive_and_contains_audit_and_fingerprint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "exports" / "session-001.events.csv"
            write_events_csv(csv_path)
            original_csv = csv_path.read_bytes()
            source = root / "九轴数据" / "DEV-A" / "session-001.json"
            source.parent.mkdir(parents=True)
            source.write_bytes(b'{"samples": [1, 2, 3]}')

            workspace = new_workspace(str(source.parent.parent))
            imported, _skipped, errors = import_csv_files(workspace, [csv_path])
            self.assertEqual((imported, errors), (1, []))
            session = workspace["sessions"][0]
            resolve_workspace_jsons(workspace, root / "九轴数据")
            first, deleted = session["events"]
            urination_index = next(
                index
                for index, label in enumerate(session["labels"])
                if label.get("code") == "URINATION"
            )
            first.update(
                {
                    "li": urination_index,
                    "label_code": "URINATION",
                    "t0": 1200.0,
                    "t1": 2500.0,
                    "note": "边界已按波形修正",
                    "review_status": "reviewed",
                    "review_modified": True,
                    "reviewed_by": "reviewer",
                    "reviewed_at": "2026-08-16T12:00:00+08:00",
                }
            )
            deleted["review_deleted"] = True
            deleted["review_modified"] = True
            deleted["review_status"] = "modified"

            output = root / "复核结果"
            created = export_review_results(workspace, output)
            self.assertEqual(len(created), 3)
            reviewed_csv = output / "session-001.reviewed.events.csv"
            reviewed_meta = output / "session-001.reviewed.events_meta.json"
            report = output / "annotation_review_report.csv"
            self.assertTrue(reviewed_csv.is_file())
            self.assertTrue(reviewed_meta.is_file())
            self.assertTrue(report.is_file())
            self.assertEqual(csv_path.read_bytes(), original_csv)

            with reviewed_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["code"], "URINATION")
            self.assertEqual(rows[0]["t_start_rel_ms"], "1200")
            self.assertEqual(rows[0]["t_end_rel_ms"], "2500")
            self.assertEqual(rows[0]["note"], "边界已按波形修正")

            meta = json.loads(reviewed_meta.read_text(encoding="utf-8"))
            self.assertEqual(meta["source_json_path"], str(source.resolve()))
            self.assertEqual(meta["source_json_name"], source.name)
            self.assertEqual(meta["source_json_size"], source.stat().st_size)
            self.assertEqual(meta["source_json_mtime_ns"], source.stat().st_mtime_ns)
            self.assertEqual(
                meta["source_json_fingerprint"],
                source_file_fingerprint(source),
            )
            report_text = report.read_text(encoding="utf-8-sig")
            self.assertIn("FEEDING", report_text)
            self.assertIn("URINATION", report_text)

            overwritten = export_review_results(
                workspace,
                overwrite_source=True,
            )
            self.assertIn(csv_path.resolve(), overwritten)
            with csv_path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                source_rows = list(csv.DictReader(handle))
            self.assertEqual(len(source_rows), 1)
            self.assertEqual(source_rows[0]["code"], "URINATION")
            self.assertEqual(source_rows[0]["t_start_rel_ms"], "1200")
            reloaded = load_events_csv(csv_path)
            self.assertEqual(len(reloaded["events"]), 1)
            self.assertEqual(reloaded["events"][0]["label_code"], "URINATION")
            self.assertEqual(
                (
                    reloaded["events"][0]["t0"],
                    reloaded["events"][0]["t1"],
                ),
                (1200.0, 2500.0),
            )
            self.assertTrue(
                (root / "exports" / "session-001.events_meta.json").is_file()
            )
            overwritten_again = export_review_results(
                workspace,
                overwrite_source=True,
            )
            self.assertIn(
                root / "exports" / "annotation_review_report.csv",
                overwritten_again,
            )
            self.assertFalse(
                (root / "exports" / "annotation_review_report_2.csv").exists()
            )

            second = export_review_results(workspace, output)
            self.assertTrue(
                (output / "session-001.reviewed.events_2.csv").is_file()
            )
            self.assertEqual(len(second), 3)

    def test_overwrite_refuses_to_recreate_a_missing_source_csv(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "session-001.events.csv"
            missing = root / "session-002.events.csv"
            write_events_csv(first)
            write_events_csv(missing, session_id="session-002")
            workspace = new_workspace()
            import_csv_files(workspace, [first, missing])
            original_first = first.read_bytes()
            missing.unlink()

            with self.assertRaises(ReviewImportError):
                export_review_results(workspace, overwrite_source=True)
            self.assertEqual(first.read_bytes(), original_first)

    def test_build_meta_preserves_future_runtime_fields(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_bytes(b"source-json")
            project = annotation_core.Project(
                source={"path": str(source), "device": "DEV"}
            )
            meta = annotation_core.build_meta(
                project,
                data_meta={
                    "calibration_temperature": 23.5,
                    "future_acc_bias": [0.1, 0.2, 0.3],
                },
            )
            self.assertEqual(meta["calibration_temperature"], 23.5)
            self.assertEqual(meta["future_acc_bias"], [0.1, 0.2, 0.3])
            self.assertEqual(meta["source_json_path"], str(source.resolve()))
            self.assertEqual(meta["source_json_name"], source.name)
            self.assertEqual(meta["source_json_size"], source.stat().st_size)
            self.assertTrue(
                meta["source_json_fingerprint"].startswith("sha256-partial:")
            )

    def test_workspace_loader_rejects_malformed_session_events(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "broken.review.json"
            path.write_text(
                json.dumps(
                    {
                        "_type": "bovine-annotation-review-workspace",
                        "schema": 1,
                        "sessions": [{"events": {}}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ReviewImportError):
                load_workspace(path)

    def test_workspace_loader_reports_invalid_schema_instead_of_leaking_value_error(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad-schema.review.json"
            path.write_text(
                json.dumps(
                    {
                        "_type": "bovine-annotation-review-workspace",
                        "schema": "not-a-number",
                        "sessions": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ReviewImportError):
                load_workspace(path)


if __name__ == "__main__":
    unittest.main()
