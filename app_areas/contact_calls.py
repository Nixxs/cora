"""Calls from the person's contacts (P2b): part of CoraApp (see app.py).

A contact whose Calls switch is on (Settings › Signal) reaches her in client mode, as in their
Signal chat: her own voice session for the call, apart from the person's, with their memories,
their conversation and the same tools as their chat (memory search limited to them, noting her
feelings, passing things on, raising a concern). The person's own session, context and desk are
never involved, and their call's audio never plays at the desk.

Calls cost a voice session, so they're kept in bounds: one contact call at a time, each at most
CALL_MINUTES (she's told to wrap up just before), and CALLS_PER_DAY per contact; past that,
their call is rejected unanswered. What's said is saved in their conversation, like their chat.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from uuid import uuid4

from live import CoraLive
from phone.routes import CallLink
from session_config import client_chat_instructions, contact_call_config
from storage.client_store import Client
from tools.pass_on import CLIENT_ONLY
from web.voice import Listener, VoiceRelay

CALL_MINUTES = 10
CALLS_PER_DAY = 3
# How long before the end she's told to wrap up.
WRAP_UP_SECONDS = 45
# The tools a contact's call has: as in their Signal chat, but she can't end a chat by voice.
CALL_TOOLS = frozenset({"search_memory", "feel", *CLIENT_ONLY}) - {"stop_replying"}

ANSWER_NOTE = (
    "Phone call note, not something they said. {name} has just rung you and the call is connected, "
    "so answer it the way you'd pick up a call from them, in a few words of your own. Do not mention "
    "this note."
)
WRAP_UP_NOTE = (
    "Call time note, not something they said. Calls are kept short, and this one ends in under a "
    "minute: wrap up naturally now and say goodbye. Do not mention this note."
)


class Transcript:
    """What's said on a call, as whole turns: a turn ends when the other one starts talking."""

    def __init__(self) -> None:
        self.role: str | None = None
        self.text = ""

    def add(self, role: str, delta: str) -> tuple[str, str] | None:
        """Adds to the current turn; returns the finished one when the speaker changes."""
        finished = None
        if role != self.role:
            finished = self.finish()
            self.role = role
        self.text += delta
        return finished

    def finish(self) -> tuple[str, str] | None:
        role, text = self.role, " ".join(self.text.split())
        self.role, self.text = None, ""
        return (role, text) if role and text else None


@dataclass(eq=False)
class CallRoute:
    """Looks like a Live connection to her tools: their results go back to the call's own session."""

    live: CoraLive
    excluded: frozenset[str]
    memory_scope: tuple
    ask_approval = None
    approval_seconds = 0
    email_party = None
    tool_seconds = None

    @property
    def response(self):
        return self.live.connection.response


@dataclass(eq=False)
class ContactCall:
    client: Client
    relay: VoiceRelay = field(default_factory=VoiceRelay)
    live: CoraLive = field(default_factory=CoraLive)
    listener: Listener | None = None
    started: float = field(default_factory=monotonic)
    conversation_id: object = None
    transcript: Transcript = field(default_factory=Transcript)


