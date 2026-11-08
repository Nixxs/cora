"""Evolve Cora's interests from new memories and her own sparks, at most once a day.

Usage: python -m subconscious.interests.evolve_interests [--dry-run]
"""

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import sys

from openai_key import load_environment
from openai import OpenAI

from session_config import BACKEND_MODEL
from storage.database import build_engine
from storage.model_settings_store import ModelSettingsStore
from subconscious.interests.interest_proposals import Proposal
from subconscious.interests.interest_store import Interest, InterestStore, validate_now
from subconscious.interests.propose_interests import propose_interests

EVOLVE_EVERY = timedelta(hours=24)
MIN_NEW_MEMORIES = 3


def evolve_interests(
    store: InterestStore, client: OpenAI, model: str = BACKEND_MODEL, *, now: datetime,
) -> str | None:
    """Run one evolution cycle if it is due; return a one-line outcome, or None if not due."""
    validate_now(now)
    last = store.last_evolved_at()
    if last is not None and now - last < EVOLVE_EVERY:
        return None
    batch = store.read_new_memories()
    # Her own sparks don't wait for the person: with any, the cycle runs on whatever memories there are.
    sparks = store.read_sparks(now=now)
    if not sparks:
        if not batch.memories:
            if store.advance_memory_cursor(batch):
                return "Passed over duplicate memories."
            return "Waiting for memories to finish processing." if batch.waiting else "No new memories."
        if len(batch.memories) < MIN_NEW_MEMORIES and not batch.more:
            return f"Waiting for more memories ({len(batch.memories)} of {MIN_NEW_MEMORIES})."
    interests = store.list_interests(include_dormant=True)
    proposal = propose_interests(client, model, interests, batch.memories, now=now, sparks=sparks)
    evolution = store.apply_evolution(batch, proposal, now=now, sparks=sparks)
    if evolution is None:
        return "Memories or sparks were used by another cycle; nothing changed."
    changes = [f"reinforced {interest.title}" for interest in evolution.reinforced]
    if evolution.spawned is not None:
        own = " (her own)" if evolution.spawned.origin == "own" else ""
        changes.append(f"new interest {evolution.spawned.title}{own}")
    if evolution.refused:
        changes.append(f"{evolution.refused} refused (live interests full)")
    return f"Evolved from {sources(batch.memories, sparks)}: {'; '.join(changes) or 'no changes'}."


def sources(memories, sparks) -> str:
    parts = [f"{len(memories)} memories"] if memories or not sparks else []
    if sparks:
        parts.append(f"{len(sparks)} of her own sparks")
    return " and ".join(parts)


def run_interest_cycle(*, should_stop: Callable[[], bool]) -> str | None:
    """One background cycle with its own engine, so an in-flight request can finish at shutdown."""
    engine = None
    try:
        if should_stop():
            return None
        engine = build_engine()
        model = ModelSettingsStore(engine).model_for("subconscious")
        with OpenAI(max_retries=0) as client:
            outcome = evolve_interests(InterestStore(engine), client, model=model, now=datetime.now(timezone.utc))
    except Exception as error:
        outcome = f"Interest cycle stopped ({type(error).__name__})."
    finally:
        if engine is not None:
            engine.dispose()
    return None if should_stop() else outcome


def preview_interests(store: InterestStore, client: OpenAI, model: str = BACKEND_MODEL, *, now: datetime) -> str:
    """Show what the model would change for the next batch, saving nothing."""
    batch = store.read_new_memories()
    sparks = store.read_sparks(now=now)
    if not batch.memories and not sparks:
        return "No new memories or sparks."
    interests = store.list_interests(include_dormant=True)
    proposal = propose_interests(client, model, interests, batch.memories, now=now, sparks=sparks)
    return describe_proposal(proposal, interests, sources(batch.memories, sparks))


def describe_proposal(proposal: Proposal, interests: list[Interest], source: str) -> str:
    titles = {interest.id: interest.title for interest in interests}
    lines = [f"Proposal from {source} (not saved):"]
    for change in proposal.reinforcements:
        lines.append(f"+{change.delta:.2f} {titles[change.interest_id]}: {change.reason}")
    if proposal.spawn is not None:
        spawn = proposal.spawn
        own = " (her own)" if spawn.origin == "own" else ""
        lines += [f"new: {spawn.title}{own}", f"     {spawn.description}", f"     why: {spawn.why_emerged}"]
    if len(lines) == 1:
        lines.append("no changes")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve Cora's interests from new memories.")
    parser.add_argument("--dry-run", action="store_true", help="Show the model's proposal without saving it")
    parser.add_argument("--model", default=BACKEND_MODEL)
    args = parser.parse_args()
    load_environment()
    run = preview_interests if args.dry_run else evolve_interests
    try:
        engine = build_engine()
        try:
            with OpenAI(max_retries=0) as client:
                outcome = run(InterestStore(engine), client, args.model, now=datetime.now(timezone.utc))
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Interests not evolved ({type(error).__name__}). Check database and API settings.")
    print(outcome or "Interests already evolved in the last 24 hours.")


if __name__ == "__main__":
    main()
