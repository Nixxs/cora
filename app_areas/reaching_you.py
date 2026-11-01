"""Telling you things: voice or Signal, heads-ups and passing messages on: part of CoraApp (see app.py)."""

from datetime import datetime

from textual import work

from messaging.setup import LINKED as SIGNAL_LINKED
from messaging.signal_cli import signal_failure
from subconscious.attention import PersonMessage


# The most things one client can pass on to the person in a day.
SIGNAL_RELAYS_PER_DAY = 5

def heads_up_text(heads_up) -> str:
    """Who, when and what kind, then her note: her words, never theirs."""
    when = heads_up.started_at.astimezone().strftime("%A %d %b").replace(" 0", " ")
    return f"Heads up: {heads_up.name} was {heads_up.conduct} in our chat on {when}. {heads_up.note}"


class ReachingYouMixin:
    """Telling you things: voice or Signal, heads-ups and passing messages on."""

    async def relay_to_person(
        self, client_id, client_name: str, text: str, task_id=None, settled: bool | None = None,
    ) -> str | None:
        """From a client's chat to the person, marked as theirs; None once on its way, else why not.

        Naming a task records their word on it, then the coordinator takes it to the person in the
        background, so it's kept even if the person can't be reached now.
        """
        return await self.pass_to_person(
            client_id, client_name, "via Cora", text, task_id, settled,
            lambda settled: (self.persistence.report_task, task_id, client_id, text, settled),
        )

    async def relay_from_email(self, party_id, party_task_id, who: str, text: str, task_id=None,
                               settled: bool | None = None) -> str | None:
        """pass_on from a task's email thread: only about that task, or something else for the person."""
        if task_id is not None and task_id != party_task_id:
            return "That isn't the task you're emailing them about; use its task_id, or pass it on without one."
        return await self.pass_to_person(
            party_id, who, "by email, via Cora", text, task_id, settled,
            lambda settled: (self.persistence.report_email_task, task_id, party_id, text, settled),
        )

    async def pass_to_person(self, sender_id, who: str, via: str, text: str, task_id, settled, report) -> str | None:
        """relay_to_person for any sender; report(settled) is the database call recording their word on a task."""
        key = (sender_id, datetime.now().date())
        if self.signal_relays.get(key, 0) >= SIGNAL_RELAYS_PER_DAY:
            return f"You've passed on as much as you can for {who} today; it wasn't sent."
        if task_id is not None:
            settled = settled is not False
            try:
                goal = await self.conversation_persistence.call_database(*report(settled))
            except LookupError:
                return "That isn't an open task you're asking them about; pass it on without the task_id."
            self.signal_relays[key] = self.signal_relays.get(key, 0) + 1
            self.activity.write(f"Recorded {who}'s word on a task.", "Task")
            self.tasks_coordinating.add(task_id)
            self.coordinate_task(
                task_id, f"{who} {'answered' if settled else 'needs something from the person'}: {text}",
                f"From {who}, {via}, for your task \"{goal}\":\n{text}", settled,
            )
            return None
        try:
            await self.tell_person(f"From {who}, {via}:\n{text}")
        except Exception:
            return "The person can't be reached right now; it wasn't sent."
        self.signal_relays[key] = self.signal_relays.get(key, 0) + 1
        self.activity.write(f"Passed something on from {who} to you.", "Signal relay")
        return None

    def can_reach_by_voice(self) -> bool:
        return not (self.closing or self.standby) and self.live.connection is not None and self.live.ready

    async def tell_person(self, text: str, delivered=None, undelivered=None) -> None:
        """Something of hers the person should know: said by voice with Live Active, otherwise on Signal.

        delivered() runs once it has gone (to Live or on Signal); undelivered() if Live couldn't say it
        and Signal failed too. Raises when neither way is open now.
        """
        async def done():
            if delivered is not None:
                await delivered()

        async def instead():
            try:
                await self.tell_person_on_signal(text)
            except Exception as error:
                self.activity.write(f"Telling you on Signal failed ({signal_failure(error)}).", "Signal error")
                if undelivered is not None:
                    undelivered()
                return
            await done()

        if self.can_reach_by_voice():
            self.attention.submit_message(PersonMessage(text, done, instead))
            return
        await self.tell_person_on_signal(text)
        await done()

    def signal_reaches_person(self) -> bool:
        return bool(
            self.signal.status.state == SIGNAL_LINKED and self.signal.status.account
            and self.signal_settings.person_recipient
        )

    async def tell_person_on_signal(self, text: str) -> None:
        account, person = self.signal.status.account, self.signal_settings.person_recipient
        if not self.signal_reaches_person():
            raise RuntimeError("Signal isn't set up to reach the person")
        await self.signal.client.send(account, person, text)
        # In the person's own chat and conversation, so voice Cora knows too.
        self.conversation.show_written_reply(text, label="Cora · Signal", channel="signal")
        self.activity.write("Told you something on Signal.", "Signal message")

    def send_unsaid_messages(self) -> None:
        """Live went to Standby: whatever she hadn't said yet goes on Signal instead."""
        for message in self.attention.withdraw_messages():
            self.run_worker(message.fallback(), group="tell-person", exit_on_error=False)

    @work(group="heads-ups", exclusive=True, exit_on_error=False)
    async def deliver_heads_ups(self) -> None:
        """Tell the person about chats where someone treated her badly, once each, in her words."""
        if self.closing:
            return
        try:
            heads_ups = await self.conversation_persistence.call_database(self.persistence.load_heads_ups)
        except Exception as error:
            self.activity.write(f"Heads-ups didn't load ({type(error).__name__}).", "Heads-up")
            return
        for heads_up in heads_ups:
            if self.closing:
                return
            if heads_up.conversation_id in self.heads_ups_pending:
                continue
            conversation_id = heads_up.conversation_id
            self.heads_ups_pending.add(conversation_id)
            try:
                await self.tell_person(
                    heads_up_text(heads_up),
                    delivered=lambda conversation_id=conversation_id: self.heads_up_told(conversation_id),
                    undelivered=lambda conversation_id=conversation_id: self.heads_ups_pending.discard(conversation_id),
                )
            except Exception as error:
                self.heads_ups_pending.discard(conversation_id)
                self.activity.write(f"A heads-up couldn't reach you yet ({type(error).__name__}); tried again next round.",
                                    "Heads-up")
                return

    async def heads_up_told(self, conversation_id) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.mark_heads_up_reported, conversation_id)
        except Exception as error:
            # Kept as pending this session, so it isn't sent twice; it may come again after a restart.
            self.activity.write(f"Saving that a heads-up went failed ({type(error).__name__}).", "Heads-up")
            return
        self.heads_ups_pending.discard(conversation_id)
        self.activity.write("Gave you a heads-up about a client chat.", "Heads-up")
