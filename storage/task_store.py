"""Errands Cora carries out with other people for the person she works with.

A task is the person's goal; each party is someone it needs, with a brief saying what to find out
or agree with them and what she may share. Nothing here is specific to anyone: every contact is
just a party row. Parties see only their own brief; the person sees the whole task.
"""

import json
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import PersonRecord, TaskPartyRecord, TaskRecord, TaskStepRecord

GOAL_MAX_CHARS = 1000
BRIEF_MAX_CHARS = 2000
OUTCOME_MAX_CHARS = 3000
STEP_MAX_CHARS = 4000
RESULT_MAX_CHARS = 4000
TASKS_CONTEXT_MAX_CHARS = 6000
OPEN = ("open", "waiting", "needs_person")
# Finished tasks stay in the person's view this long, so "what did Sarah say?" still has an answer.
RECENTLY_CLOSED = timedelta(days=3)


def clean(text: str, limit: int, what: str) -> str:
    text = text.strip()
    if not text or len(text) > limit:
        raise ValueError(f"{what} needs 1 to {limit} characters")
    return text


def bounded(items: list[dict]) -> str:
    """As many of the items as fit, as JSON; empty when there are none."""
    context, kept = "", []
    for item in items:
        candidate = json.dumps([*kept, item], ensure_ascii=False)
        if len(candidate) > TASKS_CONTEXT_MAX_CHARS:
            break
        kept.append(item)
        context = candidate
    return context


class TaskStore:
    """Use a caller-owned engine; each operation owns and closes its session."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _step(session: Session, task_id: UUID, text: str) -> None:
        session.add(TaskStepRecord(task_id=task_id, text=clean(text, STEP_MAX_CHARS, "A step")))

    def start(self, goal: str, person_id: UUID, brief: str, step: str) -> UUID:
        """A new task, waiting on its first party, who has just been contacted."""
        goal, brief = clean(goal, GOAL_MAX_CHARS, "A goal"), clean(brief, BRIEF_MAX_CHARS, "A brief")
        with Session(self.engine) as session, session.begin():
            task = TaskRecord(goal=goal, status="waiting")
            session.add(task)
            session.flush()
            session.add(TaskPartyRecord(task_id=task.id, channel="signal", person_id=person_id, brief=brief))
            self._step(session, task.id, step)
            return task.id

    def add_party(self, task_id: UUID, person_id: UUID, brief: str, step: str) -> None:
        """Someone more for an open task, or someone asked again with a new brief."""
        brief = clean(brief, BRIEF_MAX_CHARS, "A brief")
        with Session(self.engine) as session, session.begin():
            task = session.get(TaskRecord, task_id, with_for_update=True)
            if task is None or task.status not in OPEN:
                raise LookupError("No such open task")
            session.execute(insert(TaskPartyRecord).values(
                task_id=task_id, channel="signal", person_id=person_id, brief=brief,
            ).on_conflict_do_update(constraint="task_parties_person_key", set_={
                "brief": brief, "status": "waiting", "contacted_at": func.current_timestamp(), "nudges": 0,
            }))
            task.status, task.updated_at = "waiting", func.current_timestamp()
            self._step(session, task_id, step)

    def goal(self, task_id: UUID) -> str | None:
        """An open task's goal, or None."""
        with Session(self.engine) as session:
            return session.scalar(select(TaskRecord.goal).where(TaskRecord.id == task_id, TaskRecord.status.in_(OPEN)))

    def heard_from(self, person_id: UUID) -> None:
        """A party wrote: the hourly checks (later) nudge only those who have gone quiet."""
        with Session(self.engine) as session, session.begin():
            session.execute(update(TaskPartyRecord).where(
                TaskPartyRecord.person_id == person_id, TaskPartyRecord.status == "waiting",
                TaskPartyRecord.task_id.in_(select(TaskRecord.id).where(TaskRecord.status.in_(OPEN))),
            ).values(last_heard_at=func.current_timestamp()))

    def report(self, task_id: UUID, person_id: UUID, outcome: str) -> tuple[str, bool]:
        """A party's part is settled: record it, and finish the task once nobody is left waiting.

        Returns the goal and whether the task is now done; LookupError if they aren't a party to it.
        """
        outcome = clean(outcome, OUTCOME_MAX_CHARS, "An outcome")
        with Session(self.engine) as session, session.begin():
            task = session.get(TaskRecord, task_id, with_for_update=True)
            party = session.scalar(select(TaskPartyRecord).where(
                TaskPartyRecord.task_id == task_id, TaskPartyRecord.person_id == person_id,
            ))
            if task is None or task.status not in OPEN or party is None:
                raise LookupError("Not a party to an open task")
            name = session.scalar(select(PersonRecord.display_name).where(PersonRecord.id == person_id))
            party.status, party.outcome, party.last_heard_at = "done", outcome, func.current_timestamp()
            self._step(session, task_id, f"{name} reported: {outcome}")
            session.flush()
            parties = session.execute(
                select(PersonRecord.display_name, TaskPartyRecord.status, TaskPartyRecord.outcome)
                .join(PersonRecord, PersonRecord.id == TaskPartyRecord.person_id)
                .where(TaskPartyRecord.task_id == task_id).order_by(TaskPartyRecord.contacted_at)
            ).all()
            done = all(status == "done" for _, status, _ in parties)
            task.updated_at = func.current_timestamp()
            if done:
                task.status, task.closed_at = "done", func.current_timestamp()
                task.result = "\n".join(f"{who}: {said}" for who, _, said in parties)[:RESULT_MAX_CHARS]
                self._step(session, task_id, "Done.")
            return task.goal, done

    def briefs_for(self, person_id: UUID) -> str:
        """For one party's own chat: their briefs in open tasks still waiting on them, nothing else."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(TaskRecord.id, TaskPartyRecord.brief)
                .join(TaskPartyRecord, TaskPartyRecord.task_id == TaskRecord.id)
                .where(TaskPartyRecord.person_id == person_id, TaskPartyRecord.status == "waiting",
                       TaskRecord.status.in_(OPEN))
                .order_by(TaskPartyRecord.contacted_at)
            ).all()
        return bounded([{"task_id": str(task_id), "brief": brief} for task_id, brief in rows])

    def tasks_context(self, now: datetime) -> str:
        """For the person: open tasks and those finished lately, with how each party stands."""
        with Session(self.engine) as session:
            tasks = session.scalars(select(TaskRecord).where(
                TaskRecord.status.in_(OPEN) | (TaskRecord.closed_at >= now - RECENTLY_CLOSED),
            ).order_by(TaskRecord.created_at)).all()
            parties = session.execute(
                select(TaskPartyRecord.task_id, PersonRecord.display_name, TaskPartyRecord.status,
                       TaskPartyRecord.outcome)
                .join(PersonRecord, PersonRecord.id == TaskPartyRecord.person_id)
                .where(TaskPartyRecord.task_id.in_([task.id for task in tasks]))
                .order_by(TaskPartyRecord.contacted_at)
            ).all()
        by_task: dict[UUID, list[dict]] = {}
        for task_id, name, status, outcome in parties:
            by_task.setdefault(task_id, []).append({"name": name, "status": status, "outcome": outcome})
        return bounded([{
            "task_id": str(task.id), "goal": task.goal, "status": task.status,
            "started": task.created_at.date().isoformat(), "parties": by_task.get(task.id, []),
        } for task in tasks])
