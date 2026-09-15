"""Versioned event algorithms and calving evidence, independent of GUI state.

Imports stay lightweight. Training and signal processing run in the bounded
worker; original recordings and annotations are never modified here.
"""

SCHEMA = 'cowmata-algorithms-1'
EVENT_CODES = ('STANDING_UP', 'LYING_DOWN', 'STRAINING_BOUT', 'URINATION', 'TAIL_RAISED', 'TAIL_WAGGING')
EVENT_TITLES = {'STANDING_UP': '起立过程', 'LYING_DOWN': '卧倒过程', 'STRAINING_BOUT': '努责', 'URINATION': '排尿', 'TAIL_RAISED': '抬尾', 'TAIL_WAGGING': '甩尾'}
CALVING_CODES = frozenset({'STRAINING_BOUT', 'AMNIOTIC_SAC_FIRST_VISIBLE',
    'FETAL_PART_FIRST_VISIBLE', 'CALF_FULLY_EXPELLED', 'FETAL_MEMBRANES_FULLY_EXPELLED',
    'MANUAL_CALVING_ASSISTANCE'})
INDIVIDUAL_CODES = CALVING_CODES | {'MOUNTING'}


def validation_unit(code):
    """User protocol: pooled general events; individual reproductive episodes."""
    return 'cow' if code in INDIVIDUAL_CODES else 'record'


def canonical_event_code(code):
    return {'STANDING_TAIL_RAISED':'TAIL_RAISED','LYING_TAIL_RAISED':'TAIL_RAISED',
            'STANDING_TAIL_WAGGING':'TAIL_WAGGING','LYING_TAIL_WAGGING':'TAIL_WAGGING'}.get(code,code)