class ContactCallsMixin:
    """Calls from the person's contacts, in client mode, in their own voice session."""

    def contact_call_allowed(self, client: Client) -> bool:
        """Under today's allowance for them (used up as each call is connected)."""
        return self.contact_calls_today.get((client.id, datetime.now().date()), 0) < CALLS_PER_DAY

    def count_contact_call(self, client: Client) -> None:
        key = (client.id, datetime.now().date())
        self.contact_calls_today = {k: n for k, n in self.contact_calls_today.items() if k[1] == key[1]}
        self.contact_calls_today[key] = self.contact_calls_today.get(key, 0) + 1

    def contact_call_link(self, client: Client) -> CallLink | None:
        """A contact's call, connecting: its own relay and session (None if another is on)."""
        if self.contact_call is not None:
            return None
        call = ContactCall(client)
        self.contact_call = call
        return CallLink(call.relay, lambda listener, connected: self.on_contact_stream(call, listener, connected),
                        on_gaps=lambda summary: summary and self.activity.write(f"{client.name}'s call: {summary}",
                                                                                "Phone call"))

    def on_contact_stream(self, call: ContactCall, listener: Listener, connected: bool) -> None:
        if connected:
            call.listener = listener
            call.relay.start_talking(listener)
            self.run_worker(self.run_contact_call(call), group="contact-call", exit_on_error=False)
            return
        # They hung up: her session for the call closes.
        call.listener = None
        self.run_worker(call.live.close(), group="contact-call-close", exit_on_error=False)

    async def run_contact_call(self, call: ContactCall) -> None:
        client = call.client
        try:
            try:
                call.conversation_id = await self.conversation_persistence.call_database(
                    self.persistence.client_conversation, client.id)
                history = await self.conversation_persistence.call_database(
                    self.persistence.load_client_history, call.conversation_id)
                sections = await self.client_sections(client)
            except Exception as error:
                self.activity.write(f"{client.name}'s call couldn't be connected to Cora ({type(error).__name__}).",
                                    "Phone call")
                return
            if call.listener is None:
                return  # they hung up while it was getting ready
            route = CallRoute(call.live, frozenset(self.tools.names()) - CALL_TOOLS, (client.id, client.name))
            rules = client_chat_instructions(client.name, self.live.person_name, self.live.own_name, on_call=True)
            live = call.live
            live.tool_schemas = lambda: self.tools.schemas(excluding=route.excluded)
            live.config_override = lambda schemas: (
                contact_call_config(schemas, sections, history, self.live.delegation_model, rules), sections)
            # Their call's audio only: never the desk's mic or speaker.
            live.voice_relay = call.relay
            live.web_microphone = call.listener.mic
            live.speaker_muted = True
            self.activity.write(f"{client.name} is on a call with Cora (client mode, their own conversation).",
                                "Phone call")
            cap = asyncio.ensure_future(self.contact_call_cap(call))
            try:
                await live.run(lambda event: self.on_contact_call_event(call, route, event),
                               lambda *status: None)
            except Exception as error:
                self.activity.write(f"{client.name}'s call to Cora failed ({type(error).__name__}).", "Phone call")
            finally:
                cap.cancel()
        finally:
            await self.save_call_turn(call, call.transcript.finish())
            if call.listener is not None:
                # Ended at her side (time up, or the session failed): closing the stream ends the call.
                call.relay.tell(call.listener, {"type": "talk_ended", "reason": "ended"})
            if self.contact_call is call:
                self.contact_call = None
            minutes = (monotonic() - call.started) / 60
            self.activity.write(f"{client.name}'s call ended after {minutes:.0f} min.", "Phone call")

    async def contact_call_cap(self, call: ContactCall) -> None:
        """Calls are kept to CALL_MINUTES: she's told to wrap up, then the call ends."""
        await asyncio.sleep(CALL_MINUTES * 60 - WRAP_UP_SECONDS)
        connection = call.live.connection
        if connection is not None and call.live.ready:
            try:
                await call.live.append_commentary(connection, f"wrap_up_{uuid4().hex}", WRAP_UP_NOTE)
            except Exception as error:
                self.activity.write(f"Telling Cora to wrap up the call failed ({type(error).__name__}).", "Phone call")
        await asyncio.sleep(WRAP_UP_SECONDS)
        self.activity.write(f"{call.client.name}'s call reached its {CALL_MINUTES}-minute limit.", "Phone call")
        await call.live.close()

    def on_contact_call_event(self, call: ContactCall, route: CallRoute, event) -> None:
        if event.type == "session.started":
            self.run_worker(self.answer_contact_call(call), group="contact-call-notes", exit_on_error=False)
        elif event.type in ("session.input_transcript.delta", "session.output_transcript.delta"):
            role = "user" if event.type == "session.input_transcript.delta" else "assistant"
            finished = call.transcript.add(role, event.delta)
            if finished is not None:
                self.run_worker(self.save_call_turn(call, finished), group="contact-call-save", exit_on_error=False)
        elif event.type == "response.event":
            item = (event.event or {}).get("item") or {}
            if event.event.get("type") == "response.output_item.done" and item.get("type") == "function_call":
                self.tools.handle_function_call(item, route=route)
        elif event.type == "error":
            self.activity.write(f"{call.client.name}'s call: {event.error.message}", "Phone call")

    async def answer_contact_call(self, call: ContactCall) -> None:
        connection = call.live.connection
        if connection is None:
            return
        await call.live.append_commentary(connection, f"greeting_{uuid4().hex}",
                                          ANSWER_NOTE.format(name=call.client.name))

    async def save_call_turn(self, call: ContactCall, turn: tuple[str, str] | None) -> None:
        """One turn of the call, in their conversation (as their chat's messages are)."""
        if turn is None or call.conversation_id is None:
            return
        role, text = turn
        try:
            await self.conversation_persistence.call_database(
                self.persistence.save_client_message, call.conversation_id, role, text, "voice")
        except Exception as error:
            self.activity.write(f"Saving {call.client.name}'s call failed ({type(error).__name__}).", "Phone call")
