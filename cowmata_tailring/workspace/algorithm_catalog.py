"""Stable label codes, not display names, bind reviewed algorithms to tools.

Health decisions need their own reviewed adapter; an event point predictor is
never silently treated as a pregnancy/disease classifier.
"""
from dataclasses import dataclass

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS


@dataclass(frozen=True)
class Algorithm:
    code: str
    title: str
    domain: str = "behavior"



BEHAVIORS = tuple(Algorithm(row['code'], row['name']) for row in DEFAULT_LABELS)
TAIL_MODELS = {'STANDING_TAIL_RAISED':'TAIL_RAISED','LYING_TAIL_RAISED':'TAIL_RAISED',
               'STANDING_TAIL_WAGGING':'TAIL_WAGGING','LYING_TAIL_WAGGING':'TAIL_WAGGING'}

HEALTH = tuple(Algorithm(code, title, "health") for code, title in (
    ("ESTRUS", "发情"), ("CALVING", "产犊"), ("PREGNANCY_EARLY", "孕早期"),
    ("PREGNANCY_MID", "孕中期"), ("PREGNANCY_LATE", "孕晚期"), ("DISEASE", "疫病")))


def bindings(spec, packs):
    if spec.domain != "behavior":
        return []  # Future health/decision outputs require a separate contract.
    return [(pack, model) for pack in packs for model in pack["models"] if model["code"] == TAIL_MODELS.get(spec.code, spec.code)]
