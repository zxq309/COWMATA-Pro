from __future__ import annotations

from typing import Any

DEFAULT_PROTOCOL = "v4"


def _label(
    name: str,
    en: str,
    code: str,
    key: str,
    color: str,
    layer: str,
    definition: str,
    *,
    point: bool = False,
    overlap: bool = True,
    specialty: bool = False,
    trainable: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "en": en,
        "code": code,
        "key": key,
        "color": color,
        "type": "point" if point else "interval",
        "cat": "状态" if layer == "body_state" else "事件",
        "layer": layer,
        "minDur": 0.0,
        "overlap": overlap,
        "specialty": specialty,
        "trainable": trainable,
        "def": definition,
    }


# 标注协议 v4：16 项正式标签；SYNC_ANCHOR 仅用于时间同步。
DEFAULT_LABELS: list[dict[str, Any]] = [
    _label(
        "站立", "standing", "STANDING", "1", "#5f86b3", "body_state",
        "四肢支撑并保持原地，未发生行走。", overlap=False,
    ),
    _label(
        "躺卧", "lying", "LYING", "2", "#7c93aa", "body_state",
        "身体稳定卧地。", overlap=False,
    ),
    _label(
        "行走", "walking", "WALKING", "3", "#3c9b64", "body_state",
        "连续迈步并发生位置移动。", overlap=False,
    ),
    _label(
        "努责首次出现", "straining onset", "STRAINING_ONSET", "4",
        "#d97706", "calving_process",
        "首次出现明确、节律性的腹部用力；按首次清晰可确认帧记录为点事件。",
        point=True, specialty=True,
    ),
    _label(
        "努责区间", "straining bout", "STRAINING_BOUT", "5",
        "#f59e0b", "calving_process",
        "每段明确努责从腹部用力开始到该段用力结束；可与躺卧、站立、抬尾等标签重叠。",
        specialty=True,
    ),
    _label(
        "胎膜囊（水囊）首次可见", "amniotic sac first visible",
        "AMNIOTIC_SAC_FIRST_VISIBLE", "6", "#0ea5e9", "calving_process",
        "胎膜囊首次在阴门外清晰可见；建议连续可见至少1秒后，将时间回标到首次出现帧。",
        point=True, specialty=True,
    ),
    _label(
        "胎儿首个部位首次可见", "first fetal part visible",
        "FETAL_PART_FIRST_VISIBLE", "7", "#2563eb", "calving_process",
        "任一胎儿部位首次在阴门外清晰可见，同时在备注中记录前肢、后肢、头部、尾部或未知。",
        point=True, specialty=True,
    ),
    _label(
        "犊牛完全娩出", "calf fully expelled", "CALF_FULLY_EXPELLED", "8",
        "#dc2626", "calving_process",
        "犊牛最后一个身体部位完全越过阴门；脐带是否断裂不影响判定，作为核心T0。",
        point=True, specialty=True,
    ),
    _label(
        "胎膜完全排出", "fetal membranes fully expelled",
        "FETAL_MEMBRANES_FULLY_EXPELLED", "9", "#7c3aed",
        "calving_process",
        "胎膜完全脱离母体、不再悬挂于阴门；在备注中记录完全、部分或无法确认。",
        point=True, specialty=True,
    ),
    _label(
        "抬尾", "tail raised", "TAIL_RAISED", "Q", "#e85f68", "tail_action",
        "从明确抬起到明确落回的区间，可与身体行为和甩尾重叠。",
    ),
    _label(
        "甩尾", "tail wagging", "TAIL_WAGGING", "W", "#5266b5", "tail_action",
        "明确的连续摆尾区间，可与身体行为和抬尾重叠。",
    ),
    _label(
        "起立过程", "standing up", "STANDING_UP", "E", "#9a67cf",
        "posture_transition",
        "从牛开始主动收腿、抬起躯干，到四肢完全支撑并稳定站立的过程。",
    ),
    _label(
        "卧倒过程", "lying down", "LYING_DOWN", "R", "#8063ad",
        "posture_transition",
        "从牛开始屈曲四肢、降低躯干，到身体完全着地并稳定躺卧的过程。",
    ),
    _label(
        "排尿", "urination", "URINATION", "A", "#2b9c91",
        "objective_event", "从尿流清楚出现到尿流结束的区间。",
    ),
    _label(
        "排便", "defecation", "DEFECATION", "S", "#bd7b40",
        "objective_event", "从明确排便动作或粪便出现到排出结束的区间。",
    ),
    _label(
        "人工辅助产犊", "manual calving assistance",
        "MANUAL_CALVING_ASSISTANCE", "D", "#be185d", "calving_process",
        "从人工开始直接接触母牛或胎儿实施助产，到该次人工干预结束；多次分开的干预分别标注，可与其他分娩过程标签重叠。",
        specialty=True,
    ),
    _label(
        "敲击", "sync anchor", "SYNC_ANCHOR", "0", "#111827", "sync_anchor",
        "视频与九轴同步敲击点，不属于行为标签。", point=True, trainable=False,
    ),
]


# 旧工程字段仍可读取，但 v4 不使用“情境推断”。
CONTEXT_OPTIONS: dict[str, list[str]] = {}
