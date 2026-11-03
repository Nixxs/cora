"""Let Cora's recent experiences shape what she'd like to do or experience, at most once a day.

It reads her newest experiences: desires are about what draws her now, so a backlog (her
first reflection, or days with Cora closed) is skipped rather than worked through. Each day
it is due, her desires are settled first (so ones nothing reinforced fade), even
when there is too little new to reflect on.

A desire fulfilled closes the loop: the interests it grew from feel a strong pull, and what
happened becomes an experience her identity and later desires can grow from.

Usage: python -m subconscious.desires.reflect_desires [--dry-run]
"""

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import sys

from dotenv import load_dotenv
from openai import OpenAI

from session_config import BACKEND_MODEL
from storage.database import build_engine
from storage.model_settings_store import ModelSettingsStore
from subconscious.desires.desire_rules import Change, settle
from subconscious.desires.desire_store import Desire, DesireStore
from subconscious.desires.propose_desires import propose_desires
from subconscious.emotions.feeling_store import FeelingStore
from subconscious.identity.identity_evidence import ExperienceBatch, read_experiences
from subconscious.identity.identity_rules import is_held
from subconscious.identity.identity_store import IdentityStore
from subconscious.interests.interest_store import InterestStore, validate_now

REFLECT_EVERY = timedelta(hours=24)
MIN_NEW_EXPERIENCES = 3


def reflect_desires(
    store: DesireStore, client: OpenAI, model: str = BACKEND_MODEL, *, now: datetime,
) -> str | None:
    """Run one reflection if it is due; return a one-line outcome, or None if not due."""
    validate_now(now)
    last = store.last_reflected_at()
    if last is not None and now - last < REFLECT_EVERY:
        return None
    faded = fading(store.list_desires(), now)
    batch = read_experiences(store.engine, after=last, now=now, newest=True)
    waiting = waiting_for(batch)
    if waiting is not None:
        store.settle_all(now)
        return f"{waiting}{faded}"
    everything = store.list_desires(include_ended=True)
    desires = live(store.list_desires(), now)
    changes = propose_desires(client, model, desires, *what_she_has(store), batch.experiences, now=now,
                              histories=histories(store, desires), everything=everything)
    problems = store.apply_reflection(changes, read_after=last, read_until=batch.until, now=now)
    if problems is None:
        return "Another reflection ran first; nothing changed."
    fulfilled = [change.desire_id for change, problem in zip(changes, problems)
                 if problem is None and change.action == "fulfil"]
    if fulfilled:
        pull_interests(store, fulfilled, now)
    applied = [describe_change(change, desires) for change, problem in zip(changes, problems) if problem is None]
    refused = [problem for problem in problems if problem is not None]
    if refused:
        applied.append(f"{len(refused)} refused ({'; '.join(refused)})")
    felt = feel_fulfilled(store, [desire for desire in desires if desire.id in fulfilled], now)
    return f"Reflected on {len(batch.experiences)} experiences: {'; '.join(applied) or 'no changes'}{faded}{felt}."


def feel_fulfilled(store: DesireStore, fulfilled: list[Desire], now: datetime) -> str:
    """Fulfilling a wish leaves her strongly fulfilled; a failure here never undoes the reflection."""
    feelings = FeelingStore(store.engine)
    felt = 0
    for desire in fulfilled:
        try:
            feelings.record("fulfilment", "strong", f"fulfilled a wish: {desire.description}", now=now)
            felt += 1
        except Exception:
            pass
    return "; felt fulfilled" if felt else ""


def histories(store: DesireStore, desires: list[Desire]) -> dict:
    return {desire.id: store.history(desire.id) for desire in desires}


def pull_interests(store: DesireStore, fulfilled: list, now: datetime) -> None:
    """The interests a fulfilled desire grew from feel a strong pull (live ones only, within their ceiling)."""
    interests = InterestStore(store.engine)
    for desire_id in fulfilled:
        for interest_id in dict.fromkeys(store.sources(desire_id).interest_ids):
            interests.pull(interest_id, "strong", now=now)


