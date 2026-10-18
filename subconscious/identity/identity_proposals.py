"""Check the model's proposed identity changes before the rules and store see them.

This checks shape only: exact fields, types, and that every id is one the model was
shown (traits it may change or cite, experiences it may cite). Whether a change is
allowed is decided per change by identity_rules. A malformed reply rejects the whole
reflection, so the same experiences are read again next time.
"""

from collections.abc import Sequence
from uuid import UUID

from subconscious.identity.identity_evidence import Experience
from subconscious.identity.identity_rules import ACTIONS, Change, Trait
from subconscious.identity.identity_store import clean_change

CHANGE_FIELDS = {"action", "trait_id", "kind", "statement", "polarity", "subject", "confidence",
                 "reason", "cited_trait_ids", "evidence_ids"}
RETIRE_EMPTY = ("kind", "statement", "polarity", "subject", "confidence")
MAX_CHANGES = len(ACTIONS)  # at most one form, revise and retire


def optional_text(value: object, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Identity {name} must be text or null")
    return value


def parse_ids(value: object, allowed: dict, name: str) -> tuple:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Identity {name} must be a list of ids")
    parsed = []
    for item in value:
        key = str(UUID(item))
        if key not in allowed:
            raise ValueError(f"Identity change cites an unknown {name}")
        parsed.append(allowed[key])
    return tuple(dict.fromkeys(parsed))


def validate_reflection(
    payload: object, traits: Sequence[Trait], experiences: Sequence[Experience],
) -> list[Change]:
    """Pass the active traits and experiences the model was shown."""
    if not isinstance(payload, dict) or set(payload) != {"changes"} or not isinstance(payload["changes"], list):
        raise ValueError("Invalid identity reflection")
    if len(payload["changes"]) > MAX_CHANGES:
        raise ValueError(f"At most {MAX_CHANGES} identity changes")
    trait_ids = {str(trait.id): trait.id for trait in traits if trait.status == "active"}
    evidence = {str(item.id): item.evidence for item in experiences}
    changes = []
    for row in payload["changes"]:
        if not isinstance(row, dict) or set(row) != CHANGE_FIELDS:
            raise ValueError("Invalid identity change fields")
        action = row["action"]
        if action not in ACTIONS:
            raise ValueError("Unknown identity action")
        if action == "form":
            if row["trait_id"] is not None:
                raise ValueError("A new trait has no trait id")
            trait_id = None
        elif row["trait_id"] is None:
            raise ValueError(f"To {action} a trait, give its id")
        else:
            [trait_id] = parse_ids([row["trait_id"]], trait_ids, "trait id")
        if action == "retire" and any(row[name] is not None for name in RETIRE_EMPTY):
            raise ValueError("Retiring takes only a reason and its support")
        confidence = row["confidence"]
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))):
            raise ValueError("Identity confidence must be a number or null")
        changes.append(clean_change(Change(
            action, optional_text(row["reason"], "reason"), trait_id=trait_id,
            kind=optional_text(row["kind"], "kind"), statement=optional_text(row["statement"], "statement"),
            polarity=optional_text(row["polarity"], "polarity"), subject=optional_text(row["subject"], "subject"),
            confidence=None if confidence is None else float(confidence),
            cited_trait_ids=parse_ids(row["cited_trait_ids"], trait_ids, "trait id"),
            evidence=parse_ids(row["evidence_ids"], evidence, "experience id"),
        )))
    return changes
