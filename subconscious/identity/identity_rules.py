"""Pure identity rules: what a single reflection may change about Cora's traits.

No database, model or clock access: callers pass in the current traits, when a
value last changed, and `now`. A trait is held (and reaches conversations) while
active with confidence >= HELD_CONFIDENCE. New traits start below that, so a trait
needs a later reflection with fresh support before it is held, and each revision
moves confidence only a step. The foundation lives in code and never appears here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from uuid import UUID

KINDS = ("value", "preference", "perspective", "tendency")
POLARITIES = ("like", "dislike")
ACTIONS = ("form", "revise", "retire")
EVIDENCE_SOURCES = ("interest_event", "exploration", "conversation")
OWN_SOURCES = ("interest_event", "exploration")  # Cora's own experience, not a conversation

HELD_CONFIDENCE = 0.5
FORM_MAX_CONFIDENCE = 0.4  # new traits are never held straight away
MAX_CONFIDENCE_STEP = 0.2  # largest change one revision may make
MAX_HELD = 12
MAX_ACTIVE = 24  # held plus still-forming traits
MAX_LEARNED_VALUES = 5
MIN_VALUE_CITATIONS = 2
VALUE_CHANGE_GAP = timedelta(days=7)
NEEDS_OWN_EVIDENCE = ("value", "preference", "perspective")


@dataclass(frozen=True)
class Trait:
    id: UUID
    kind: str
    statement: str
    polarity: str | None
    subject: str | None
    confidence: float
    status: str  # "active" | "retired"


@dataclass(frozen=True)
class Evidence:
    source: str  # one of EVIDENCE_SOURCES
    id: UUID


@dataclass(frozen=True)
class Change:
    """One proposed change: form a trait, or revise or retire an existing one."""

    action: str
    reason: str
    trait_id: UUID | None = None  # revise and retire
    kind: str | None = None  # form
    statement: str | None = None  # form and revise
    polarity: str | None = None  # preferences only
    subject: str | None = None  # form a preference
    confidence: float | None = None  # form and revise
    cited_trait_ids: tuple[UUID, ...] = ()
    evidence: tuple[Evidence, ...] = ()


def validate_confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("Confidence must be a finite number")
    if not 0 <= value <= 1:
        raise ValueError("Confidence must be from 0 to 1")
    return float(value)


def is_held(trait: Trait) -> bool:
    return trait.status == "active" and trait.confidence >= HELD_CONFIDENCE


def same_text(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and left.strip().casefold() == right.strip().casefold()


def check_reflection(
    changes: list[Change], traits: list[Trait], *, last_value_change_at: datetime | None, now: datetime,
) -> list[str | None]:
    """Return, for each change in order, None if allowed or why it is not.

    Each reflection may form, revise and retire at most one trait each, and change
    at most one value. Rejected changes do not count toward those limits.
    """
    if now.tzinfo is None or (last_value_change_at is not None and last_value_change_at.tzinfo is None):
        raise ValueError("Timestamps must be timezone-aware")
    used: set[str] = set()
    value_changed = last_value_change_at is not None and now - last_value_change_at < VALUE_CHANGE_GAP
    problems: list[str | None] = []
    for change in changes:
        problem = check_change(change, traits)
        if problem is None and change.action in used:
            problem = f"Only one {change.action} per reflection"
        if problem is None and touches_value(change, traits) and value_changed:
            problem = "Values change at most once a week"
        if problem is None:
            used.add(change.action)
            value_changed = value_changed or touches_value(change, traits)
        problems.append(problem)
    return problems


def touches_value(change: Change, traits: list[Trait]) -> bool:
    if change.action == "form":
        return change.kind == "value"
    return any(trait.id == change.trait_id and trait.kind == "value" for trait in traits)


def check_change(change: Change, traits: list[Trait]) -> str | None:
    """Why one change breaks the identity rules on its own, or None if it does not."""
    if change.action not in ACTIONS:
        return "Unknown action"
    if any(item.source not in EVIDENCE_SOURCES for item in change.evidence):
        return "Unknown evidence source"
    active = [trait for trait in traits if trait.status == "active"]
    if change.action == "form":
        return check_form(change, active)
    target = next((trait for trait in active if trait.id == change.trait_id), None)
    if target is None:
        return "No such active trait"
    if change.action == "retire":
        return None
    return check_revise(change, target, active)


def check_form(change: Change, active: list[Trait]) -> str | None:
    if change.kind not in KINDS:
        return "Unknown trait kind"
    problem = check_shape(change.kind, change.polarity, change.subject) or check_new_confidence(change)
    if problem:
        return problem
    if change.confidence > FORM_MAX_CONFIDENCE:
        return f"New traits start at {FORM_MAX_CONFIDENCE} or less"
    if len(active) >= MAX_ACTIVE:
        return "Too many active traits"
    if change.kind == "value" and sum(trait.kind == "value" for trait in active) >= MAX_LEARNED_VALUES:
        return "Too many learned values"
    if any(same_text(trait.statement, change.statement) for trait in active):
        return "Already a trait; revise it instead"
    if change.kind == "preference" and any(
        trait.kind == "preference" and same_text(trait.subject, change.subject) for trait in active
    ):
        return "Already a preference about this; revise it instead"
    return check_support(change, change.kind, None, active)


def check_revise(change: Change, target: Trait, active: list[Trait]) -> str | None:
    if change.kind not in (None, target.kind):
        return "A revision cannot change the trait's kind"
    if change.subject not in (None, target.subject):
        return "A revision cannot change the preference's subject"
    problem = check_shape(target.kind, change.polarity, target.subject) or check_new_confidence(change)
    if problem:
        return problem
    if abs(change.confidence - target.confidence) > MAX_CONFIDENCE_STEP + 1e-9:
        return f"Confidence moves at most {MAX_CONFIDENCE_STEP} per revision"
    becomes_held = target.confidence < HELD_CONFIDENCE <= change.confidence
    if becomes_held and sum(is_held(trait) for trait in active) >= MAX_HELD:
        return "Too many held traits"
    if any(trait.id != target.id and same_text(trait.statement, change.statement) for trait in active):
        return "Another trait already says this"
    return check_support(change, target.kind, target.id, active)


def check_shape(kind: str, polarity: str | None, subject: str | None) -> str | None:
    if kind == "preference":
        if polarity not in POLARITIES or subject is None:
            return "A preference needs like or dislike and a subject"
    elif polarity is not None or subject is not None:
        return "Only preferences have a polarity or subject"
    return None


def check_new_confidence(change: Change) -> str | None:
    if change.statement is None:
        return "A statement is required"
    try:
        validate_confidence(change.confidence)
    except ValueError as error:
        return str(error)
    return None


def check_support(change: Change, kind: str, own_id: UUID | None, active: list[Trait]) -> str | None:
    """Changes rest on evidence; values also rest on other held traits."""
    if not change.evidence:
        return "Evidence is required"
    if kind in NEEDS_OWN_EVIDENCE and not any(item.source in OWN_SOURCES for item in change.evidence):
        return "Needs her own experience (an interest change or finding), not only conversations"
    cited = set(change.cited_trait_ids)
    held = {trait.id: trait for trait in active if is_held(trait)}
    if own_id in cited or any(trait_id not in held for trait_id in cited):
        return "Cited traits must be other held traits"
    if kind == "value":
        if len(cited) < MIN_VALUE_CITATIONS:
            return f"A value must cite at least {MIN_VALUE_CITATIONS} held traits"
        if any(held[trait_id].kind == "value" for trait_id in cited):
            return "A value must rest on traits that are not values"
    return None
