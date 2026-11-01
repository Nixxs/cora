"""Serialized foreground storage and conversation persistence lifecycle."""
import asyncio
import threading
from time import monotonic
from uuid import UUID, uuid4
from textual.worker import WorkerError
from memory.background import run_background_memory_pass
from session_config import BACKEND_MODEL

# How long the launch greeting waits for the last conversation to be summarized.
SUMMARY_WAIT_SECONDS = 30


class ConversationPersistence:
    def __init__(self, storage, *, schedule_save, is_closing,
                 set_memory_context, set_interest_context, set_identity_context, set_relationship_context,
                 set_self_image_context=lambda context: None, set_people_context=lambda context: None,
                 set_tasks_context=lambda context: None, set_sign_ups_context=lambda context: None,
                 set_person_name, set_delegation_model, write_activity, notify):
        self.storage = storage
        self.schedule_save = schedule_save
        self.is_closing = is_closing
        self.set_memory_context = set_memory_context
        self.set_interest_context = set_interest_context
        self.set_identity_context = set_identity_context
        self.set_self_image_context = set_self_image_context
        self.set_people_context = set_people_context
        self.set_tasks_context = set_tasks_context
        self.set_sign_ups_context = set_sign_ups_context
        self.set_relationship_context = set_relationship_context
        self.set_person_name = set_person_name
        self.set_delegation_model = set_delegation_model
        self.write_activity = write_activity
        self.notify = notify
        self.lock = asyncio.Lock()
        self.workers = []
        self.recording_closed = False
        # Loaded once at launch for the opening greeting. None with loaded=True means they
        # have never talked; loaded=False means unknown, so a stranger intro would be wrong.
        self.last_talked_at = None
        self.last_talked_loaded = False
        # Set from the background pass thread once this launch's summaries are written.
        self.summaries_done = threading.Event()

    async def call_database(self, operation, *args):
        # Serialise off-thread calls, including lazy engine creation and shutdown.
        async with self.lock:
            return await asyncio.to_thread(operation, *args)

    def record_chat_message(self, role: str, text: str, channel: str) -> None:
        if self.recording_closed:
            return
        self.workers = [worker for worker in self.workers if not worker.is_finished]
        self.workers.append(self.schedule_save(uuid4(), role, text, channel))

    async def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str) -> None:
        try:
            await self.call_database(self.storage.save_chat_message, message_id, role, text, channel)
        except Exception as error:
            self.write_activity(
                f"Chat save failed ({type(error).__name__}); displayed text remains available.",
                "Chat save failed",
            )
            self.notify("Conversation text could not be saved to the database.", severity="warning")
        else:
            self.write_activity(f"Chat text saved: role={role}, channel={channel}", "Chat text saved")

    async def check_interrupted_conversations(self) -> None:
        try:
            count = await self.call_database(self.storage.mark_interrupted_conversations)
        except Exception as error:
            self.write_activity(
                f"Interrupted conversation check failed ({type(error).__name__})",
                "Database startup check failed",
            )
            self.notify("Could not tag unfinished conversations in the database.", severity="warning")
        else:
            if count:
                self.write_activity(
                    f"Tagged {count} unfinished conversation(s) as interrupted.",
                    "Unfinished conversations tagged",
                )
        if not self.is_closing():
            try:
                self.last_talked_at = await self.call_database(self.storage.load_last_talked_at)
            except Exception as error:
                self.write_activity(
                    f"Last talk time load failed ({type(error).__name__}); skipping the greeting.",
                    "Last talk time unavailable",
                )
            else:
                self.last_talked_loaded = True
        await self.load_session_context()

    async def load_session_context(self) -> None:
        # Runs before every Live connect so waking from standby sees fresh memories and interests.
        if self.is_closing():
            return
        # Without a name Cora treats the person as someone she has not been introduced to.
        self.set_person_name(None)
        try:
            name = await self.call_database(self.storage.load_person_name)
        except Exception as error:
            self.write_activity(
                f"Person load failed ({type(error).__name__}); connecting without a name.",
                "Person unavailable",
            )
        else:
            if not self.is_closing():
                self.set_person_name(name)

        if self.is_closing():
            return
        self.set_memory_context("")
        try:
            context = await self.call_database(self.storage.load_startup_memory_context)
        except Exception as error:
            self.write_activity(
                f"Startup memory load failed ({type(error).__name__}); connecting without memories.",
                "Memory context unavailable",
            )
            self.notify("Could not load saved memories; connecting without them.", severity="warning")
        else:
            if not self.is_closing():
                self.set_memory_context(context)
                self.write_activity(
                    f"Startup memory context loaded ({len(context)} characters).", "Memory context loaded",
                )

        # Interests load separately so a failure here never costs Cora her memories.
        if self.is_closing():
            return
        self.set_interest_context("")
        try:
            context = await self.call_database(self.storage.load_startup_interest_context)
        except Exception as error:
            self.write_activity(
                f"Startup interest load failed ({type(error).__name__}); connecting without interests.",
                "Interest context unavailable",
            )
        else:
            if not self.is_closing():
                self.set_interest_context(context)
                self.write_activity(
                    f"Startup interest context loaded ({len(context)} characters).", "Interest context loaded",
                )

        # Her chosen picture of herself loads on its own too.
        if self.is_closing():
            return
        self.set_self_image_context("")
        try:
            context = await self.call_database(self.storage.load_self_image_context)
        except Exception as error:
            self.write_activity(
                f"Self-image load failed ({type(error).__name__}); connecting without it.",
                "Self-image unavailable",
            )
        else:
            if not self.is_closing():
                self.set_self_image_context(context)

        # Identity also loads on its own, so a failure here only costs her self-view.
        if self.is_closing():
            return
        self.set_identity_context("")
        try:
            context = await self.call_database(self.storage.load_startup_identity_context)
        except Exception as error:
            self.write_activity(
                f"Startup identity load failed ({type(error).__name__}); connecting without it.",
                "Identity context unavailable",
            )
        else:
            if not self.is_closing():
                self.set_identity_context(context)
                self.write_activity(
                    f"Startup identity context loaded ({len(context)} characters).", "Identity context loaded",
                )

        # The relationship loads on its own too; without it she talks as she would to anyone.
        if self.is_closing():
            return
        self.set_relationship_context("")
        try:
            context = await self.call_database(self.storage.load_relationship_context)
        except Exception as error:
            self.write_activity(
                f"Startup relationship load failed ({type(error).__name__}); connecting without it.",
                "Relationship context unavailable",
            )
        else:
            if not self.is_closing():
                self.set_relationship_context(context)
                self.write_activity(
                    f"Startup relationship context loaded ({len(context)} characters).",
                    "Relationship context loaded",
                )

        # The people she talks with as clients, and her view of each; without it she just doesn't know them.
        if self.is_closing():
            return
        self.set_people_context("")
        try:
            context = await self.call_database(self.storage.load_people_context)
        except Exception as error:
            self.write_activity(
                f"People context load failed ({type(error).__name__}); connecting without it.",
                "People context unavailable",
            )
        else:
            if not self.is_closing():
                self.set_people_context(context)
                if context:
                    self.write_activity(
                        f"People context loaded ({len(context)} characters).", "People context loaded",
                    )

        # Her tasks with other people, so she knows what's waiting and what came back.
        if self.is_closing():
            return
        self.set_tasks_context("")
        try:
            context = await self.call_database(self.storage.load_tasks_context)
        except Exception as error:
            self.write_activity(
                f"Tasks load failed ({type(error).__name__}); connecting without them.", "Tasks unavailable",
            )
        else:
            if not self.is_closing():
                self.set_tasks_context(context)

        # Her sign-ups, so she knows which accounts she has and which are stuck.
        if self.is_closing():
            return
        self.set_sign_ups_context("")
        try:
            context = await self.call_database(self.storage.load_sign_ups_context)
        except Exception as error:
            self.write_activity(
                f"Sign-ups load failed ({type(error).__name__}); connecting without them.", "Sign-ups unavailable",
            )
        else:
            if not self.is_closing():
                self.set_sign_ups_context(context)

        # The chosen delegation model loads last; if it fails Cora connects with the default.
        if self.is_closing():
            return
        self.set_delegation_model(BACKEND_MODEL)
        try:
            model = await self.call_database(self.storage.load_delegation_model)
        except Exception as error:
            self.write_activity(
                f"Delegation model load failed ({type(error).__name__}); connecting with the default.",
                "Delegation model unavailable",
            )
        else:
            if not self.is_closing():
                self.set_delegation_model(model)
                self.write_activity(f"Delegation model: {model}.", "Delegation model loaded")

    def summarize_previous_conversations(self, should_stop, dispatch_activity) -> None:
        try:
            run_background_memory_pass(
                self.storage.conversation_id,
                should_stop=should_stop,
                write_activity=dispatch_activity,
                summaries_done=self.summaries_done.set,
            )
        finally:
            self.summaries_done.set()

    async def load_last_summary(self) -> str | None:
        """Wait (capped) for this launch's summaries, then load the last conversation's summary."""
        deadline = monotonic() + SUMMARY_WAIT_SECONDS
        while not self.summaries_done.is_set():
            if self.is_closing() or monotonic() >= deadline:
                self.write_activity(
                    "Summaries not ready; greeting without the last thread.", "Last thread skipped",
                )
                return None
            await asyncio.sleep(0.25)
        try:
            return await self.call_database(self.storage.load_last_summary)
        except Exception as error:
            self.write_activity(
                f"Last summary load failed ({type(error).__name__}); greeting without it.",
                "Last thread unavailable",
            )
            return None

    async def load_relationship_values(self):
        """Current relationship values for the greeting; None if unknown or not loaded."""
        try:
            return await self.call_database(self.storage.load_relationship_values)
        except Exception as error:
            self.write_activity(
                f"Relationship load failed ({type(error).__name__}); greeting without it.",
                "Greeting relationship unavailable",
            )
            return None

    async def load_recent_plan(self) -> str | None:
        """A plan the person mentioned this week, for the greeting; None if none or not loaded."""
        try:
            return await self.call_database(self.storage.load_recent_plan)
        except Exception as error:
            self.write_activity(
                f"Recent plan load failed ({type(error).__name__}); greeting without it.",
                "Greeting memory unavailable",
            )
            return None

    async def load_dashboard_snapshot(self):
        """Fresh dashboard data; None if the database could not be read."""
        try:
            return await self.call_database(self.storage.load_dashboard_snapshot)
        except Exception as error:
            self.write_activity(
                f"Dashboard load failed ({type(error).__name__}); showing the last data.",
                "Dashboard unavailable",
            )
            return None

    async def wait_for_saves(self) -> None:
        for worker in self.workers:
            await worker.wait()

    async def end_conversation(self) -> bool:
        """Close this conversation at standby so it can be processed; return whether it closed."""
        try:
            await self.call_database(self.storage.start_new_conversation)
        except Exception as error:
            self.write_activity(
                f"Conversation close failed ({type(error).__name__}); it continues after standby.",
                "Conversation close failed",
            )
            return False
        return True

    async def finish(self, flush_transcript) -> None:
        try:
            flush_transcript()
            self.recording_closed = True
            for worker in self.workers:
                try:
                    await worker.wait()
                except WorkerError:
                    self.write_activity("Chat save worker stopped", "Chat save worker stopped")
            try:
                await self.call_database(self.storage.finish_conversation)
            except Exception as error:
                self.write_activity(
                    f"Conversation close failed ({type(error).__name__})",
                    "Conversation close failed",
                )
                self.notify("Conversation end could not be saved to the database.", severity="warning")
        finally:
            await self.close()

    async def close(self) -> None:
        try:
            await self.call_database(self.storage.close)
        except Exception as error:
            self.write_activity(f"Database cleanup failed ({type(error).__name__})", "Database cleanup failed")
