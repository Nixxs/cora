"""Calls she places for the person's tasks (P3): part of CoraApp (see app.py).

call_someone (approved first) records the person called as a task party and asks Twilio to ring
them from her number. When they answer, Twilio streams the call to her listener, which connects it
to a voice session of its own, as with a contact's call: her own self and the task's brief, never
the person's context. She says she's an AI assistant calling for someone she works for and that she
keeps a transcript, keeps to the brief, and reports what she learns with pass_on, as in a task's
email thread, so the task's coordinator brings the person their word. The transcript is kept as a
step on the task. A call nobody answers, or one that ends without her reporting, goes to the
coordinator too, so the person always hears how it went.

Calls go out only when calling out is set up (Settings › Phone calls: the Twilio account and her
number), between CALL_HOURS local time, one at a time, each at most CALL_MINUTES (Twilio ends it
then too).
"""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from time import monotonic
from uuid import UUID, uuid4

from textual import work

from live import CoraLive
from phone import twilio
from phone.routes import CallLink
from session_config import TASK_CALL_LIVE_INSTRUCTIONS, contact_call_config, instruction_sections, phone_party_instructions
from storage.task_store import STEP_MAX_CHARS, PhoneParty
from web.voice import Listener, VoiceRelay

from phone.goodbye import GoodbyeWatch

from .contact_calls import CALL_MINUTES, CallRoute, Transcript, note_for_goodbye

# Local hours she places calls in: from 8am to before 8pm.
CALL_HOURS = range(8, 20)
# How long a placed call's token waits for it to be answered and its stream to open.
TOKEN_SECONDS = 120
# The tools on a call she placed: as in a task's email thread.
TASK_CALL_TOOLS = frozenset({"pass_on", "end_call"})

ANSWERED_NOTE = (
    "Call note, not something they said. {who} has just answered your call: introduce yourself as your "
    "instructions say and say why you're calling. Do not mention this note."
)
# Twilio's word for a call that didn't connect, as she'd say it.
NOT_CONNECTED = {
    "no-answer": "nobody answered", "busy": "the line was busy", "failed": "the call didn't go through",
    "canceled": "the call was cancelled",
}


@dataclass(eq=False)
class TaskCall:
    number: str
    name: str | None
    task_id: UUID
    goal: str
    brief: str
    party_id: UUID | None = None
    call_sid: str | None = None
    relay: VoiceRelay = field(default_factory=VoiceRelay)
    live: CoraLive = field(default_factory=CoraLive)
    listener: Listener | None = None
    # Whether it was answered (its stream opened), whether she reported on it, and whether it's over.
    connected: bool = False
    reported: bool = False
    done: bool = False
    started: float = field(default_factory=monotonic)
    transcript: Transcript = field(default_factory=Transcript)
    turns: list = field(default_factory=list)
    goodbye: GoodbyeWatch = field(default_factory=GoodbyeWatch)

    @property
    def who(self) -> str:
        return self.name or self.number


def transcript_step(call: TaskCall, minutes: float) -> str:
    """The call's transcript as a task step, cut to fit."""
    head = f"Call with {call.who} ({minutes:.0f} min), transcript:\n"
    lines = "\n".join(f"{'Them' if role == 'user' else 'Cora'}: {text}" for role, text in call.turns)
    text = head + (lines or "(nothing was said)")
    return text if len(text) <= STEP_MAX_CHARS else text[:STEP_MAX_CHARS - 1] + "…"


