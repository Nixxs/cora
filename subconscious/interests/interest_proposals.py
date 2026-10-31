"""Check the model's proposed interest changes before anything is saved.

The model may reinforce interests it was shown, spawn at most one new interest, or do
nothing. Reinforcements cite memories from the batch it was given; a spawn cites those
memories, her own sparks (the explorations they came from), or both. Any invalid part
rejects the whole proposal, so the batch is retried later.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from subconscious.interests.interest_store import (
    TITLE_MAX_CHARS, Interest, NewMemory, Spark, clean_text, validate_delta,
)

REINFORCE_FIELDS = {"interest_id", "delta", "reason", "memory_ids"}
SPAWN_FIELDS = {"title", "description", "why_emerged", "reason", "parent_interest_id", "memory_ids", "exploration_ids"}


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
    exploration_ids: tuple[UUID, ...] = ()

    @property
    def origin(self) -> str:
        """Her own when only her exploring sparked it; anything from the person makes it theirs too."""
        return "conversation" if self.memory_ids else "own"


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


def parse_ids(value: object, allowed: set[UUID], name: str) -> tuple[UUID, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Proposal {name}s must be a list")
    return tuple(dict.fromkeys(parse_id(item, allowed, name) for item in value))


def parse_memory_ids(value: object, memory_ids: set[UUID]) -> tuple[UUID, ...]:
    cited = parse_ids(value, memory_ids, "memory id")
    if not cited:
        raise ValueError("Each interest change must cite at least one memory")
    return cited


def validate_proposal(
    payload: object, interests: Sequence[Interest], memories: Sequence[NewMemory], sparks: Sequence[Spark] = (),
) -> Proposal:
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
        cited_memories = parse_ids(spawn["memory_ids"], memory_ids, "memory id")
        cited_sparks = parse_ids(spawn["exploration_ids"], {spark.exploration_id for spark in sparks}, "exploration id")
        if not cited_memories and not cited_sparks:
            raise ValueError("A new interest must cite at least one memory or spark")
        spawn = Spawn(
            title, clean_text(spawn["description"], "description"),
            clean_text(spawn["why_emerged"], "why_emerged"), clean_text(spawn["reason"], "reason"),
            None if parent is None else parse_id(parent, interest_ids, "parent interest id"),
            cited_memories, cited_sparks,
        )
    return Proposal(tuple(reinforcements), spawn)
