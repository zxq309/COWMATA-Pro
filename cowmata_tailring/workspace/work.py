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
    history_video: dict = field(default_factory=dict)

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

    def identity_fields(self):
        identity = self.project.extras.get("device_identity")
        return {"device_identity": copy.deepcopy(identity)} if identity else {}

    def _sync_identity(self):
        for draft in self.drafts:
            draft.update(self.identity_fields())
        for event in self.project.events:
            event.extras.update(self.identity_fields())

    def bind_device_identity(self, identity, *, source_path, capture_timing):
        """Bind a checked folder to this content asset, never to a device globally."""
        previous = self.project.extras.get("device_identity", {})
        value = {**copy.deepcopy(identity), "asset_id": self.asset_id, "source_path": source_path,
                 "capture_timing": copy.deepcopy(capture_timing), "folder_cow_id": identity.get("cow_id", "")}
        if identity.get("status") == "ready":
            if not self.project.cow_id.strip():
                self.project.cow_id = identity["cow_id"]
                value["cow_id_origin"] = "folder"
            elif self.project.cow_id == identity["cow_id"]:
                value["cow_id_origin"] = previous.get("cow_id_origin", "manual")
            elif self.project.cow_id != identity["cow_id"]:
                value["cow_id_origin"] = "manual"
                already_checked = (previous.get("asset_id") == self.asset_id and
                    previous.get("source_folder") == identity.get("source_folder") and
                    previous.get("device_id") == identity.get("device_id") and
                    previous.get("manual_cow_id") == self.project.cow_id)
                value["status"] = "manual_override" if already_checked else "conflict"
                value["message"] = (f"目录耳标 {identity['cow_id']}，本记录牛号 {self.project.cow_id}。" +
                    ("已按本份记录人工核对，保留目录来源。" if already_checked else
                     "已保留原牛号；请在牛号框核对后按回车，相关标签需重新复核。"))
                if already_checked:
                    value["manual_cow_id"] = self.project.cow_id
                else:
                    if self.progress.get("status") == "done":
                        self.progress["status"] = "in_progress"
                    for event in self.project.events:
                        if event.extras.get("confirmation") == "confirmed":
                            event.extras["confirmation"] = "needs_review"
        elif previous.get("asset_id") == self.asset_id and previous.get("field_mark"):
            # Relocating a known recording into an old-style folder must not erase its saved provenance.
            value["saved_identity"] = copy.deepcopy(previous.get("saved_identity", previous))
            value["field_mark"] = previous["field_mark"]
            value["folder_cow_id"] = previous.get("folder_cow_id", "")
            value["identity_provenance"] = "saved_record_identity_current_folder_unverified"
        value["cow_id"] = self.project.cow_id
        self.project.extras["device_identity"] = value
        self._sync_identity()
        return value

    def confirm_cow(self, value):
        """An explicit edit/review applies only to the current recording."""
        value = value.strip()
        self.checkpoint()
        self.project.cow_id = value
        identity = self.project.extras.get("device_identity")
        if identity:
            identity["cow_id"] = value
            identity["cow_id_origin"] = "manual"
            if identity.get("status") in {"ready", "conflict", "manual_override"}:
                identity["status"] = "ready" if value == identity.get("folder_cow_id") else "manual_override"
                identity["manual_cow_id"] = value
                identity["message"] = (f"本记录牛号 {value}；目录耳标 {identity.get('folder_cow_id', '')}，已人工核对。")
        for event in self.project.events:
            if event.extras.get("confirmation") == "confirmed":
                event.extras["confirmation"] = "needs_review"
        self._sync_identity()

    def to_dict(self):
        from .resource_layout import PPG_PLACEHOLDER
        self.project.extras.setdefault("ppg", copy.deepcopy(PPG_PLACEHOLDER))
        return {"schema": 1, "asset_id": self.asset_id, "project": self.project.to_dict(),
                "clock": self.clock.to_dict(), "mapping_history": self.mapping_history,
                "drafts": self.drafts, "progress": self.progress,
                **({'history_video':self.history_video} if self.history_video else {})}

    @classmethod
    def from_dict(cls, data):
        video=data.get('video',{}) if 'work' in data else data.get('history_video',{})
        data=data.get('work',data)
        result=cls(data["asset_id"], Project.from_dict(data["project"]),
                   ClockMap.from_dict(data.get("clock", {})), data.get("mapping_history", []),
                   data.get("drafts", []), data.get("progress", {}))
        result.history_video=copy.deepcopy({key:video[key] for key in ('archive','camera_maps','camera_overrides','selected_cameras') if key in video})
        return result

    def checkpoint(self):
        self.undo.push(self.to_dict())
        if self.progress.get("status") == "done":
            self.progress["status"] = "in_progress"

    def restore(self, data):
        other = self.from_dict(data)
        for key in ("project", "clock", "mapping_history", "drafts", "progress", "history_video"):
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
        if self.clock.anchors and updated.anchors:
            for draft in self.drafts:
                previous = {'reference_start':draft['reference_start'],'reference_end':draft['reference_end'],
                            'mapping_revision':self.clock.revision}
                start = self.clock.map(draft['reference_start'],inverse=True)
                end = self.clock.map(draft['reference_end'],inverse=True) if draft['reference_end'] is not None else None
                draft.setdefault('alignment_history',[]).append(previous)
                draft.update(reference_start=updated.map(start),reference_end=updated.map(end) if end is not None else None)
                # A confirmed draft remains hidden behind its unchanged IMU event.
                if draft.get('confirmation')!='confirmed':
                    draft['confirmation']='needs_review'
                draft['video_evidence']=[]
        self.clock = updated
        for event in self.project.events:
            if event.extras.get("confirmation") == "confirmed":
                event.extras["confirmation"] = "needs_review"

    def assert_state_interval(self, label_index, start, end, *, exclude_draft=None, exclude_event=None):
        if self.project.labels[label_index].code not in {'STANDING','LYING','WALKING'} or end is None:
            return
        for draft in self.drafts:
            if draft['id']==exclude_draft or draft.get('confirmation')=='confirmed':
                continue
            if self.project.labels[draft['label_index']].code in {'STANDING','LYING','WALKING'}:
                if draft['reference_end'] is not None and start<draft['reference_end'] and end>draft['reference_start']:
                    raise ValueError('站立、躺卧、行走互斥；当前区间与已有状态标签重叠，请先调整起止或修改原标签。')
        if self.clock.anchors:
            for event in self.project.events:
                if event.id==exclude_event or event.extras.get('draft_id')==exclude_draft and exclude_draft is not None:
                    continue
                if self.project.labels[event.li].code in {'STANDING','LYING','WALKING'} and event.t1 is not None:
                    if start<self.clock.map(event.t1) and end>self.clock.map(event.t0):
                        raise ValueError('站立、躺卧、行走互斥；当前区间与已有状态标签重叠，请先调整起止或修改原标签。')

    def add_draft(self, label_index, start, end, evidence, *, group_id=None, note=""):
        if end is not None and end < start:
            start, end = end, start
        if not math.isfinite(start) or end is not None and not math.isfinite(end):
            raise ValueError("动作时间必须为有效数值")
        if end is not None and end == start:
            raise ValueError("结束时间必须晚于开始时间。")
        self.assert_state_interval(label_index,start,end)
        self.checkpoint()
        draft = {"id": uuid.uuid4().hex, "group_id": group_id or uuid.uuid4().hex,
                 "label_index": label_index, "reference_start": start, "reference_end": end,
                 "video_evidence": copy.deepcopy(evidence), "cow_id": self.project.cow_id,
                 "confirmation": "video_draft", "note": note, **self.category_fields(), **self.identity_fields()}
        self.drafts.append(draft)
        return draft

    def edit_draft(self, identifier, start, end, *, label_index=None, note=None):
        draft=next(d for d in self.drafts if d['id']==identifier)
        if not math.isfinite(start) or end is not None and not math.isfinite(end):
            raise ValueError('动作时间必须为有效数值')
        if end is not None and end<=start:
            raise ValueError('结束时间必须晚于开始时间。')
        index=draft['label_index'] if label_index is None else label_index
        self.assert_state_interval(index,start,end,exclude_draft=identifier)
        self.checkpoint()
        draft.update(label_index=index,reference_start=start,reference_end=end,
                     video_evidence=[],confirmation='video_draft')
        if note is not None:
            draft['note']=note
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
        if self.project.extras.get("device_identity", {}).get("status") == "conflict":
            raise ValueError("目录耳标与本记录牛号存在冲突，请先在牛号框核对并按回车确认")
        if not source_available:
            raise ValueError("源文件不可用，不能确认真值；草稿保留")
        if not self.project.cow_id.strip():
            raise ValueError("请先确认本记录对应牛号，不能仅凭设备目录猜测")
        if draft.get("cow_id") and draft["cow_id"] != self.project.cow_id:
            raise ValueError("草稿牛号与当前记录不一致，请核对对象")
        start, end = self.project_draft(draft, duration_ms)
        self.assert_state_interval(draft['label_index'],draft['reference_start'],draft['reference_end'],exclude_draft=draft_id)
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
                      extras={**self.category_fields(), **self.identity_fields(), "confirmation": "confirmed", "mapping_revision": self.clock.revision,
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
        if self.project.extras.get("device_identity", {}).get("status") == "conflict":
            raise ValueError("目录耳标与本记录牛号存在冲突，请先核对身份再导出训练真值")
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
        bounded_start = max(0, min(duration_ms, start))
        bounded_end = max(0, min(duration_ms, end)) if end is not None else None
        if bounded_end is not None and bounded_end == bounded_start:
            raise ValueError("结束时间必须晚于开始时间。")
        if self.clock.anchors:
            self.assert_state_interval(event.li if label_index is None else label_index,
                self.clock.map(min(bounded_start,bounded_end)) if bounded_end is not None else self.clock.map(bounded_start),
                self.clock.map(max(bounded_start,bounded_end)) if bounded_end is not None else None,
                exclude_event=identifier,exclude_draft=event.extras.get('draft_id'))
        self.checkpoint()
        event.t0 = bounded_start
        event.t1 = bounded_end
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