class TaskCallsMixin:
    """Calls she places for the person's tasks."""

    def calling_out_problem(self) -> str:
        """Why she can't place calls at all ("" when she can): what's still to set up."""
        settings = self.webhook_settings
        if not settings.phone_enabled or not self.webhook_listening():
            return "Phone calls aren't on in Settings, so you can't place calls."
        if not settings.twilio_account_sid or not settings.phone_number or not self.twilio_token:
            return "Calling out isn't set up yet (Settings › Phone calls: the Twilio account and your number)."
        return ""

    def can_call_out(self, number: str) -> str:
        """Why this call can't be placed now ("" when it can)."""
        if problem := self.calling_out_problem():
            return problem
        if twilio.same_number(number, self.webhook_settings.phone_number):
            return "That's your own number."
        if twilio.same_number(number, self.signal_settings.allowed_number):
            return "That's the person's own number; message them instead."
        now = datetime.now()
        if now.hour not in CALL_HOURS:
            return (f"It's {now:%H:%M} here: calls are only placed between {CALL_HOURS.start}am and "
                    f"{CALL_HOURS.stop - 12}pm. Offer to call then, or to email instead.")
        if self.task_call is not None:
            return "You're already on a call for a task; try again once it's done."
        return ""

    async def place_task_call(self, number: str, name: str | None, goal: str | None, task_id, brief: str,
                              deadline=None) -> UUID:
        """After the person approved it: the task (or them added to one), then the call. ValueError(why)
        if Twilio wouldn't place it."""
        who = name or number
        task_id = await self.save_task_party(PhoneParty(number, name), who, goal, task_id, brief,
                                             f"Calling {who}.", deadline)
        goal = await self.task_goal(task_id) or goal or ""
        party_id = await self.conversation_persistence.call_database(self.persistence.task_party_id, task_id, number)
        call = TaskCall(number, name, task_id, goal, brief, party_id)
        self.task_call = call
        settings = self.webhook_settings
        token = self.give_phone_token(call, TOKEN_SECONDS)
        try:
            call.call_sid = await twilio.place_call(
                settings.twilio_account_sid, self.twilio_token, settings.phone_number, number,
                twilio.connect_stream(twilio.stream_url(settings.public_url), token),
                settings.public_url + twilio.STATUS_PATH, CALL_MINUTES * 60,
            )
        except Exception as error:
            self.task_call = None
            self.phone_tokens.pop(token, None)
            detail = str(error) if isinstance(error, twilio.TwilioError) else type(error).__name__
            self.activity.write(f"Twilio couldn't place the call to {who} ({detail}).", "Phone call")
            await self.note_task_step(task_id, f"Calling {who} failed: Twilio couldn't place the call ({detail}).")
            raise ValueError(f"Twilio couldn't place the call ({detail}); nothing was said to them.") from None
        self.activity.write(f"Calling {who} for a task.", "Phone call")
        return task_id

    def task_call_link(self, call: TaskCall) -> CallLink | None:
        """Her call has been answered: its own relay and session."""
        if call.done or self.task_call is not call:
            return None
        call.connected = True
        return CallLink(call.relay, lambda listener, connected: self.on_task_call_stream(call, listener, connected),
                        on_gaps=lambda summary: summary and self.activity.write(f"The call with {call.who}: {summary}",
                                                                                "Phone call"))

    def on_task_call_stream(self, call: TaskCall, listener: Listener, connected: bool) -> None:
        if connected:
            call.listener = listener
            call.relay.start_talking(listener)
            self.run_worker(self.run_task_call(call), group="task-call", exit_on_error=False)
            return
        call.listener = None
        self.run_worker(call.live.close(), group="task-call-close", exit_on_error=False)

    async def run_task_call(self, call: TaskCall) -> None:
        try:
            await self.conversation_persistence.load_session_context()
            sections = instruction_sections(
                identity_context=self.live.identity_context, self_image_context=self.live.self_image_context,
                task_briefs=json.dumps([{"task_id": str(call.task_id), "brief": call.brief}], ensure_ascii=False),
            )
            route = CallRoute(call.live, frozenset(self.tools.names()) - TASK_CALL_TOOLS,
                              email_party=(call.party_id, call.task_id, call.who),
                              end_call=lambda: self.hang_up(call.live, call.relay, call.who))
            rules = phone_party_instructions(call.who, call.number, self.live.person_name, self.live.own_name)
            live = call.live
            live.tool_schemas = lambda: self.tools.schemas(excluding=route.excluded)
            model = await self.contacts_model()
            live.config_override = lambda schemas: (contact_call_config(
                schemas, sections, [], model, rules,
                live_instructions=TASK_CALL_LIVE_INSTRUCTIONS, voice=self.live.voice), sections)
            # The call's audio only: never the desk's mic or speaker.
            live.voice_relay = call.relay
            live.web_microphone = call.listener.mic if call.listener is not None else None
            live.speaker_muted = True
            if call.listener is None:
                return  # they hung up while it was getting ready
            self.activity.write(f"{call.who} answered; Cora is on the call.", "Phone call")
            cap = asyncio.ensure_future(self.call_cap(live, call.who))
            goodbye = asyncio.ensure_future(self.watch_goodbye(live, call.relay, call.goodbye, call.who))
            try:
                await live.run(lambda event: self.on_task_call_event(call, route, event), lambda *status: None)
            except Exception as error:
                self.activity.write(f"The call with {call.who} failed ({type(error).__name__}).", "Phone call")
            finally:
                cap.cancel()
                goodbye.cancel()
        finally:
            await self.finish_task_call(call)

    def on_task_call_event(self, call: TaskCall, route: CallRoute, event) -> None:
        note_for_goodbye(call.goodbye, event)
        if event.type == "session.started":
            self.run_worker(self.answered_note(call), group="task-call-notes", exit_on_error=False)
        elif event.type in ("session.input_transcript.delta", "session.output_transcript.delta"):
            role = "user" if event.type == "session.input_transcript.delta" else "assistant"
            if (finished := call.transcript.add(role, event.delta)) is not None:
                call.turns.append(finished)
        elif event.type == "response.event":
            item = (event.event or {}).get("item") or {}
            if event.event.get("type") == "response.output_item.done" and item.get("type") == "function_call":
                if item.get("name") == "pass_on":
                    call.reported = True
                self.tools.handle_function_call(item, route=route)
        elif event.type == "error":
            self.activity.write(f"The call with {call.who}: {event.error.message}", "Phone call")

    async def answered_note(self, call: TaskCall) -> None:
        connection = call.live.connection
        if connection is not None:
            await call.live.append_commentary(connection, f"answered_{uuid4().hex}", ANSWERED_NOTE.format(who=call.who))

    async def finish_task_call(self, call: TaskCall) -> None:
        """The call is over: its transcript on the task, and the coordinator told if she didn't report."""
        if call.done:
            return
        call.done = True
        if self.task_call is call:
            self.task_call = None
        if call.listener is not None:
            call.relay.tell(call.listener, {"type": "talk_ended", "reason": "ended"})
        if (finished := call.transcript.finish()) is not None:
            call.turns.append(finished)
        minutes = (monotonic() - call.started) / 60
        await self.note_task_step(call.task_id, transcript_step(call, minutes))
        self.activity.write(f"The call with {call.who} ended after {minutes:.0f} min.", "Phone call")
        if not call.reported:
            said = "\n".join(f"{'Them' if role == 'user' else 'Cora'}: {text}" for role, text in call.turns[-8:])
            self.tasks_coordinating.add(call.task_id)
            self.coordinate_task(
                call.task_id, f"Your call with {call.who} ended without you passing anything on; its transcript "
                "is the task's latest step.",
                f"I called {call.who} for your task \"{call.goal}\". How it went:\n{said or '(nothing was said)'}",
                settled=False,
            )

    def on_twilio_status(self, call_sid: str, status: str) -> None:
        """How a call she placed ended. One that was answered is finished when its stream closes; one
        that never connected is finished here, and the person hears why."""
        call = self.task_call
        if call is None or call.call_sid != call_sid or call.connected or status not in NOT_CONNECTED:
            return
        call.done = True
        self.task_call = None
        why = NOT_CONNECTED[status]
        self.activity.write(f"Calling {call.who}: {why}.", "Phone call")
        self.run_worker(self.note_task_step(call.task_id, f"Called {call.who}: {why}."),
                        group="task-call-save", exit_on_error=False)
        self.tasks_coordinating.add(call.task_id)
        self.coordinate_task(call.task_id, f"Your call to {call.who} didn't connect: {why}.",
                             f"I tried calling {call.who} for your task \"{call.goal}\", but {why}.", settled=False)

    async def note_task_step(self, task_id, text: str) -> None:
        try:
            await self.conversation_persistence.call_database(self.persistence.add_task_step, task_id, text)
        except Exception as error:
            self.activity.write(f"Saving a call on its task failed ({type(error).__name__}).", "Phone call")

    @work(group="webhook-setup", exit_on_error=False)
    async def save_twilio_region(self, code: str) -> None:
        """The Twilio region for calls she places (Settings › Phone calls); applies to the next call."""
        twilio.set_region(code)
        self.activity.write(f"Calls she places now go through Twilio's {twilio.REGIONS[code][0]} region.",
                            "Settings changed")
        try:
            await self.conversation_persistence.call_database(self.persistence.save_twilio_region, code)
        except Exception as error:
            self.activity.write(f"Twilio region save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save the Twilio region; it applies until Cora closes.", severity="error")

    @work(group="webhook-setup", exit_on_error=False)
    async def save_twilio_account(self, account_sid: str, phone_number: str) -> None:
        """The Twilio account she calls out through and her number (Settings › Phone calls); empty clears."""
        if bool(account_sid.strip()) != bool(phone_number.strip()):
            self.notify("Give both the Account SID and her Twilio number (or leave both empty).", severity="warning")
            return
        try:
            await self.conversation_persistence.call_database(self.persistence.save_twilio_account,
                                                              account_sid, phone_number)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        except Exception as error:
            self.activity.write(f"Saving the Twilio account failed ({type(error).__name__}).", "Phone calls")
            self.notify("That couldn't be saved. Check Activity.", severity="error")
            return
        before = self.calling_out_problem() == ""
        self.webhook_settings = replace(self.webhook_settings, twilio_account_sid=account_sid.strip() or None,
                                        phone_number="".join(phone_number.split()) or None)
        if (self.calling_out_problem() == "") != before:
            # call_someone is offered (or not) from her next connection.
            self.service_tools_changed = True
        self.notify("Saved." if account_sid.strip() else "Calling out is off.")
        self.activity.write("Saved the Twilio account for calling out." if account_sid.strip()
                            else "Cleared the Twilio account: Cora can't call out.", "Phone calls")
