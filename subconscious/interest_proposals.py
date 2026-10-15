"""Check the model's proposed interest changes before anything is saved.

The model may reinforce interests it was shown, spawn at most one new interest, or do
nothing. Every change must cite memories from the batch it was given. Like memory
extraction, any invalid part rejects the whole proposal, so the batch is retried later.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from subconscious.interest_store import (
    TITLE_MAX_CHARS, Interest, NewMemory, clean_text, validate_delta,
)

REINFORCE_FIELDS = {"interest_id", "delta", "reason", "memory_ids"}
SPAWN_FIELDS = {"title", "description", "why_emerged", "reason", "parent_interest_id", "memory_ids"}


@dataclass(frozen=True)
class Reinforcement:
    interest_id: UUID
    delta: float
    reason: str
    memory_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class Spawn:
    title: str
    description: str
    why_emerged: str
    reason: str
    parent_interest_id: UUID | None
    memory_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class Proposal:
    reinforcements: tuple[Reinforcement, ...]
    spawn: Spawn | None


def title_key(title: str) -> str:
    return " ".join(title.split()).casefold()


def parse_id(value: object, allowed: set[UUID], name: str) -> UUID:
    if not isinstance(value, str):
        raise ValueError(f"Proposal {name} must be a string")
    parsed = UUID(value)
    if parsed not in allowed:
        raise ValueError(f"Proposal cites an unknown {name}")
    return parsed


def parse_memory_ids(value: object, memory_ids: set[UUID]) -> tuple[UUID, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("Each interest change must cite at least one memory")
    return tuple(dict.fromkeys(parse_id(item, memory_ids, "memory id") for item in value))


def validate_proposal(payload: object, interests: Sequence[Interest], memories: Sequence[NewMemory]) -> Proposal:
    """Pass every interest the model was shown; new titles must not repeat any of them."""
    if not isinstance(payload, dict) or set(payload) != {"reinforce", "spawn"}:
        raise ValueError("Invalid interest proposal")
    if not isinstance(payload["reinforce"], list):
        raise ValueError("Invalid interest proposal")
    interest_ids = {interest.id for interest in interests}
    memory_ids = {memory.id for memory in memories}
    reinforcements = []
    for row in payload["reinforce"]:
        if not isinstance(row, dict) or set(row) != REINFORCE_FIELDS:
            raise ValueError("Invalid reinforcement fields")
        interest_id = parse_id(row["interest_id"], interest_ids, "interest id")
        if any(item.interest_id == interest_id for item in reinforcements):
            raise ValueError("An interest can be reinforced only once per proposal")
        validate_delta(row["delta"])
        reinforcements.append(Reinforcement(
            interest_id, float(row["delta"]), clean_text(row["reason"], "reason"),
            parse_memory_ids(row["memory_ids"], memory_ids),
        ))
    spawn = payload["spawn"]
    if spawn is not None:
        if not isinstance(spawn, dict) or set(spawn) != SPAWN_FIELDS:
            raise ValueError("Invalid spawn fields")
        title = clean_text(spawn["title"], "title", TITLE_MAX_CHARS)
        if title_key(title) in {title_key(interest.title) for interest in interests}:
            raise ValueError("New interest repeats an existing title")
        parent = spawn["parent_interest_id"]
        spawn = Spawn(
            title, clean_text(spawn["description"], "description"),
            clean_text(spawn["why_emerged"], "why_emerged"), clean_text(spawn["reason"], "reason"),
            None if parent is None else parse_id(parent, interest_ids, "parent interest id"),
            parse_memory_ids(spawn["memory_ids"], memory_ids),
        )
    return Proposal(tuple(reinforcements), spawn)
