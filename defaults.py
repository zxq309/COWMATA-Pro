from __future__ import annotations

from typing import Any


DEFAULT_PROTOCOL = "v3"


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


# 标注协议 v3：11 项正式区间标签；SYNC_ANCHOR 仅用于时间同步。
DEFAULT_LABELS: list[dict[str, Any]] = [
    _label(
        "站立", "standing", "STANDING", "1", "#5f86b3", "body_state",
        "四肢支撑并保持原地，未行走或采食。", overlap=False,
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
        "采食", "feeding", "FEEDING", "4", "#d58a32", "body_state",
        "明确取食或持续摄入饲料。", overlap=False,
    ),
    _label(
        "其他", "other", "OTHER", "5", "#8b929d", "body_state",
        "画面清楚但不属于站立、躺卧、行走或采食；饮水、梳毛、蹭痒等归入这里。",
        overlap=False,
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
        "敲击", "sync anchor", "SYNC_ANCHOR", "0", "#111827", "sync_anchor",
        "视频与九轴同步敲击点，不属于行为标签。", point=True, trainable=False,
    ),
]


# 旧工程字段仍可读取，但 v2 不再使用“情境推断”。
CONTEXT_OPTIONS: dict[str, list[str]] = {}
