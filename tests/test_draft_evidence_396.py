# ruff: noqa: F811 -- pytest fixture injection
from test_annotation_pipeline_v330 import case, window  # noqa: F401


def test_unconfirmed_video_draft_can_open_evidence_capture(window, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.evidence_ui import CaptureDialog

    window.work.project.cow_id = "20071"
    draft = window.work.add_draft(0, window.work.clock.map(100), window.work.clock.map(200), [])
    window.refresh_events(preferred=("draft", draft["id"]))
    # Actual decoder is covered by media tests; test the formerly blocked UI gate.
    monkeypatch.setattr(CaptureDialog, "begin_capture", lambda self: None)
    window.capture_evidence()
    QApplication.processEvents()
    dialog = window._capture_dialog
    assert dialog is not None and dialog.isVisible()
    assert draft["confirmation"] != "confirmed"
    assert window.work.project.events == []
    dialog.reject()


def test_existing_images_open_without_reextracting_or_confirming(window, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.evidence_ui import CaptureDialog

    event = window.work.project.add_event(0, 100, 200)
    event.extras.update(
        confirmation="needs_review",
        screenshots={
            "reference_ms": window.work.clock.map(100),
            "context": {"cow_id": "20071"},
            "items": [],
        },
    )
    window.refresh_events(preferred=("event", event.id))
    calls = []
    monkeypatch.setattr(CaptureDialog, "begin_capture", lambda self: calls.append(True))
    window.capture_evidence()
    QApplication.processEvents()
    assert window._capture_dialog is not None
    assert not calls
    assert event.extras["confirmation"] == "needs_review"
    window._capture_dialog.reject()


def test_saved_draft_images_remain_viewable_without_alignment(window):
    from PySide6.QtWidgets import QApplication

    draft = window.work.add_draft(0, window.work.clock.map(100), window.work.clock.map(200), [])
    draft["screenshots"] = {"reference_ms": 100, "context": {"cow_id": "20071"}, "items": []}
    window.work.clock.anchors.clear()
    window.refresh_events(preferred=("draft", draft["id"]))
    window.capture_evidence()
    QApplication.processEvents()
    assert window._capture_dialog is not None and window._capture_dialog.isVisible()
    assert draft["confirmation"] != "confirmed"
    window._capture_dialog.reject()
