"""Stable label codes, not display names, bind reviewed algorithms to tools.

Health decisions need their own reviewed adapter; an event point predictor is
never silently treated as a pregnancy/disease classifier.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Algorithm:
    code: str
    title: str
    domain: str = "behavior"


BEHAVIORS = (
    Algorithm("STANDING_UP", "起立过程"), Algorithm("LYING_DOWN", "卧倒过程"),
    Algorithm("TAIL_RAISED", "抬尾"), Algorithm("TAIL_WAGGING", "甩尾"),
    Algorithm("URINATION", "排尿"), Algorithm("DEFECATION", "排便"),
    Algorithm("MOUNTING", "爬跨"), Algorithm("STRAINING_BOUT", "努责"),
)
HEALTH = tuple(Algorithm(code, title, "health") for code, title in (
    ("ESTRUS", "发情"), ("CALVING", "产犊"), ("PREGNANCY_EARLY", "孕早期"),
    ("PREGNANCY_MID", "孕中期"), ("PREGNANCY_LATE", "孕晚期"), ("DISEASE", "疫病")))


def bindings(spec, packs):
    if spec.domain != "behavior":
        return []  # Future health/decision outputs require a separate contract.
    return [(pack, model) for pack in packs for model in pack["models"] if model["code"] == spec.code]
