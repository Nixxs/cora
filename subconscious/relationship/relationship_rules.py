"""How one appraised conversation moves Cora's relationship with a person.

Each value runs from 0 to 1. A change moves a fraction of the remaining distance
to 1 (a gain) or to 0 (a loss), so values stay in bounds and each step shrinks
near the edges. Losses are larger than gains, so one hostile conversation undoes
several warm ones. Single appraisals are noisy, so every step is small. Trust and
warmth drift slowly back to baseline between conversations; familiarity only grows.
Development conversations count like any other: they are real time together.
"""

from dataclasses import dataclass
from datetime import timedelta

from subconscious.relationship.appraisal import Appraisal


@dataclass(frozen=True)
class RelationshipValues:
    trust: float
    warmth: float
    familiarity: float


BASELINE = RelationshipValues(trust=0.5, warmth=0.5, familiarity=0.0)
DRIFT_HALF_LIFE = timedelta(days=30)

FAMILIARITY_GAIN = 0.02
WARMTH_CHANGES = {"warm": 0.03, "neutral": 0.0, "curt": -0.08, "hostile": -0.2}
STEADY_TRUST_GAIN = 0.01
UNREPAIRED_FRICTION_TRUST_LOSS = 0.15
# Friction aimed at Cora that the person made amends for builds trust a little,
# and halves the warmth lost to the tone of that conversation.
REPAIRED_FRICTION_TRUST_GAIN = 0.03


def move(value: float, change: float) -> float:
    """Move a positive change toward 1 or a negative one toward 0, by that fraction."""
    target = 1.0 if change > 0 else 0.0
    return value + (target - value) * abs(change)


def drift(values: RelationshipValues, elapsed: timedelta) -> RelationshipValues:
    """Let trust and warmth ease back toward baseline; familiarity stays."""
    if elapsed <= timedelta(0):
        return values
    remaining = 0.5 ** (elapsed / DRIFT_HALF_LIFE)
    return RelationshipValues(
        trust=BASELINE.trust + (values.trust - BASELINE.trust) * remaining,
        warmth=BASELINE.warmth + (values.warmth - BASELINE.warmth) * remaining,
        familiarity=values.familiarity,
    )


def apply_appraisal(values: RelationshipValues, appraisal: Appraisal) -> RelationshipValues:
    """Return the values after one appraised conversation; Cora's own mistakes don't change them."""
    repaired = appraisal.repair in ("person", "both")
    warmth_change = WARMTH_CHANGES[appraisal.tone]
    if appraisal.friction == "at_cora":
        trust_change = REPAIRED_FRICTION_TRUST_GAIN if repaired else -UNREPAIRED_FRICTION_TRUST_LOSS
        if repaired and warmth_change < 0:
            warmth_change /= 2
    elif appraisal.tone in ("warm", "neutral"):
        trust_change = STEADY_TRUST_GAIN
    else:
        trust_change = 0.0
    return RelationshipValues(
        trust=move(values.trust, trust_change),
        warmth=move(values.warmth, warmth_change),
        familiarity=move(values.familiarity, FAMILIARITY_GAIN),
    )
