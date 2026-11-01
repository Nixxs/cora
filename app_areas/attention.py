"""The opening greeting, and observations and findings offered to Live: part of CoraApp (see app.py)."""

import asyncio
from datetime import datetime
from time import monotonic

from textual import work

from messaging.setup import (
    FAILED as SIGNAL_FAILED, LINKED as SIGNAL_LINKED, NOT_LINKED as SIGNAL_NOT_LINKED, OFF as SIGNAL_OFF,
)
from subconscious.event_bus import FindingEvent, ObservationEvent
from subconscious.interests.exploration_store import claim_mention_now, release_mention_now
from greeting import (
    choose_hook, finding_hook, greeting_brief, knows_them_well, last_thread_hook, memory_hook, signal_hook,
)


FINDING_IDLE_SECONDS = 90

# At launch, the greeting waits up to this long for Signal messages that waited while Cora was
# closed to be answered, counting them all in once nothing new has come for SIGNAL_QUIET_SECONDS.
SIGNAL_GREETING_WAIT_SECONDS = 20

SIGNAL_QUIET_SECONDS = 3


class AttentionMixin:
    """The opening greeting, and observations and findings offered to Live."""

    async def choose_launch_hook(self, last_talked_at, relationship=None):
        """Return (hook text, claimed finding to release if the greeting is not sent)."""
        options = []
        if last_talked_at:
            options = ["finding", "last_thread", *(["memory"] if knows_them_well(relationship) else [])]
        kind = choose_hook(options)
        if kind == "memory":
            plan = await self.conversation_persistence.load_recent_plan()
            if plan:
                return memory_hook(plan), None
            kind = "last_thread"  # nothing recent to ask about
        if kind == "finding":
            # Claiming counts as a mention, so the usual gap and daily limit apply.
            try:
                mention = await asyncio.to_thread(claim_mention_now)
            except Exception as error:
                self.activity.write(
                    f"Greeting finding check failed ({type(error).__name__})", "Greeting finding unavailable",
                )
                mention = None
            if mention is not None:
                return finding_hook(mention.interest_title, mention.finding), mention
            kind = "last_thread"  # nothing fresh to share
        if kind == "last_thread":
            summary = await self.conversation_persistence.load_last_summary()
            if summary:
                return last_thread_hook(summary), None
        return None, None

    def signal_catching_up(self) -> bool:
        """Whether Signal may still be delivering, or answering, messages that waited while Cora was closed."""
        if self.signal_startup is not None and not self.signal_startup.is_finished:
            return True  # its settings aren't even loaded yet
        if not self.signal_settings.enabled or self.signal.status.state in (SIGNAL_FAILED, SIGNAL_NOT_LINKED, SIGNAL_OFF):
            return False
        if self.signal.status.state != SIGNAL_LINKED:
            return True  # still starting or linking
        now = asyncio.get_running_loop().time()
        listener = self.signal_listener
        if listener is None or listener.connected_at is None or now - listener.connected_at < SIGNAL_QUIET_SECONDS:
            return True
        if self.signal_last_message_at is not None and now - self.signal_last_message_at < SIGNAL_QUIET_SECONDS:
            return True
        person = self.signal_settings.allowed_number
        answering = self.signal_answering.get(person)
        return bool(self.signal_waiting.get(person)) or (answering is not None and not answering.is_finished)

    async def wait_for_signal_catch_up(self) -> None:
        """At launch, let messages that waited on Signal be answered first, so the greeting can follow on."""
        deadline = asyncio.get_running_loop().time() + SIGNAL_GREETING_WAIT_SECONDS
        while self.signal_catching_up() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)

    async def release_greeting_finding(self, mention) -> None:
        if mention is None:
            return
        try:
            await asyncio.to_thread(release_mention_now, mention)
        except Exception as error:
            self.activity.write(
                f"Greeting finding release failed ({type(error).__name__})", "Greeting finding failed",
            )

    @work(group="greeting", exclusive=True)
    async def send_greeting(self, kind: str) -> None:
        mention = None
        if kind == "launch":
            last_talked_at = self.conversation_persistence.last_talked_at
            relationship = await self.conversation_persistence.load_relationship_values()
            await self.wait_for_signal_catch_up()
            if self.signal_launch_exchanges:
                # What they just said on Signal beats any other hook.
                hook = signal_hook(self.signal_launch_exchanges)
            else:
                hook, mention = await self.choose_launch_hook(last_talked_at, relationship)
            # The launch greeting stays pending while waiting, so typing or speaking still cancels it.
            if self.pending_greeting != "launch":
                self.activity.write("Opening greeting skipped: the person spoke first", "Greeting skipped")
                await self.release_greeting_finding(mention)
                return
            self.pending_greeting = None
            brief = greeting_brief(
                self.live.person_name, last_talked_at, datetime.now().astimezone(), hook, relationship,
            )
            send, sent = self.live.send_greeting(brief), "Opening greeting sent"
        else:
            send, sent = self.live.send_welcome_back(), "Welcome back sent"
        try:
            await asyncio.wait_for(send, timeout=5)
        except Exception as error:
            self.activity.write(f"Greeting send failed ({type(error).__name__})", "Greeting skipped")
            await self.release_greeting_finding(mention)
        else:
            self.activity.write(sent, "Greeting")

    def action_test_observation(self) -> None:
        self.queue_observation(
            "Simulated camera observation: the person just got a signal message. "
            "Their identity is unknown.", source="test",
        )

    def queue_observation(self, observation: str, *, source: str = "app") -> None:
        self.event_bus.publish(ObservationEvent(source=source, content=observation))

    def handle_event_bus_error(self, error: Exception) -> None:
        self.activity.write(
            f"Event subscriber failed ({type(error).__name__}); not retried.", "Event bus error",
        )

    def can_deliver_observation(self) -> bool:
        # The sender's own transition blocker must not block its readiness recheck.
        return (
            self.is_mounted and not (self.closing or self.reconnecting or self.standby)
            and self.live.ready
            and not any(name != "observation send" for name in self.live_transition_blockers())
        )

    def check_observations(self) -> None:
        self.attention.check()

    @work(group="observation-send")
    async def send_next_observation(self) -> None:
        await self.attention.deliver_next()

    def observation_delivery_finished(self) -> None:
        # Give Live a short chance to react; transcript/audio events extend this gap.
        self.last_live_activity = monotonic()
        self.record_idle_activity()

    def can_offer_finding(self) -> bool:
        # Only into a quiet, connected conversation: never wakes Live from standby.
        return (
            self.can_deliver_observation() and not self.live_transition_blockers()
            and not self.attention.pending
            and monotonic() - self.inactivity.last_activity >= FINDING_IDLE_SECONDS
        )

    def check_findings(self) -> None:
        if self.can_offer_finding():
            self.event_bus.publish(FindingEvent(source="interests"))
