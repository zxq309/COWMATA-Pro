"""Versioned human work. Recalibration invalidates review, never moves labels."""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass, field

from cowmata_tailring.annotation.core import Event, Project, UndoStack, new_project

from .clocks import ClockMap


@dataclass
class SessionWork:
    asset_id: str
    project: Project = field(default_factory=new_project)
    clock: ClockMap = field(default_factory=ClockMap)
    mapping_history: list[dict] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    undo: UndoStack = field(default_factory=lambda: UndoStack(limit=100), repr=False)

    def set_category(self, code, *, context=None):
        from .data_category import category_fields
        fields = category_fields(code)
        self.project.extras.update(fields)
        if context is not None:
            self.project.extras["collection_context"] = copy.deepcopy(context)
        for item in self.drafts:
            item.update(fields)
        for event in self.project.events:
            event.extras.update(fields)

    def category_fields(self):
        return {key: self.project.extras.get(key, "") for key in ("dataset_category", "dataset_category_label")}

    def to_dict(self):
        return {"schema": 1, "asset_id": self.asset_id, "project": self.project.to_dict(),
                "clock": self.clock.to_dict(), "mapping_history": self.mapping_history,
                "drafts": self.drafts, "progress": self.progress}

    @classmethod
    def from_dict(cls, data):
        return cls(data["asset_id"], Project.from_dict(data["project"]),
                   ClockMap.from_dict(data.get("clock", {})), data.get("mapping_history", []),
                   data.get("drafts", []), data.get("progress", {}))

    def checkpoint(self):
        self.undo.push(self.to_dict())
        if self.progress.get("status") == "done":
            self.progress["status"] = "in_progress"

    def restore(self, data):
        other = self.from_dict(data)
        for key in ("project", "clock", "mapping_history", "drafts", "progress"):
            setattr(self, key, getattr(other, key))

    def undo_once(self, *, redo=False):
        result = self.undo.redo(self.to_dict()) if redo else self.undo.undo(self.to_dict())
        if result is not None:
            self.restore(result)
            return True
        return False

    def calibrate(self, source_ms, reference_ms, evidence):
        updated = self.clock.with_anchor(source_ms, reference_ms, evidence)
        self.set_clock(updated)

    def set_clock(self, updated):
        self.checkpoint()
        self.mapping_history.append(self.clock.to_dict())
        self.clock = updated
        for event in self.project.events:
            if event.extras.get("confirmation") == "confirmed":
                event.extras["confirmation"] = "needs_review"

    def add_draft(self, label_index, start, end, evidence, *, group_id=None, note=""):
        if end is not None and end < start:
            start, end = end, start
        if not math.isfinite(start) or end is not None and not math.isfinite(end):
            raise ValueError("动作时间必须为有效数值")
        self.checkpoint()
        draft = {"id": uuid.uuid4().hex, "group_id": group_id or uuid.uuid4().hex,
                 "label_index": label_index, "reference_start": start, "reference_end": end,
                 "video_evidence": copy.deepcopy(evidence), "cow_id": self.project.cow_id,
                 "confirmation": "video_draft", "note": note, **self.category_fields()}
        self.drafts.append(draft)
        return draft

    def project_draft(self, draft, duration_ms):
        if not self.clock.anchors:
            raise ValueError("请先钉住九轴与视频对应点，草稿已经保留")
        start = self.clock.map(draft["reference_start"], inverse=True)
        end = self.clock.map(draft["reference_end"], inverse=True) if draft["reference_end"] is not None else None
        if end is not None:
            if end <= 0 or start >= duration_ms:
                raise ValueError("这个动作不落在当前九轴记录范围")
            start, end = max(0.0, start), min(duration_ms, end)
        elif not 0 <= start <= duration_ms:
            raise ValueError("这个动作点不落在当前九轴记录范围")
        return start, end

    def confirm_draft(self, draft_id, duration_ms, *, source_available=True, evidence_validator=None):
        draft = next(d for d in self.drafts if d["id"] == draft_id)
        if not source_available:
            raise ValueError("源文件不可用，不能确认真值；草稿保留")
        if not self.project.cow_id.strip():
            raise ValueError("请先确认本记录对应牛号，不能仅凭设备目录猜测")
        if draft.get("cow_id") and draft["cow_id"] != self.project.cow_id:
            raise ValueError("草稿牛号与当前记录不一致，请核对对象")
        start, end = self.project_draft(draft, duration_ms)
        if any(self.clock.quality(v) != "interpolated" for v in (start, end if end is not None else start)):
            raise ValueError("该范围尚未被前后校准点覆盖，或位于未确认区间；可继续保存视频草稿")
        if end is not None and any(a < end and b > start for a, b in self.clock.breaks):
            raise ValueError("动作跨越未确认的同步区间，请分段复核")
        evidence = draft["video_evidence"]
        if not evidence or not any(e.get("frame_ready") and e.get("verified_interval") for e in evidence):
            raise ValueError("视频画面/时间映射尚未确认到位，请回看并更新证据")
        if evidence_validator is not None and not evidence_validator(evidence):
            raise ValueError("视频素材或相机校准版本已变化，请回看并更新画面证据")
        self.checkpoint()
        previous = next((e for e in self.project.events if e.extras.get("draft_id") == draft_id), None)
        event = Event(previous.id if previous else self.project.next_event_id, draft["label_index"], start, end,
                      note=draft.get("note", ""), ev="video",
                      extras={**self.category_fields(), "confirmation": "confirmed", "mapping_revision": self.clock.revision,
                              "video_evidence": copy.deepcopy(evidence), "group_id": draft["group_id"],
                              "draft_id": draft_id, "asset_id": self.asset_id,
                              "reference_start": draft["reference_start"], "reference_end": draft["reference_end"]})
        if previous and previous.extras.get("screenshots"):
            # Keep the original provenance, even if later edits make it stale.
            event.extras["screenshots"] = copy.deepcopy(previous.extras["screenshots"])
        self.project.events = [e for e in self.project.events if e.extras.get("draft_id") != draft_id]
        self.project.events.append(event)
        if draft.get("model_candidate"):
            event.extras["model_candidate"] = copy.deepcopy(draft["model_candidate"])
        draft["confirmation"] = "confirmed"
        return event

    def training_project(self, *, source_available=True, evidence_validator=None):
        if not source_available:
            raise ValueError("九轴源文件缺失/变化，无法导出训练真值")
        if not self.project.cow_id.strip():
            raise ValueError("当前记录没有已确认牛号")
        project = copy.deepcopy(self.project)
        project.events = [e for e in project.events if e.extras.get("confirmation") == "confirmed"
                          and e.extras.get("mapping_revision") == self.clock.revision
                          and (evidence_validator is None or evidence_validator(e.extras.get("video_evidence", [])))]
        if not project.events:
            raise ValueError("没有通过同步与人工复核的真值；视频草稿可另行导出")
        for event in project.events:
            event.extras.pop("screenshots", None)  # Human review only, never training inputs.
        project.align["workspaceClock"] = self.clock.to_dict()
        project.extras["unreviewed_is_negative"] = False
        project.extras["training_scope"] = "confirmed_events_only"
        return project

    def edit_event(self, identifier, start, end, duration_ms, *, label_index=None, note=None):
        event = next(e for e in self.project.events if e.id == identifier)
        if not math.isfinite(start) or end is not None and not math.isfinite(end):
            raise ValueError("动作时间必须为有效数值")
        self.checkpoint()
        event.t0 = max(0, min(duration_ms, start))
        event.t1 = max(0, min(duration_ms, end)) if end is not None else None
        event.normalise_bounds()
        if label_index is not None:
            event.li = label_index
        if note is not None:
            event.note = note
        event.extras["confirmation"] = "needs_review"
        draft = next((d for d in self.drafts if d["id"] == event.extras.get("draft_id")), None)
        if draft:
            draft.update(reference_start=self.clock.map(event.t0),
                         reference_end=self.clock.map(event.t1) if event.t1 is not None else None,
                         label_index=event.li, note=event.note, video_evidence=[])
            event.extras.update(reference_start=draft["reference_start"], reference_end=draft["reference_end"], video_evidence=[])
        return event
