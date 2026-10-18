"""Let Cora's recent experiences shape her identity traits, at most once a day.

Usage: python -m subconscious.identity.reflect_identity [--dry-run]
"""

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import sys

from dotenv import load_dotenv
from openai import OpenAI

from session_config import BACKEND_MODEL
from storage.database import build_engine
from subconscious.identity.identity_evidence import read_experiences
from subconscious.identity.identity_rules import Change, Trait, check_reflection
from subconscious.identity.identity_store import IdentityStore
from subconscious.identity.propose_identity import propose_identity
from subconscious.interests.interest_store import validate_now

REFLECT_EVERY = timedelta(hours=24)
MIN_NEW_EXPERIENCES = 3


def reflect_identity(
    store: IdentityStore, client: OpenAI, model: str = BACKEND_MODEL, *, now: datetime,
) -> str | None:
    """Run one reflection if it is due; return a one-line outcome, or None if not due."""
    validate_now(now)
    last = store.last_reflected_at()
    if last is not None and now - last < REFLECT_EVERY:
        return None
    batch = read_experiences(store.engine, after=last, now=now)
    if not batch.experiences:
        return "No new experiences."
    if len(batch.experiences) < MIN_NEW_EXPERIENCES and not batch.more:
        return f"Waiting for more experiences ({len(batch.experiences)} of {MIN_NEW_EXPERIENCES})."
    traits = store.list_traits()
    changes = propose_identity(client, model, traits, batch.experiences)
    problems = store.apply_reflection(changes, read_after=last, read_until=batch.until, now=now)
    if problems is None:
        return "Another reflection ran first; nothing changed."
    applied = [describe_change(change, traits) for change, problem in zip(changes, problems) if problem is None]
    refused = sum(problem is not None for problem in problems)
    if refused:
        applied.append(f"{refused} refused by the rules")
    return f"Reflected on {len(batch.experiences)} experiences: {'; '.join(applied) or 'no changes'}."


def run_identity_cycle(*, should_stop: Callable[[], bool]) -> str | None:
    """One background reflection with its own engine, so an in-flight request can finish at shutdown."""
    engine = None
    try:
        if should_stop():
            return None
        engine = build_engine()
        with OpenAI(max_retries=0) as client:
            outcome = reflect_identity(IdentityStore(engine), client, now=datetime.now(timezone.utc))
    except Exception as error:
        outcome = f"Identity reflection stopped ({type(error).__name__})."
    finally:
        if engine is not None:
            engine.dispose()
    return None if should_stop() else outcome


def preview_identity(store: IdentityStore, client: OpenAI, model: str = BACKEND_MODEL, *, now: datetime) -> str:
    """Show what the model would change for the next batch and what the rules think, saving nothing."""
    validate_now(now)
    batch = read_experiences(store.engine, after=store.last_reflected_at(), now=now)
    if not batch.experiences:
        return "No new experiences."
    traits = store.list_traits()
    changes = propose_identity(client, model, traits, batch.experiences)
    problems = check_reflection(changes, traits, last_value_change_at=store.last_value_change_at(), now=now)
    lines = [f"Proposal from {len(batch.experiences)} experiences (not saved):"]
    for change, problem in zip(changes, problems):
        lines += [describe_change(change, traits), f"     why: {change.reason}",
                  f"     rules: {'allowed' if problem is None else problem}"]
    if len(lines) == 1:
        lines.append("no changes")
    return "\n".join(lines)


def describe_change(change: Change, traits: list[Trait]) -> str:
    if change.action == "form":
        return f"formed {change.kind} at {change.confidence}: {change.statement}"
    trait = next(trait for trait in traits if trait.id == change.trait_id)
    if change.action == "revise":
        return f"revised {trait.kind} {trait.confidence} -> {change.confidence}: {change.statement}"
    return f"retired {trait.kind}: {trait.statement}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Let Cora's recent experiences shape her identity.")
    parser.add_argument("--dry-run", action="store_true", help="Show the model's proposal without saving it")
    parser.add_argument("--model", default=BACKEND_MODEL)
    args = parser.parse_args()
    load_dotenv()
    run = preview_identity if args.dry_run else reflect_identity
    try:
        engine = build_engine()
        try:
            with OpenAI(max_retries=0) as client:
                outcome = run(IdentityStore(engine), client, args.model, now=datetime.now(timezone.utc))
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Identity not reflected ({type(error).__name__}). Check database and API settings.")
    print(outcome or "Already reflected in the last 24 hours.")


if __name__ == "__main__":
    main()
