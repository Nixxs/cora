"""Typed chat: the composer, written replies and saving messages: part of CoraApp (see app.py)."""

import asyncio
import sys
from uuid import UUID

import pyperclip
from textual.actions import SkipAction
from textual.widgets import TextArea
from textual import events, work

from ui.composer import ChatComposer
from live import typed_content
from backend_chat import STANDBY_EXCLUDED, BackendRoute
from session_config import instruction_sections
from subconscious.emotions.feeling_rules import feelings_context
from app_areas.shared import CONVERSATION_END_STANDBY_SECONDS


class ChatMixin:
    """Typed chat: the composer, written replies and saving messages."""

    def on_key(self, event: events.Key) -> None:
        self.record_idle_activity(user_activity=True)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, ChatComposer):
            self.record_idle_activity(user_activity=True)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        if isinstance(event.text_area, ChatComposer):
            self.record_idle_activity(user_activity=True)

    # Textual owns worker scheduling and cancellation; coordinators own the work.
    def on_chat_composer_submitted(self, event: ChatComposer.Submitted) -> None:
        self.submission.submitted(event)

    @work(group="keyboard-submit")
    async def submit_keyboard_message(
        self, message: str, message_id: UUID, composer: ChatComposer, image_png: bytes | None = None
    ) -> None:
        await self.submission.submit_keyboard_message(message, message_id, composer, image_png)

    def record_chat_message(self, role: str, text: str, channel: str, device: str | None = None) -> None:
        if device is None:
            self.conversation_persistence.record_chat_message(role, text, channel)
        else:
            self.conversation_persistence.record_chat_message(role, text, channel, device)

    @work(group="chat-save")
    async def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str,
                                device: str | None = None) -> None:
        if device is None:
            await self.conversation_persistence.save_chat_message(message_id, role, text, channel)
        else:
            await self.conversation_persistence.save_chat_message(message_id, role, text, channel, device)

    def send_without_live(self, message: str, image_seen: str | None = None) -> None:
        """In Standby, answer a typed message in writing without waking Live."""
        # Taken now, before the message itself joins the chat, so it isn't sent twice.
        history = self.conversation.get_reconnect_history()
        self.keep_conversation_open()
        self.answer_without_live(typed_content(message, image_seen), history)

    def keep_conversation_open(self) -> None:
        """Talking in writing keeps the conversation going: in Standby it ends only after a long quiet."""
        if self.conversation_end_timer is not None:
            self.stop_conversation_end_timer()
            self.conversation_end_timer = self.set_timer(
                CONVERSATION_END_STANDBY_SECONDS, self.end_standby_conversation,
            )

    async def answer_person_in_writing(
        self, said: list[str], show_message, write, *, label: str, channel: str, where: str,
    ) -> str | None:
        """The person's own messages from away from the desk (Signal, the web page), answered as one Cora.

        Shown in the desk's chat with show_message, answered by write(history) (which also delivers
        the reply, and returns None if it failed), then shown and saved with label and channel, and
        noted to voice Cora as "the person just {where}". Returns her reply, or None.
        """
        # Taken before these messages join the chat, so they aren't sent twice.
        history = self.conversation.get_reconnect_history()
        for text in said:
            show_message(text)
        self.keep_conversation_open()
        text = await write(history)
        if text is None or self.closing:
            return None
        self.conversation.show_written_reply(text, label=label, channel=channel)
        self.person_is_talking()
        joined = "\n".join(said)
        if self.pending_greeting == "launch":
            # Answered before she says hello, so the hello can follow on from it.
            self.signal_launch_exchanges.append((joined, text))
        try:
            await self.live.note_signal_exchange(joined, text, where)
        except Exception as error:
            self.activity.write(f"Note to voice Cora failed ({type(error).__name__}).", f"{label} note failed")
        return text

    async def reply_in_writing(
        self, content: list[dict], history: list[dict], route: BackendRoute, extra_instructions: str = "",
        on_text=None, feelings: bool = True,
    ) -> str:
        """One turn of a chat without Live, as the same Cora: raises if it fails.

        on_text, if given, gets her text so far as it streams in. feelings: whether how she feels
        now is in her instructions (not in email).
        """
        # Reloaded each time, as at a Live connect, so she is the same Cora with what she
        # knows now: name, memories, interests, identity, relationship and chosen model.
        return await self.backend_chat.reply(
            content=content, history=history, sections=await self.person_sections(feelings),
            model=self.live.delegation_model, route=route, extra_instructions=extra_instructions,
            on_text=on_text,
        )

    async def person_sections(self, feelings: bool = True) -> dict[str, str]:
        """The person's own context, reloaded so she is the same Cora with what she knows now,
        including how she feels (eased since last time) unless `feelings` is false."""
        await self.conversation_persistence.load_session_context()
        return instruction_sections(
            self.live.memory_context, interest_context=self.live.interest_context,
            person_name=self.live.person_name, identity_context=self.live.identity_context,
            self_image_context=self.live.self_image_context,
            relationship_context=self.live.relationship_context, people_context=self.live.people_context,
            tasks_context=self.live.tasks_context, calendar_context=self.live.calendar_context,
            sign_ups_context=self.live.sign_ups_context, desire_context=self.live.desire_context,
            own_name=self.live.own_name, feelings_context=feelings_context(self.live.feelings) if feelings else "",
        )

    @work(group="text-chat")
    async def answer_without_live(self, content: list[dict], history: list[dict]) -> None:
        self.activity.write("Answering in writing without waking Live.", "Writing a reply")
        # Shown as it streams in, then replaced by the finished reply.
        draft = self.conversation.start_written_reply()
        try:
            text = await self.reply_in_writing(
                content, history, BackendRoute(excluded=STANDBY_EXCLUDED), on_text=draft.update,
            )
        except Exception as error:
            draft.fail()
            # The type only: a message could echo the conversation.
            self.activity.write(f"Written reply failed ({type(error).__name__}).", "Written reply failed")
            self.notify("Cora couldn't reply in writing. Check Activity.", severity="error")
            return
        except asyncio.CancelledError:
            # Stopped, say by closing: anything already shown stays, marked incomplete.
            draft.fail()
            raise
        if self.closing:
            draft.fail()
            return
        if text:
            draft.finish(text)
            self.activity.write(f"Written reply shown: characters={len(text)}", "Written reply")
        else:
            draft.fail()
            self.activity.write("The written reply was empty.", "Written reply empty")

    def action_copy_chat_selection(self) -> None:
        text = self.screen.get_selected_text()
        if not text:
            # Let the focused input handle its own normal copy shortcut.
            raise SkipAction()
        try:
            if sys.platform == "win32":
                # VS Code's terminal may not support Textual's OSC 52 clipboard.
                pyperclip.copy(text)
            else:
                self.copy_to_clipboard(text)
        except pyperclip.PyperclipException as error:
            self.activity.write(f"Clipboard error: {error}", "Clipboard error")
        else:
            self.notify("Selected text copied", timeout=2)