def waiting_for(batch: ExperienceBatch) -> str | None:
    if not batch.experiences:
        return "No new experiences"
    if len(batch.experiences) < MIN_NEW_EXPERIENCES and not batch.more:
        return f"Waiting for more experiences ({len(batch.experiences)} of {MIN_NEW_EXPERIENCES})"
    return None


def live(desires: list[Desire], now: datetime) -> list[Desire]:
    """Desires that have not faded by now: the only ones the model is shown."""
    return [desire for desire in desires
            if settle(desire.strength, desire.strength_at, desire.status, now)[1] != "let_go"]


def fading(desires: list[Desire], now: datetime) -> str:
    gone = [desire.description for desire in desires if desire not in live(desires, now)]
    return f"; faded: {'; '.join(gone)}" if gone else ""


def what_she_has(store: DesireStore) -> tuple[list, list[str]]:
    """Her live interests and the traits she holds, for the model to read."""
    interests = InterestStore(store.engine).list_interests()
    traits = [trait.statement for trait in IdentityStore(store.engine).list_traits() if is_held(trait)]
    return interests, traits


def run_desire_cycle(*, should_stop: Callable[[], bool]) -> str | None:
    """One background reflection with its own engine, so an in-flight request can finish at shutdown."""
    engine = None
    try:
        if should_stop():
            return None
        engine = build_engine()
        model = ModelSettingsStore(engine).model_for("subconscious")
        with OpenAI(max_retries=0) as client:
            outcome = reflect_desires(DesireStore(engine), client, model=model, now=datetime.now(timezone.utc))
    except Exception as error:
        outcome = f"Desire reflection stopped ({type(error).__name__})."
    finally:
        if engine is not None:
            engine.dispose()
    return None if should_stop() else outcome


def preview_desires(store: DesireStore, client: OpenAI, model: str = BACKEND_MODEL, *, now: datetime) -> str:
    """Show what the model would change for the next batch, saving nothing."""
    validate_now(now)
    batch = read_experiences(store.engine, after=store.last_reflected_at(), now=now, newest=True)
    if not batch.experiences:
        return "No new experiences."
    desires = live(store.list_desires(), now)
    changes = propose_desires(client, model, desires, *what_she_has(store), batch.experiences, now=now,
                              histories=histories(store, desires),
                              everything=store.list_desires(include_ended=True))
    lines = [f"Proposal from {len(batch.experiences)} experiences (not saved; the rules are checked on saving):"]
    for change in changes:
        lines += [describe_change(change, desires), f"     why: {change.note}"]
        if change.action == "form":
            lines.append(f"     motivation: {change.motivation}")
    if len(lines) == 1:
        lines.append("no changes")
    return "\n".join(lines)


def describe_change(change: Change, desires: list[Desire]) -> str:
    if change.action == "form":
        return f"formed a candidate: {change.description}"
    desire = next(desire for desire in desires if desire.id == change.desire_id)
    if change.action == "reinforce":
        return f"reinforced ({change.strength}): {desire.description}"
    if change.action == "fulfil":
        return f"fulfilled: {desire.description}"
    return f"let go: {desire.description}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Let Cora's recent experiences shape what she'd like to do.")
    parser.add_argument("--dry-run", action="store_true", help="Show the model's proposal without saving it")
    parser.add_argument("--model", default=BACKEND_MODEL)
    args = parser.parse_args()
    load_dotenv()
    run = preview_desires if args.dry_run else reflect_desires
    try:
        engine = build_engine()
        try:
            with OpenAI(max_retries=0) as client:
                outcome = run(DesireStore(engine), client, args.model, now=datetime.now(timezone.utc))
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Desires not reflected ({type(error).__name__}). Check database and API settings.")
    print(outcome or "Already reflected in the last 24 hours.")


if __name__ == "__main__":
    main()
