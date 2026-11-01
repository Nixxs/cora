"""Background jobs: backups, memory, interests, identity, exploring and reaching out: part of CoraApp (see app.py)."""

import asyncio
from datetime import datetime
from time import monotonic

from textual.worker import get_current_worker
from textual import work

from backup import BackupError, daily_backup_due, run_backup
from services.connection import CONNECTED
from services.tools import result_text
from services.web_guard import refused_address
from subconscious.identity.reflect_identity import run_identity_cycle
from subconscious.interests.evolve_interests import run_interest_cycle
from subconscious.interests.exploration_store import claim_reach_out_now, release_reach_out_now
from subconscious.interests.browse_exploration import BROWSER_TOOLS, BrowserAccess
from subconscious.interests.explore_interest import run_exploration_cycle
from app_areas.shared import NUDGE_HOURS


# Explore only in standby or after this long without conversation.
EXPLORE_IDLE_SECONDS = 10 * 60

# One browser action while she explores, from the exploration thread (the service has its own limit).
BROWSER_CALL_SECONDS = 70


class BackgroundMixin:
    """Background jobs: backups, memory, interests, identity, exploring and reaching out."""

    @work(group="database-startup")
    async def check_interrupted_conversations(self) -> None:
        await self.conversation_persistence.check_interrupted_conversations()

    @work(thread=True, group="backup", exit_on_error=False)
    def daily_backup(self) -> None:
        """At launch, back the database up if the newest backup is a day old (see backup.py)."""
        outcome = self.back_up_if_due()
        if outcome is not None:
            self.call_from_thread(self.activity.write, *outcome)

    def back_up_if_due(self) -> tuple[str, str] | None:
        """Back up the database if due; returns what to say in Activity, or None if nothing was due."""
        try:
            if not daily_backup_due(datetime.now()):
                return None
            made, copied = run_backup(include_signal=False)
        except BackupError as error:
            # Docker's own reason, which never contains her data.
            return f"Daily backup failed: {error}", "Backup failed"
        except Exception as error:
            return f"Daily backup failed ({type(error).__name__}).", "Backup failed"
        where = f"; copied to {copied}" if copied else ""
        return f"Backup saved: {made[0].name}{where}", "Backup saved"

    def process_finished_conversations(self) -> None:
        # Single flight: a pass already running leaves this one for the next standby or launch.
        if self.summary_worker is None or self.summary_worker.is_finished:
            self.summary_worker = self.summarize_previous_conversations()

    @work(thread=True, group="memory-summary", exit_on_error=False)
    def summarize_previous_conversations(self) -> None:
        worker = get_current_worker()
        self.conversation_persistence.summarize_previous_conversations(
            should_stop=lambda: self.closing or worker.is_cancelled,
            dispatch_activity=lambda message, summary: self.call_from_thread(
                self.activity.write, message, summary,
            ),
        )
        # The pass rates how clients treated her; anything bad goes to the person now.
        if not (self.closing or worker.is_cancelled):
            self.call_from_thread(self.deliver_heads_ups)

    @work(group="background-round", exclusive=True, exit_on_error=False)
    async def background_round(self) -> None:
        """Hourly: close clients' chats quiet for the conversation gap, then run the memory pass on them."""
        if self.closing:
            return
        self.refresh_calendar()
        try:
            closed = await self.conversation_persistence.call_database(self.persistence.close_quiet_client_conversations)
        except Exception as error:
            self.activity.write(f"Closing quiet chats failed ({type(error).__name__}).", "Background round")
            return
        if closed and not self.closing:
            self.activity.write(f"Closed {closed} quiet client chat(s) for the memory pass.", "Background round")
            self.process_finished_conversations()
        # Retries any heads-up that couldn't reach the person before.
        if not self.closing:
            self.deliver_heads_ups()
            self.check_tasks()

    def check_interests(self) -> None:
        # Single flight: a slow model call simply delays the next check.
        if self.closing or (self.interest_worker is not None and not self.interest_worker.is_finished):
            return
        self.interest_worker = self.evolve_interests()

    @work(thread=True, group="interests", exit_on_error=False)
    def evolve_interests(self) -> None:
        worker = get_current_worker()
        outcome = run_interest_cycle(should_stop=lambda: self.closing or worker.is_cancelled)
        # Repeated outcomes (e.g. the same failure every check) are written once.
        if outcome is not None and outcome != self.interest_outcome:
            self.interest_outcome = outcome
            self.call_from_thread(self.activity.write, outcome, "Interests")

    def check_identity(self) -> None:
        if self.closing or (self.identity_worker is not None and not self.identity_worker.is_finished):
            return
        self.identity_worker = self.reflect_identity()

    @work(thread=True, group="identity", exit_on_error=False)
    def reflect_identity(self) -> None:
        worker = get_current_worker()
        outcome = run_identity_cycle(should_stop=lambda: self.closing or worker.is_cancelled)
        if outcome is not None and outcome != self.identity_outcome:
            self.identity_outcome = outcome
            self.call_from_thread(self.activity.write, outcome, "Identity")

    def can_explore(self) -> bool:
        if self.closing or self.reconnecting:
            return False
        if self.standby:
            return True
        return (
            self.live.ready and not self.live_transition_blockers()
            and monotonic() - self.inactivity.last_activity >= EXPLORE_IDLE_SECONDS
        )

    def check_exploration(self) -> None:
        self.check_reach_out()
        if not self.can_explore() or (
            self.exploration_worker is not None and not self.exploration_worker.is_finished
        ) or self.signing_up():
            return
        self.exploration_worker = self.explore_interest(self.exploration_browser(asyncio.get_running_loop()))

    def exploration_browser(self, loop, needs: frozenset[str] = BROWSER_TOOLS, keep_going=None,
                            title: str = "Exploration") -> BrowserAccess | None:
        """Her own browser for exploring, while the Browser service is connected with the tools it needs.

        Its calls come from the exploration thread and run here on the app's loop. She stops
        browsing (and writes up) as soon as you're back, a sign-up needs the browser or the app
        closes. A sign-up borrows it with its own tools, keep_going and Activity title.
        """
        status = self.services.status("playwright")
        if status.state != CONNECTED or not needs <= {tool.name for tool in status.tools}:
            return None

        def on_loop(coroutine, timeout):
            return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout)

        def call(tool, arguments):
            reply = on_loop(self.services.call_tool("playwright", tool, arguments), BROWSER_CALL_SECONDS)
            return result_text(reply), bool(reply.is_error)

        def on_page(url):
            if not self.closing:
                self.call_from_thread(self.activity.write, f"Browsing {url}", title)

        return BrowserAccess(
            call, refuse=lambda url: on_loop(refused_address(url), 30), on_page=on_page,
            keep_going=keep_going or (lambda: self.can_explore() and not self.signing_up()),
        )

    def check_reach_out(self) -> None:
        """Something from her exploring she really wants to tell the person: daytime only, and only
        claimed when she can reach them now (by voice, or on Signal); otherwise it waits, while fresh."""
        if self.closing or (self.reach_out_worker is not None and not self.reach_out_worker.is_finished):
            return
        if datetime.now().hour not in NUDGE_HOURS or not (self.can_reach_by_voice() or self.signal_reaches_person()):
            return
        self.reach_out_worker = self.reach_out()

    @work(group="reach-out", exit_on_error=False)
    async def reach_out(self) -> None:
        try:
            # Claiming counts towards the daily limit, so it can never go twice.
            claimed = await asyncio.to_thread(claim_reach_out_now)
        except Exception as error:
            self.activity.write(f"Checking for something to tell you failed ({type(error).__name__}).", "Reach out")
            return
        if claimed is None:
            return
        if self.closing:
            await self.release_reach_out(claimed)
            return

        async def delivered():
            self.activity.write(f"Cora reached out to you about {claimed.topic}: {claimed.why}", "Reach out")

        def undelivered():
            self.run_worker(self.release_reach_out(claimed), group="reach-out-release", exit_on_error=False)

        try:
            await self.tell_person(claimed.message, delivered=delivered, undelivered=undelivered)
        except Exception as error:
            self.activity.write(f"Reaching out to you failed ({type(error).__name__}); she'll try again.", "Reach out")
            await self.release_reach_out(claimed)

    async def release_reach_out(self, claimed) -> None:
        try:
            await asyncio.to_thread(release_reach_out_now, claimed)
        except Exception as error:
            self.activity.write(f"Releasing a message for you failed ({type(error).__name__}).", "Reach out")

    @work(thread=True, group="exploration", exit_on_error=False)
    def explore_interest(self, browser: BrowserAccess | None = None) -> None:
        worker = get_current_worker()
        outcome = run_exploration_cycle(should_stop=lambda: self.closing or worker.is_cancelled, browser=browser)
        if outcome is not None and outcome != self.exploration_outcome:
            self.exploration_outcome = outcome
            self.call_from_thread(self.activity.write, outcome, "Exploration")
