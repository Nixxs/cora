"""Validation plus opt-in disposable PostgreSQL tests; never reset personal data."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from storage.models import (
    MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL, InterestEventMemoryRecord,
    InterestEventRecord, InterestRecord, InterestWorkerStateRecord, MemoryRecord,
)
from subconscious.interest_proposals import Proposal, Reinforcement, Spawn
from subconscious.interest_store import FADE_REASON, MEMORY_BATCH_LIMIT, InterestStore, MemoryBatch
from subconscious.interest_strength import (
    HALF_LIFE, INITIAL_STRENGTH, MAX_LIVE_INTERESTS, MAX_REINFORCE_DELTA, reinforce,
)
import test_conversation_summaries as summary_tests

NOW = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)


def spawn_args(title="Old mechanical design"):
    return (title, "Why older machines were built as they were", "Nick is restoring a car", "Came up twice")


class InterestStoreValidationTests(unittest.TestCase):
    def test_invalid_inputs_are_rejected_before_connecting(self):
        engine = MagicMock()
        store = InterestStore(engine)
        with patch("subconscious.interest_store.Session") as session:
            for args in (
                ("", *spawn_args()[1:]), (" \n", *spawn_args()[1:]), ("x" * 201, *spawn_args()[1:]),
                (spawn_args()[0], "", *spawn_args()[2:]), (*spawn_args()[:2], " ", spawn_args()[3]),
                (*spawn_args()[:3], "x" * 1001), (*spawn_args()[:3], None),
            ):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    store.spawn(*args, now=NOW)
            with self.assertRaises(ValueError):
                store.spawn(*spawn_args(), now=datetime(2026, 10, 15))
            for delta in (0, -0.1, MAX_REINFORCE_DELTA + 0.001, True, float("nan"), "0.1"):
                with self.subTest(delta=delta), self.assertRaises(ValueError):
                    store.reinforce(uuid4(), delta, "Because", now=NOW)
            for reason in ("", " ", "x" * 1001):
                with self.subTest(reason=reason), self.assertRaises(ValueError):
                    store.reinforce(uuid4(), 0.1, reason, now=NOW)
            with self.assertRaises(ValueError):
                store.reinforce(uuid4(), 0.1, "Because", now=datetime(2026, 10, 15))
            with self.assertRaises(ValueError):
                store.settle_all(datetime(2026, 10, 15))
            for limit in (0, MEMORY_BATCH_LIMIT + 1, True, 1.0):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    store.read_new_memories(limit)
            session.assert_not_called()
        engine.connect.assert_not_called()


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class InterestStoreDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def setUp(self):
        # Only in the disposable DB created above. Children before parents.
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM public.interest_worker_state"))
            connection.execute(text("DELETE FROM public.interest_event_memories"))
            connection.execute(text("DELETE FROM public.interest_events"))
            connection.execute(text("DELETE FROM public.interests WHERE parent_interest_id IS NOT NULL"))
            connection.execute(text("DELETE FROM public.interests"))
            connection.execute(text("DELETE FROM public.memories"))
        summary_tests.SummaryDatabaseTests.setUp(self)
        self.interests = InterestStore(self.engine)

    def memory(self, content="I am restoring a 1968 car."):
        message = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role="user", text=content,
            channel="voice", status="completed",
        )
        with Session(self.engine) as session, session.begin():
            record = MemoryRecord(source_message_id=message.id, kind="fact", text=content, evidence=content)
            session.add(record)
            session.flush()
            return record.id

    def events(self, interest_id=None):
        query = select(InterestEventRecord).order_by(InterestEventRecord.created_at, InterestEventRecord.kind)
        if interest_id is not None:
            query = query.where(InterestEventRecord.interest_id == interest_id)
        with Session(self.engine) as session:
            return list(session.scalars(query))

    def links(self, event_id):
        with Session(self.engine) as session:
            return set(session.scalars(select(InterestEventMemoryRecord.memory_id).where(
                InterestEventMemoryRecord.event_id == event_id,
            )))

    def stored(self, interest_id):
        with Session(self.engine) as session:
            return session.get(InterestRecord, interest_id)

    def fill(self, count, now=NOW):
        return [self.interests.spawn(*spawn_args(f"Interest {index}"), now=now) for index in range(count)]

    def test_spawn_creates_emerging_interest_event_and_memory_links(self):
        memory_id = self.memory()
        parent = self.interests.spawn(*spawn_args("Cars"), now=NOW)
        interest = self.interests.spawn(
            "  Old mechanical design  ", "Why older machines were built as they were",
            "Nick is restoring a car", "Came up twice", now=NOW,
            parent_interest_id=parent.id, memory_ids=[memory_id, memory_id],
        )
        self.assertEqual(interest.title, "Old mechanical design")
        self.assertEqual((interest.strength, interest.status), (INITIAL_STRENGTH, "emerging"))
        self.assertEqual((interest.strength_at, interest.parent_interest_id), (NOW, parent.id))
        self.assertIsNone(interest.last_reinforced_at)
        with self.assertRaises(FrozenInstanceError):
            interest.title = "Other"
        [event] = self.events(interest.id)
        self.assertEqual((event.kind, event.strength_before, event.strength_after, event.reason),
                         ("spawn", None, INITIAL_STRENGTH, "Came up twice"))
        self.assertEqual(self.links(event.id), {memory_id})

    def test_spawn_is_refused_when_live_cap_is_full_and_writes_nothing(self):
        self.fill(MAX_LIVE_INTERESTS)
        self.assertIsNone(self.interests.spawn(*spawn_args("One too many"), now=NOW))
        self.assertEqual(len(self.interests.list_interests(include_dormant=True)), MAX_LIVE_INTERESTS)
        self.assertEqual(len(self.events()), MAX_LIVE_INTERESTS)

    def test_interests_that_decayed_to_dormant_free_room_before_being_settled(self):
        self.fill(MAX_LIVE_INTERESTS)
        later = NOW + HALF_LIFE + timedelta(days=1)  # 0.2 decays below 0.1
        self.assertIsNotNone(self.interests.spawn(*spawn_args("New"), now=later))

    def test_reinforce_settles_then_moves_toward_one(self):
        memory_id = self.memory()
        interest = self.interests.spawn(*spawn_args(), now=NOW)
        later = NOW + timedelta(days=1)
        settled = INITIAL_STRENGTH * 0.5 ** (1 / 7)
        result = self.interests.reinforce(interest.id, 0.15, "Talked about it again", now=later,
                                          memory_ids=[memory_id])
        self.assertAlmostEqual(result.strength, reinforce(settled, 0.15))
        self.assertEqual((result.status, result.strength_at, result.last_reinforced_at), ("active", later, later))
        event = self.events(interest.id)[-1]
        self.assertEqual(event.kind, "reinforce")
        self.assertAlmostEqual(event.strength_before, settled)
        self.assertEqual(self.links(event.id), {memory_id})

    def test_reinforcing_a_faded_interest_records_fade_then_reinforce(self):
        interest = self.interests.spawn(*spawn_args(), now=NOW)
        later = NOW + 2 * HALF_LIFE  # 0.2 -> 0.05, dormant; one reinforcement stays below active
        result = self.interests.reinforce(interest.id, 0.15, "Mentioned briefly", now=later)
        self.assertEqual(result.status, "dormant")
        kinds = sorted(event.kind for event in self.events(interest.id))
        self.assertEqual(kinds, ["fade", "reinforce", "spawn"])
        fade = next(event for event in self.events(interest.id) if event.kind == "fade")
        self.assertEqual(fade.reason, FADE_REASON)
        self.assertAlmostEqual(fade.strength_after, 0.05)

    def test_revive_needs_room_and_refusal_changes_nothing(self):
        dormant = self.interests.spawn(*spawn_args("Faded"), now=NOW)
        later = NOW + 2 * HALF_LIFE
        self.interests.settle_all(later)
        self.interests.reinforce(dormant.id, 0.15, "Came back up", now=later)  # 0.05 -> 0.19, still dormant
        before = self.stored(dormant.id)
        self.assertEqual(before.status, "dormant")
        self.fill(MAX_LIVE_INTERESTS, now=later)
        event_count = len(self.events())
        self.assertIsNone(self.interests.reinforce(dormant.id, 0.15, "Again", now=later))  # would be 0.31
        after = self.stored(dormant.id)
        self.assertEqual((after.strength, after.status, after.strength_at), (before.strength, "dormant", later))
        self.assertEqual(len(self.events()), event_count)

    def test_revive_with_room_records_revive_event(self):
        dormant = self.interests.spawn(*spawn_args("Faded"), now=NOW)
        later = NOW + 2 * HALF_LIFE
        self.interests.settle_all(later)
        statuses = [self.interests.reinforce(dormant.id, 0.15, "Back again", now=later).status
                    for _ in range(2)]
        self.assertEqual(statuses, ["dormant", "active"])
        self.assertEqual(self.events(dormant.id)[-1].kind, "revive")

    def test_reinforcing_a_live_interest_never_needs_room(self):
        interests = self.fill(MAX_LIVE_INTERESTS)
        self.assertEqual(self.interests.reinforce(interests[0].id, 0.15, "More", now=NOW).status, "active")

    def test_unknown_interest_is_rejected(self):
        with self.assertRaises(LookupError):
            self.interests.reinforce(uuid4(), 0.1, "Because", now=NOW)

    def test_settle_all_decays_fades_and_lists_by_strength(self):
        weak = self.interests.spawn(*spawn_args("Weak"), now=NOW)
        strong = self.interests.spawn(*spawn_args("Strong"), now=NOW)
        for _ in range(3):
            self.interests.reinforce(strong.id, 0.15, "Often discussed", now=NOW)
        later = NOW + HALF_LIFE + timedelta(days=1)
        settled = {interest.id: interest for interest in self.interests.settle_all(later)}
        self.assertEqual(settled[weak.id].status, "dormant")
        self.assertEqual(settled[strong.id].status, "active")
        self.assertEqual(settled[strong.id].strength_at, later)
        self.assertEqual([event.kind for event in self.events(weak.id)], ["spawn", "fade"])
        self.assertEqual([interest.id for interest in self.interests.list_interests()], [strong.id])
        self.assertEqual([interest.id for interest in self.interests.list_interests(include_dormant=True)],
                         [strong.id, weak.id])
        # Settling again records no second fade.
        self.interests.settle_all(later + timedelta(days=1))
        self.assertEqual([event.kind for event in self.events(weak.id)], ["spawn", "fade"])

    def test_clock_going_backwards_never_moves_strength_at_back(self):
        interest = self.interests.spawn(*spawn_args(), now=NOW)
        [settled] = self.interests.settle_all(NOW - timedelta(days=3))
        self.assertEqual((settled.strength, settled.strength_at), (INITIAL_STRENGTH, NOW))
        result = self.interests.reinforce(interest.id, 0.1, "Earlier clock", now=NOW - timedelta(days=1))
        self.assertEqual(result.strength_at, NOW)

    def test_concurrent_spawns_cannot_exceed_the_cap(self):
        self.fill(MAX_LIVE_INTERESTS - 1)
        barrier = threading.Barrier(4)

        def attempt(index):
            barrier.wait()
            return self.interests.spawn(*spawn_args(f"Race {index}"), now=NOW)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(attempt, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.interests.list_interests()), MAX_LIVE_INTERESTS)

    def test_unknown_memory_link_rolls_back_the_whole_spawn(self):
        with self.assertRaises(Exception):
            self.interests.spawn(*spawn_args(), now=NOW, memory_ids=[uuid4()])
        self.assertEqual(self.interests.list_interests(include_dormant=True), [])
        self.assertEqual(self.events(), [])


@unittest.skipUnless(os.environ.get("CORA_RUN_DB_TESTS") == "1", "Set CORA_RUN_DB_TESTS=1 for database tests")
class MemoryCursorDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(summary_tests.SummaryDatabaseTests.setUpClass.__func__)

    def saved(self, created_at, *, embedded=True, checked=True, duplicate_of=None, memory_id=None,
              role="user", content="A fact"):
        message = self.store.save_message(
            self.conversation_id, message_id=uuid4(), role=role, text=content,
            channel="voice", status="completed",
        )
        values = {"source_message_id": message.id, "kind": "fact", "text": content, "evidence": content,
                  "created_at": created_at, "duplicate_of": duplicate_of}
        if memory_id is not None:
            values["id"] = memory_id
        if embedded:
            values.update(embedding=[0.1] * MEMORY_EMBEDDING_DIMENSIONS,
                          embedding_model=MEMORY_EMBEDDING_MODEL, embedding_created_at=created_at)
        if checked:
            values["dedup_checked_at"] = created_at
        with Session(self.engine) as session, session.begin():
            record = MemoryRecord(**values)
            session.add(record)
            session.flush()
            return record.id

    def mark(self, memory_id, **changes):
        with Session(self.engine) as session, session.begin():
            record = session.get(MemoryRecord, memory_id)
            for name, value in changes.items():
                setattr(record, name, value)

    def ids(self, batch):
        return [memory.id for memory in batch.memories]

    def setUp(self):
        InterestStoreDatabaseTests.setUp(self)
        self.base = datetime.now(timezone.utc) - timedelta(days=1)

    def at(self, minutes):
        return self.base + timedelta(minutes=minutes)

    def test_no_memories_reads_an_empty_batch(self):
        batch = self.interests.read_new_memories()
        self.assertEqual(batch, MemoryBatch((), None, None, False))
        self.assertFalse(self.interests.advance_memory_cursor(batch))

    def test_reads_oldest_first_and_passes_over_duplicates(self):
        first = self.saved(self.at(0), content="Lives in Perth")
        second = self.saved(self.at(1), content="Restoring a 1968 car")
        duplicate = self.saved(self.at(2), duplicate_of=first, content="Lives in Perth, WA")
        batch = self.interests.read_new_memories()
        self.assertEqual(self.ids(batch), [first, second])
        self.assertEqual([memory.text for memory in batch.memories], ["Lives in Perth", "Restoring a 1968 car"])
        self.assertEqual((batch.after, batch.cursor, batch.waiting), (None, (self.at(2), duplicate), False))
        self.assertTrue(self.interests.advance_memory_cursor(batch))
        again = self.interests.read_new_memories()
        self.assertEqual(again, MemoryBatch((), batch.cursor, batch.cursor, False))
        self.assertFalse(self.interests.advance_memory_cursor(again))

    def test_stops_at_the_first_memory_not_yet_ready(self):
        first = self.saved(self.at(0))
        pending = self.saved(self.at(1), embedded=False, checked=False)
        later = self.saved(self.at(2))
        batch = self.interests.read_new_memories()
        self.assertEqual((self.ids(batch), batch.cursor, batch.waiting), ([first], (self.at(0), first), True))
        self.assertTrue(self.interests.advance_memory_cursor(batch))
        self.mark(pending, embedding=[0.1] * MEMORY_EMBEDDING_DIMENSIONS,
                  embedding_model=MEMORY_EMBEDDING_MODEL, embedding_created_at=self.at(1))
        embedded_only = self.interests.read_new_memories()
        self.assertEqual((self.ids(embedded_only), embedded_only.waiting), ([], True))
        self.assertEqual(embedded_only.cursor, embedded_only.after)
        self.mark(pending, dedup_checked_at=self.at(1))
        ready = self.interests.read_new_memories()
        self.assertEqual((self.ids(ready), ready.waiting), ([pending, later], False))

    def test_very_recent_memories_wait_for_slow_saves_to_commit(self):
        now = datetime.now(timezone.utc)
        recent = self.saved(now - timedelta(minutes=1))
        self.assertEqual(self.interests.read_new_memories(), MemoryBatch((), None, None, False))
        self.mark(recent, created_at=now - timedelta(minutes=10))
        self.assertEqual(self.ids(self.interests.read_new_memories()), [recent])

    def test_equal_times_are_ordered_by_id_across_batches(self):
        low, high = uuid4(), uuid4()
        low, high = sorted((low, high))
        self.saved(self.at(0), memory_id=high)
        self.saved(self.at(0), memory_id=low)
        first = self.interests.read_new_memories(limit=1)
        self.assertEqual(self.ids(first), [low])
        self.assertTrue(self.interests.advance_memory_cursor(first))
        self.assertEqual(self.ids(self.interests.read_new_memories(limit=1)), [high])

    def test_cursor_only_moves_from_the_position_the_batch_was_read_from(self):
        self.saved(self.at(0))
        second = self.saved(self.at(1))
        short = self.interests.read_new_memories(limit=1)
        full = self.interests.read_new_memories(limit=2)
        self.assertTrue(self.interests.advance_memory_cursor(full))
        self.assertFalse(self.interests.advance_memory_cursor(short))
        self.assertEqual(self.interests.read_new_memories().after, (self.at(1), second))

    def test_memories_from_non_user_messages_never_block_the_cursor(self):
        self.saved(self.at(0), embedded=False, checked=False, role="assistant")
        user = self.saved(self.at(1))
        self.assertEqual(self.ids(self.interests.read_new_memories()), [user])

    def state(self):
        with Session(self.engine) as session:
            return session.get(InterestWorkerStateRecord, 1)

    def wedding(self, *memory_ids, parent=None):
        return Spawn("Wedding customs", "How weddings differ", "A family wedding", "New theme",
                     parent, memory_ids)

    def test_evolution_applies_changes_moves_cursor_and_stamps_cycle_together(self):
        now = datetime.now(timezone.utc)
        car = self.interests.spawn(*spawn_args(), now=now - timedelta(days=1))
        first, second, third = (self.saved(self.at(minute)) for minute in range(3))
        batch = self.interests.read_new_memories()
        self.assertFalse(batch.more)
        evolution = self.interests.apply_evolution(batch, Proposal(
            (Reinforcement(car.id, 0.1, "Rebuilt the carburettor", (first, second)),),
            self.wedding(third, parent=car.id),
        ), now=now)
        self.assertEqual([interest.id for interest in evolution.reinforced], [car.id])
        self.assertEqual((evolution.spawned.title, evolution.spawned.parent_interest_id, evolution.refused),
                         ("Wedding customs", car.id, 0))
        state = self.state()
        self.assertEqual((state.memory_cursor_created_at, state.memory_cursor_id), batch.cursor)
        self.assertEqual(state.last_evolved_at, now)
        self.assertEqual(self.interests.last_evolved_at(), now)
        self.assertEqual(self.interests.read_new_memories().memories, ())
        events = InterestStoreDatabaseTests.events(self, car.id)
        self.assertEqual([event.kind for event in events], ["spawn", "reinforce"])
        self.assertEqual(InterestStoreDatabaseTests.links(self, events[1].id), {first, second})
        [spawn] = InterestStoreDatabaseTests.events(self, evolution.spawned.id)
        self.assertEqual(InterestStoreDatabaseTests.links(self, spawn.id), {third})

    def test_stale_batch_changes_nothing(self):
        memory_id = self.saved(self.at(0))
        batch = self.interests.read_new_memories()
        self.assertTrue(self.interests.advance_memory_cursor(batch))
        self.assertIsNone(self.interests.apply_evolution(
            batch, Proposal((), self.wedding(memory_id)), now=datetime.now(timezone.utc)))
        self.assertEqual(self.interests.list_interests(include_dormant=True), [])
        self.assertIsNone(self.interests.last_evolved_at())

    def test_a_failed_change_rolls_back_the_cursor_and_the_whole_cycle(self):
        now = datetime.now(timezone.utc)
        car = self.interests.spawn(*spawn_args(), now=now - timedelta(days=1))
        memory_id = self.saved(self.at(0))
        batch = self.interests.read_new_memories()
        with self.assertRaises(Exception):
            self.interests.apply_evolution(batch, Proposal(
                (Reinforcement(car.id, 0.1, "Rebuilt it", (memory_id,)),), self.wedding(uuid4()),
            ), now=now)
        self.assertIsNone(self.state())
        self.assertEqual(self.interests.read_new_memories(), batch)
        self.assertEqual([interest.id for interest in self.interests.list_interests()], [car.id])
        self.assertEqual(self.stored(car.id).strength, INITIAL_STRENGTH)

    def test_full_cap_refuses_the_spawn_but_the_cycle_completes(self):
        now = datetime.now(timezone.utc)
        InterestStoreDatabaseTests.fill(self, MAX_LIVE_INTERESTS, now)
        memory_id = self.saved(self.at(0))
        batch = self.interests.read_new_memories()
        evolution = self.interests.apply_evolution(batch, Proposal((), self.wedding(memory_id)), now=now)
        self.assertEqual((evolution.reinforced, evolution.spawned, evolution.refused), ((), None, 1))
        self.assertEqual(self.interests.last_evolved_at(), now)
        self.assertEqual(len(self.interests.list_interests()), MAX_LIVE_INTERESTS)

    def test_cycle_settles_every_interest_and_records_fades(self):
        now = datetime.now(timezone.utc)
        old = self.interests.spawn(*spawn_args(), now=now - timedelta(days=30))
        self.saved(self.at(0))
        evolution = self.interests.apply_evolution(self.interests.read_new_memories(), Proposal((), None), now=now)
        self.assertEqual(evolution.reinforced, ())
        self.assertEqual(self.stored(old.id).status, "dormant")
        self.assertEqual(self.stored(old.id).strength_at, now)
        self.assertEqual([event.kind for event in InterestStoreDatabaseTests.events(self, old.id)],
                         ["spawn", "fade"])

    stored = InterestStoreDatabaseTests.stored


if __name__ == "__main__":
    unittest.main()
