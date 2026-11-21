"""Presence (Settings > Her home > Presence): part of CoraApp (see app.py).

Whether anyone's in her room, from the webcam and what she knows (home/presence.py). Every minute
while it's on, she checks how long the PC has gone untouched and since the person last spoke to her;
once that's IDLE_BEFORE_LOOK_SECONDS she takes one look, and again at most every LOOK_EVERY_SECONDS.
Each look goes to Activity.

An automatic look that finds the room empty (Look now never acts):
- while she's in voice with no one on the web page or a phone call, she goes to standby, as idle
  standby does;
- once it's been empty EMPTY_FOR_SECONDS (from when it went quiet), confirmed by a look then, her
  room's air conditioner goes off if it's on and nothing else will stop it (a timer, Climate React),
  and the person is told on Signal. Once per empty stretch; anyone using the PC, speaking to her or a
  look that isn't "empty" ends the stretch.

When someone comes back after an empty stretch while she's in standby: if something's waiting for
them (observations queued while they were out, the mail digest, a task that needs them) she wakes and
welcomes them back with it; otherwise she stays in standby and leaves a short note in the chat,
written by her backend model as the same Cora (a fraction of a cent), saved with the conversation.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from time import monotonic

from textual import work

from app_areas.home import CHANGE_MAX_AGE_SECONDS, left_on
from backend_chat import STANDBY_EXCLUDED, BackendRoute
from home.presence import (
    EMPTY_FOR_SECONDS, IDLE_BEFORE_LOOK_SECONDS, QUIET_SLACK_SECONDS, CameraError, Look, away_words, confirm_due,
    cost, due, evidence, grab_frame, idle_seconds, judge, minutes, verdict,
)
from home.sensibo import SensiboError, reading

CHECK_SECONDS = 60.0
WELCOME_NOTE = (
    "A note from the app, not a message from the person. You're in standby, resting rather than "
    "listening, and the person has just come back to the desk after being out of the room (the facts "
    "below). Leave them a short written note in the chat, a line or two in your own words, to welcome "
    "them back, carrying on from where things were if that's natural. If you turned their air "
    "conditioner off while they were out, say so and offer to turn it back on. Don't use tools, and "
    "don't mention this note, standby or the app.\n\nFacts: "
)


class PresenceMixin:
    """Whether anyone's in her room."""

    def init_presence(self) -> None:
        self.presence_enabled = False
        self.presence_timer = None
        # The latest look, and when (monotonic) it was taken.
        self.presence_last: Look | None = None
        self.presence_looked_at: float | None = None
        # The latest problem looking, or "" (shown in Settings until a look works).
        self.presence_problem = ""
        # When (monotonic) the person's voice was last heard, for the evidence and the idle gate.
        self.person_heard_at: float | None = None
        # The empty stretch: when (monotonic) it went quiet, from the first look that found it empty
        # (None when it isn't), and whether her room's aircon has been seen to in it.
        self.presence_empty_since: float | None = None
        self.presence_aircon_done = False
        # The room whose air conditioner she turned off in this empty stretch, or "".
        self.presence_aircon_off = ""
        # What to tell her as she wakes for someone coming back (send_greeting's welcome back), or "".
        self.return_note = ""

    def start_presence(self, on: bool) -> None:
        """The minute check runs exactly while presence is on."""
        self.presence_enabled = on
        if on and self.presence_timer is None and not self.closing:
            self.presence_timer = self.set_interval(CHECK_SECONDS, self.presence_check)
        elif not on and self.presence_timer is not None:
            self.presence_timer.stop()
            self.presence_timer = None
        if not on:
            self.end_empty_stretch()

    def quiet_seconds(self) -> float | None:
        """How long since anyone used the PC or spoke to her; None where the PC's input can't be read."""
        idle = idle_seconds()
        if idle is None or self.person_heard_at is None:
            return idle
        return min(idle, monotonic() - self.person_heard_at)

    def presence_check(self) -> None:
        if not self.presence_enabled or self.closing:
            return
        quiet = self.quiet_seconds()
        since = None if self.presence_looked_at is None else monotonic() - self.presence_looked_at
        empty_since = self.presence_empty_since
        if empty_since is not None and (quiet is None or quiet + QUIET_SLACK_SECONDS < monotonic() - empty_since):
            # Someone's used the PC or spoken to her since it went quiet: they're back.
            away = monotonic() - (quiet or 0.0) - empty_since
            aircon_off = self.presence_aircon_off
            self.end_empty_stretch()
            if self.standby and not self.closing:
                self.person_back(away, aircon_off)
        if due(quiet, since) or (self.presence_empty_since is not None and not self.presence_aircon_done
                                 and self.her_aircon_on() and confirm_due(quiet, since)):
            self.look_for_someone()

    def end_empty_stretch(self) -> None:
        self.presence_empty_since = None
        self.presence_aircon_done = False
        self.presence_aircon_off = ""

    async def waiting_for_person(self) -> list[str]:
        """What's waiting to be told, in a few words each: observations queued while she rested, the
        mail digest, and tasks that need the person."""
        waiting = []
        queued = sum(1 for item in self.attention.pending if isinstance(item, str))
        if queued:
            waiting.append(f"{queued} thing{'s' if queued != 1 else ''} you noticed while they were out")
        if self.mail_digest:
            count = len(self.mail_digest)
            waiting.append(f"{count} email{'s' if count != 1 else ''} worth telling them about")
        try:
            tasks = await self.conversation_persistence.call_database(self.persistence.load_open_tasks)
        except Exception:
            tasks = []
        for task in tasks if isinstance(tasks, list) else []:
            if getattr(task, "status", None) == "needs_person":
                waiting.append(f"a task that needs them: {task.goal}")
        return waiting

    @work(group="person-back", exclusive=True, exit_on_error=False)
    async def person_back(self, away: float, aircon_off: str) -> None:
        """Someone's back after an empty stretch while she's in standby: she wakes if something's
        waiting for them, and otherwise leaves a note in the chat."""
        waiting = await self.waiting_for_person()
        if self.closing or not self.standby:
            return
        back_at = datetime.now()
        facts = {"out_for": away_words(away), "left_around": f"{back_at - timedelta(seconds=away):%H:%M}",
                 "back_at": f"{back_at:%H:%M}", "air_conditioner_turned_off": aircon_off or None}
        if waiting:
            self.activity.write(f"You're back ({facts['out_for']} out); waking: {'; '.join(waiting)}.", "Presence")
            self.return_note = (f"They've just come back to the desk after being out {facts['out_for']}. Waiting for "
                                f"them: {'; '.join(waiting)}." + (f" While they were out you turned {aircon_off}'s air "
                                                                  "conditioner off; offer to turn it back on."
                                                                  if aircon_off else ""))
            self.action_reconnect()
            if self.reconnecting:
                self.pending_greeting = "welcome_back"
            else:
                self.return_note = ""
            return
        self.activity.write(f"You're back ({facts['out_for']} out); leaving a note.", "Presence")
        route = BackendRoute(excluded=STANDBY_EXCLUDED | self.unavailable_tools(), channel="desk")
        try:
            text = await self.reply_in_writing(
                [{"type": "input_text", "text": WELCOME_NOTE + json.dumps(facts, ensure_ascii=False)}],
                self.conversation.get_reconnect_history(), route)
        except Exception as error:
            self.activity.write(f"The welcome-back note failed ({type(error).__name__}).", "Presence")
            return
        if text and not self.closing and self.standby:
            self.conversation.show_written_reply(text.strip())

    def her_aircon_on(self) -> bool:
        """Whether her room's air conditioner was on when last read (Sensibo set up)."""
        room = getattr(self, "her_room_reading", None)
        return self.sensibo_ready() and room is not None and bool(room.on)

    def presence_room(self) -> tuple[str, str]:
        """Her room's name and its air conditioner as she last read it, or a plain name and ""."""
        room = getattr(self, "her_room_reading", None)
        if room is None:
            return "a home office", ""
        shown = reading(room, datetime.now(timezone.utc))
        aircon = shown.get("aircon", "")
        if "temperature" in shown:
            aircon = f"{aircon}; the room reads {shown['temperature']}" if aircon else f"the room reads {shown['temperature']}"
        return room.name, aircon

    @work(group="presence-look", exclusive=True, exit_on_error=False)
    async def look_for_someone(self, asked: bool = False) -> None:
        """One look: a frame, weighed with what she knows, into Activity. asked: from Settings' Look now."""
        self.presence_looked_at = monotonic()
        quiet = self.quiet_seconds()
        pc_idle = idle_seconds()
        heard_ago = None if self.person_heard_at is None else monotonic() - self.person_heard_at
        room, aircon = self.presence_room()
        notes = evidence(datetime.now(), room, pc_idle, heard_ago, not self.standby, aircon, self.presence_last)
        try:
            jpeg = await asyncio.to_thread(grab_frame)
        except CameraError as error:
            self.presence_failed(str(error))
            return
        except Exception as error:
            self.presence_failed(f"The camera couldn't be read ({type(error).__name__}).")
            return
        try:
            probability, tokens = await asyncio.to_thread(judge, jpeg, notes)
        except Exception as error:
            # Errors may echo the request; log only the type.
            self.presence_failed(f"The Decisions API couldn't weigh it ({type(error).__name__}).")
            return
        self.presence_last = Look(probability, datetime.now(), tokens)
        self.presence_problem = ""
        idle = "" if pc_idle is None else f" · PC idle {minutes(pc_idle)}"
        self.activity.write(f"{room}: {verdict(probability)} ({round(probability * 100)}% someone's there)"
                            f"{idle}{' · asked' if asked else ''} · {cost(tokens)}.", "Presence")
        if not asked:
            await self.act_on_look(room, probability, quiet)

    async def act_on_look(self, room: str, probability: float, quiet: float | None) -> None:
        """An empty room: standby if she's in voice for no one, and her room's aircon off after an hour."""
        if verdict(probability) != "empty":
            self.end_empty_stretch()
            return
        if self.presence_empty_since is None:
            self.presence_empty_since = monotonic() - (quiet or 0.0)
        web_voice = getattr(self, "web_voice", None)
        if self.can_reach_by_voice() and not (web_voice is not None and web_voice.active):
            blockers = self.live_transition_blockers()
            if blockers:
                self.activity.write(f"{room} looks empty, but she's busy ({', '.join(blockers)}); staying in voice.",
                                    "Presence")
            else:
                self.activity.write(f"{room} looks empty; going to standby.", "Presence")
                self.action_standby()
        if not self.presence_aircon_done and monotonic() - self.presence_empty_since >= EMPTY_FOR_SECONDS:
            self.presence_aircon_done = True
            await self.turn_off_empty_room()

    async def turn_off_empty_room(self) -> None:
        """Her room's air conditioner off, if it's on and nothing else will stop it; told on Signal."""
        if not self.sensibo_ready() or not self.her_room_id:
            return
        try:
            rooms, _ = await self.sensibo.rooms(max_age=CHANGE_MAX_AGE_SECONDS)
        except SensiboError as error:
            self.activity.write(f"Presence: her room's aircon couldn't be read ({error}).", "Presence")
            return
        self.remember_rooms(rooms)
        still_on = left_on(rooms, [self.her_room_id], datetime.now(timezone.utc))
        if not still_on:
            return
        result = await self.change_aircon("set", still_on, {"on": False, "mode": None, "temperature": None,
                                                            "fan": None, "swing": None})
        name = still_on[0].name
        if result.get("status") != "ok":
            self.activity.write(f"Presence: {name} has been empty an hour, but its aircon didn't turn off.", "Presence")
            return
        self.presence_aircon_off = name
        text = f"{name} has looked empty for about an hour, so I turned its air conditioner off."
        if self.signal_reaches_person():
            try:
                await self.tell_person_on_signal(text)
            except Exception as error:
                self.activity.write(f"Presence: the Signal message failed ({type(error).__name__}).", "Presence")
        else:
            self.activity.write(f"Presence: {text} (no way to tell you)", "Presence")

    def presence_failed(self, problem: str) -> None:
        if problem != self.presence_problem:
            self.activity.write(f"Presence: {problem}", "Presence")
        self.presence_problem = problem

    def presence_status(self) -> str:
        """Settings' line: the latest look, or what's wrong, or what it's waiting for."""
        if self.presence_problem:
            return self.presence_problem
        last = self.presence_last
        if last is not None:
            looked = f"looked {last.at:%H:%M}: {verdict(last.probability)} ({round(last.probability * 100)}% someone's there)"
            return looked if self.presence_enabled else f"off · {looked}"
        return "on · waits for the PC to sit idle 15 minutes" if self.presence_enabled else "off"

    def presence_view(self) -> tuple[str, str] | None:
        """The dashboard's line, (words, kind), or None while presence is off. kind is "here",
        "someone", "empty", "unsure", "problem" or "waiting". Someone using the PC or talking to her
        since the last look outranks it: they're plainly there, so the look is out of date."""
        if not self.presence_enabled:
            return None
        if self.presence_problem:
            return self.presence_problem, "problem"
        quiet = self.quiet_seconds()
        since_look = None if self.presence_looked_at is None else monotonic() - self.presence_looked_at
        if quiet is not None and quiet < IDLE_BEFORE_LOOK_SECONDS and (since_look is None or quiet < since_look):
            return f"someone's here · active {'just now' if quiet < 120 else minutes(quiet) + ' ago'}", "here"
        last = self.presence_last
        if last is None:
            return "watching · no look yet", "waiting"
        kind = {"someone's there": "someone", "empty": "empty"}.get(verdict(last.probability), "unsure")
        return f"{verdict(last.probability)} · looked {last.at:%H:%M} ({round(last.probability * 100)}%)", kind

    @work(group="presence-save", exclusive=True, exit_on_error=False)
    async def save_presence_enabled(self, on: bool) -> None:
        self.start_presence(on)
        if not on:
            self.presence_problem = ""
        self.activity.write(f"Presence {'on' if on else 'off'}.", "Settings changed")
        try:
            await self.conversation_persistence.call_database(self.persistence.save_presence_enabled, on)
        except Exception as error:
            self.activity.write(f"Presence setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save the Presence switch; it applies until Cora closes.", severity="error")
