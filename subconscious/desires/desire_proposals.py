"""Check the model's proposed desire changes before the store sees them.

This checks shape only: exact fields, types, the per-reflection limits, and that every id is
one the model was shown (desires it may change, interests and experiences it may cite).
Whether a change is allowed is decided per change by the store. A malformed reply rejects
the whole reflection, so the same experiences are read again next time.

The model is shown short ids (d1, i1, e1) rather than UUIDs, which it miscopies.
"""

from collections import Counter
from collections.abc import Sequence

from subconscious.desires.desire_rules import ACTIONS, MAX_PER_REFLECTION, Change
from subconscious.desires.desire_store import Desire, clean_change
from subconscious.identity.identity_evidence import Experience
from subconscious.identity.identity_proposals import experience_refs, optional_text, parse_ids
from subconscious.interests.interest_store import Interest

CHANGE_FIELDS = {"action", "desire_id", "description", "motivation", "strength", "reason",
                 "interest_ids", "evidence_ids"}


def desire_refs(desires: Sequence[Desire]) -> dict[str, Desire]:
    return {f"d{n}": desire for n, desire in enumerate(desires, 1)}


def interest_refs(interests: Sequence[Interest]) -> dict[str, Interest]:
    return {f"i{n}": interest for n, interest in enumerate(interests, 1)}


def validate_reflection(
    payload: object, desires: Sequence[Desire], interests: Sequence[Interest], experiences: Sequence[Experience],
) -> list[Change]:
    """Pass the desires, interests and experiences the model was shown."""
    if not isinstance(payload, dict) or set(payload) != {"changes"} or not isinstance(payload["changes"], list):
        raise ValueError("Invalid desire reflection")
    rows = payload["changes"]
    if not all(isinstance(row, dict) and set(row) == CHANGE_FIELDS for row in rows):
        raise ValueError("Invalid desire change fields")
    counts = Counter(row["action"] for row in rows)
    if any(action not in ACTIONS for action in counts):
        raise ValueError("Unknown desire action")
    if any(count > MAX_PER_REFLECTION[action] for action, count in counts.items()):
        raise ValueError("Too many desire changes of one kind")
    desire_ids = {ref: desire.id for ref, desire in desire_refs(desires).items()}
    interest_ids = {ref: interest.id for ref, interest in interest_refs(interests).items()}
    evidence = {ref: item.evidence for ref, item in experience_refs(experiences).items()}
    changes = []
    for row in rows:
        desire_id = None
        if row["desire_id"] is not None:
            [desire_id] = parse_ids([row["desire_id"]], desire_ids, "desire id")
        changes.append(clean_change(Change(
            row["action"], optional_text(row["reason"], "reason"), desire_id=desire_id,
            description=optional_text(row["description"], "description"),
            motivation=optional_text(row["motivation"], "motivation"),
            strength=optional_text(row["strength"], "strength"),
            interest_ids=parse_ids(row["interest_ids"], interest_ids, "interest id"),
            evidence=parse_ids(row["evidence_ids"], evidence, "experience id"),
        )))
    return changes
