"""Push-to-talk into the game's chat (Settings > Game): part of CoraApp (see app.py).

Holding the middle mouse button mutes Live's microphone and records; letting go transcribes it
(with OpenAI, or on this PC) and, if World of Warcraft is the window in front, types it into the game's chat. Cora is
told quietly what was said, so she knows it was for the game, not her.

Map pins (place_wow_map_pins) and the pin addon's other commands (wow_map_pins_command) go the same
way: the CoraMapPins addon's /cmp lines, typed while WoW is in front, or as soon as it comes to the front
within PIN_WAIT_SECONDS, in the order Cora asked for them."""

import asyncio
from dataclasses import dataclass
from time import monotonic

from textual import work

from game.chat_line import CHANNELS, DEFAULT_CHANNEL, casual, chat_line, chat_text, game_in_front, type_into_game
from game.map_pins import is_addon_line
from game.moments import CHATTINESS, DEFAULT_CHATTINESS
from game.mouse_button import MiddleButtonWatcher
from game.speech import (
    DEFAULT_TRANSCRIPTION, GAME_WORDS, MAX_VOCABULARY_CHARS, SAMPLE_RATE, TRANSCRIPTION_CHOICES, CloudTranscriber, Recorder,
    Transcriber, trimmed,
)

# How long map pins wait for WoW to come to the front, how often that's checked, and the pause
# between typed lines (each is its own Enter, line, Enter batch) so the game takes each one in turn.
PIN_WAIT_SECONDS = 120.0
PIN_CHECK_SECONDS = 0.5
PIN_LINE_GAP_SECONDS = 0.15
PINS_TYPED_MESSAGE = (
    "The pins were typed into WoW as CoraMapPins commands: numbered pins on their map, and a box on "
    "their screen with the first step. The game doesn't answer back, so you don't know they appeared; "
    "if they say they didn't, the addon may be off (they can type /cmp status in the game)."
)
PINS_WAITING_MESSAGE = (
    "WoW isn't the window in front, so the pins will be typed into it as soon as it is, within two "
    "minutes. Tell them to switch to the game."
)
COMMAND_TYPED_MESSAGE = (
    "The command was typed into WoW for CoraMapPins. The game doesn't answer back: the addon prints its "
    "reply in their chat, which you don't see, so don't say what it did beyond what the command does."
)
COMMAND_WAITING_MESSAGE = (
    "WoW isn't the window in front, so the command will be typed into it as soon as it is, within two "
    "minutes (after anything else still waiting). Tell them to switch to the game."
)


@dataclass(eq=False)
class AddonJob:
    """Lines for the CoraMapPins addon waiting for WoW: a whole route (place_wow_map_pins) or one
    command (wow_map_pins_command)."""
    route: str | None  # the route's name; None for a command
    lines: list[str]
    give_up: float = 0.0

    @property
    def what(self) -> str:
        return self.route if self.route is not None else self.lines[0]


class GameMixin:
    """Push-to-talk into the game's chat."""

    def init_game(self) -> None:
        # Settings > Game, as loaded at launch (off until then).
        self.game_push_to_talk = False
        self.game_channel = DEFAULT_CHANNEL
        self.game_watcher: MiddleButtonWatcher | None = None
        self.game_recorder = Recorder()
        self.game_transcription = DEFAULT_TRANSCRIPTION
        self.game_vocabulary = ""
        self.game_cloud_transcriber = CloudTranscriber()
        # On this PC: the choice, or the fallback when OpenAI can't be reached.
        self.game_transcriber = Transcriber()
        # Held down now, and whether holding it muted Live's microphone (so letting go unmutes it).
        self.game_talking = False
        self.game_muted_live = False
        # For each line's timing in Activity: when the button went down, how far into the press
        # OpenAI's connection was ready (None while opening, False if it failed), and what
        # transcribed it in how long.
        self.game_pressed_at = 0.0
        self.game_warmed: float | bool | None = None
        self.game_heard_by = ""
        self.game_lock = asyncio.Lock()
        # Held while typing into the game, so a push-to-talk line never lands inside a batch of map pins.
        self.game_typing_lock = asyncio.Lock()
        # Addon lines waiting for WoW to come to the front, oldest first: commands in the order asked,
        # and at most one route (a newer one replaces it).
        self.game_pending_pins: list[AddonJob] = []
        self.game_pins_waiter = None

    def apply_game_settings(self, saved: dict) -> None:
        """Settings > Game, once at launch."""
        channel = saved.get("channel")
        self.game_channel = channel if channel in CHANNELS else DEFAULT_CHANNEL
        transcription = saved.get("transcription")
        self.game_transcription = transcription if transcription in TRANSCRIPTION_CHOICES else DEFAULT_TRANSCRIPTION
        vocabulary = saved.get("vocabulary")
        self.game_vocabulary = vocabulary if isinstance(vocabulary, str) else ""
        if saved.get("push_to_talk") is True:
            self.game_push_to_talk = True
            self.start_push_to_talk()
        chattiness = saved.get("chattiness")
        self.set_game_chattiness(chattiness if chattiness in CHATTINESS else DEFAULT_CHATTINESS)
        if saved.get("watch") is True:
            self.game_watch = True
            self.start_game_watch()

    def start_push_to_talk(self) -> None:
        if self.closing or (self.game_watcher is not None and self.game_watcher.running):
            return
        loop = asyncio.get_running_loop()
        self.game_watcher = MiddleButtonWatcher(
            on_press=lambda: loop.call_soon_threadsafe(self.game_button_pressed),
            on_release=lambda: loop.call_soon_threadsafe(self.game_button_released),
        )
        try:
            self.game_watcher.start()
        except OSError as error:
            self.game_watcher = None
            self.activity.write(f"Push-to-talk unavailable: {error}", "Push-to-talk")
            self.notify(f"Push-to-talk unavailable: {error}", severity="error")
            return
        self.activity.write("Push-to-talk on: hold the middle mouse button to speak into WoW's chat.",
                            "Push-to-talk on")
        self.load_game_transcriber()

    @work(group="game-transcriber", thread=True)
    def load_game_transcriber(self) -> None:
        # Ready before the first press (or the first fallback), so the first line isn't slow.
        try:
            self.game_transcriber.load()
        except Exception as error:
            self.call_from_thread(self.activity.write,
                                  f"Push-to-talk's speech model didn't load ({type(error).__name__}).", "Push-to-talk")

    def stop_push_to_talk(self) -> None:
        watcher, self.game_watcher = self.game_watcher, None
        if watcher is not None:
            watcher.stop()
        if self.game_recorder.recording:
            self.game_recorder.stop()
        self.game_talking = False

    def game_button_pressed(self) -> None:
        if not self.closing and self.game_watcher is not None:
            self.start_game_talk()

    def game_button_released(self) -> None:
        if not self.closing:
            self.finish_game_talk()

    @work(group="game-talk")
    async def start_game_talk(self) -> None:
        async with self.game_lock:
            if self.game_talking:
                return
            self.game_talking = True
            # What's said here is for the game: Live mustn't hear it as said to her.
            if self.live.ready and not self.live.microphone_muted:
                try:
                    await self.live.set_microphone_muted(True)
                    self.game_muted_live = True
                except Exception as error:
                    self.activity.write(f"Push-to-talk couldn't mute Cora's microphone ({type(error).__name__}).",
                                        "Push-to-talk")
                self.update_audio_controls()
            try:
                await asyncio.to_thread(self.game_recorder.start)
            except Exception as error:
                self.game_talking = False
                await self.restore_live_microphone()
                self.activity.write(f"Push-to-talk couldn't record ({type(error).__name__}).", "Push-to-talk")
                return
            self.game_pressed_at = monotonic()
            self.game_warmed = None
            if self.game_transcription == "openai":
                self.warm_game_transcription(self.game_pressed_at)

    @work(group="game-warm", thread=True)
    def warm_game_transcription(self, pressed: float) -> None:
        # OpenAI's connection opened while the button is held, so letting go doesn't wait for one.
        # How long it took into the press (False if it failed) goes in the line's timing.
        warmed = self.game_cloud_transcriber.warm()
        if self.game_pressed_at == pressed:
            self.game_warmed = monotonic() - pressed if warmed else False

    async def restore_live_microphone(self) -> None:
        if not self.game_muted_live:
            return
        self.game_muted_live = False
        if self.live.microphone_muted and not self.closing:
            try:
                await self.live.set_microphone_muted(False)
            except Exception as error:
                self.activity.write(f"Push-to-talk couldn't unmute Cora's microphone ({type(error).__name__}).",
                                    "Push-to-talk")
        self.update_audio_controls()

    @work(group="game-talk")
    async def finish_game_talk(self) -> None:
        released = monotonic()
        async with self.game_lock:
            if not self.game_talking:
                return
            self.game_talking = False
            audio = await asyncio.to_thread(self.game_recorder.stop)
            await self.restore_live_microphone()
        # Only the speech itself is sent: less to upload and transcribe.
        audio = trimmed(audio)
        spoken = await self.transcribe_game_speech(audio)
        heard = monotonic()
        if spoken is None:
            return
        # Typed the way players type: names keep their capitals.
        text = chat_text(casual(spoken, (*GAME_WORDS, self.game_vocabulary)))
        if not text:
            self.activity.write("Push-to-talk heard nothing to send.", "Push-to-talk")
            return
        command, how = CHANNELS[self.game_channel]
        if not await asyncio.to_thread(game_in_front):
            self.conversation.show_note(f"Not sent (WoW wasn't the window in front): {text}")
            self.activity.write("Push-to-talk: WoW wasn't the window in front, so nothing was typed.",
                                "Push-to-talk")
            return
        try:
            async with self.game_typing_lock:
                await asyncio.to_thread(type_into_game, chat_line(text, self.game_channel))
        except OSError as error:
            self.conversation.show_note(f"Not sent to WoW: {text}")
            self.activity.write(f"Push-to-talk couldn't type into WoW: {error}", "Push-to-talk")
            return
        typed = monotonic()
        self.activity.write(f"Push-to-talk: {audio.size / SAMPLE_RATE:.1f}s of speech heard in {heard - released:.2f}s "
                            f"({self.game_heard_by}), typed {typed - heard:.2f}s later.", "Push-to-talk")
        self.record_idle_activity()
        self.conversation.show_note(f"You in WoW ({command}): {text}")
        try:
            await self.live.note_said_in_game(text, how)
        except Exception as error:
            self.activity.write(f"Cora wasn't told what you said in game ({type(error).__name__}).", "Push-to-talk")

    async def transcribe_game_speech(self, audio) -> str | None:
        """What was said ("" for nothing), with OpenAI unless the person chose this PC, falling back to
        this PC when OpenAI fails; None when neither worked."""
        if self.game_transcription == "openai":
            started = monotonic()
            try:
                spoken = await asyncio.to_thread(self.game_cloud_transcriber.transcribe, audio, self.game_vocabulary)
            except Exception as error:
                self.activity.write(f"Push-to-talk: OpenAI's transcription failed ({type(error).__name__}); "
                                    "using the model on this PC.", "Push-to-talk")
            else:
                if self.game_warmed is None:
                    connection = "its connection still opening"
                elif self.game_warmed is False:
                    connection = "opening its connection failed"
                else:
                    connection = f"connection ready {self.game_warmed:.2f}s into the press"
                self.game_heard_by = f"OpenAI {monotonic() - started:.2f}s, {connection}"
                return spoken
        started = monotonic()
        try:
            spoken = await asyncio.to_thread(self.game_transcriber.transcribe, audio, self.game_vocabulary)
            self.game_heard_by = f"this PC {monotonic() - started:.2f}s"
            return spoken
        except Exception as error:
            self.activity.write(f"Push-to-talk couldn't transcribe ({type(error).__name__}).", "Push-to-talk")
            return None

    async def place_game_map_pins(self, route: str, lines: list[str]) -> dict:
        """place_wow_map_pins, with lines from game.map_pins: typed now if WoW is in front, else kept
        until it is (the newest route replacing one still waiting). The result goes back to Cora."""
        result = await self.send_to_map_pins_addon(AddonJob(route, lines))
        if result["status"] != "ok":
            return result
        if result.get("waiting"):
            return {"status": "ok", "waiting": True, "message": PINS_WAITING_MESSAGE}
        return {"status": "ok", "placed": len(lines) - 2, "message": PINS_TYPED_MESSAGE}

    async def run_game_map_pins_command(self, line: str) -> dict:
        """wow_map_pins_command, with a line from game.map_pins.command_line: typed now if WoW is in
        front, else kept until it is, after anything already waiting."""
        result = await self.send_to_map_pins_addon(AddonJob(None, [line]))
        if result["status"] != "ok":
            return result
        if result.get("waiting"):
            return {"status": "ok", "waiting": True, "command": line, "message": COMMAND_WAITING_MESSAGE}
        return {"status": "ok", "typed": line, "message": COMMAND_TYPED_MESSAGE}

    async def send_to_map_pins_addon(self, job: AddonJob) -> dict:
        """Types the job (after anything still waiting) if WoW is in front, else queues it."""
        if self.closing:
            return {"status": "error", "message": "Cora is closing; nothing was typed into the game."}
        if not job.lines or not all(map(is_addon_line, job.lines)):
            # Built by game.map_pins, so never expected: the last guard against typing anything else.
            self.activity.write("Refused to type a line into WoW that isn't a CoraMapPins command.", "Map pins")
            return {"status": "error", "message": "That isn't a CoraMapPins command; nothing was typed."}
        async with self.game_typing_lock:
            if job.route is not None:
                # The newest route wins: one still waiting for the game is dropped.
                self.game_pending_pins = [each for each in self.game_pending_pins if each.route is None]
            if await asyncio.to_thread(game_in_front):
                jobs, self.game_pending_pins = [*self.game_pending_pins, job], []
                failures = await self.type_addon_jobs(jobs)
                if job in failures:
                    noun = "pins" if job.route is not None else "command"
                    return {"status": "error",
                            "message": f"The {noun} couldn't be typed into the game ({failures[job]})."}
                return {"status": "ok"}
            job.give_up = monotonic() + PIN_WAIT_SECONDS
            self.game_pending_pins.append(job)
            if job.route is not None:
                self.conversation.show_note(f"Map pins for WoW, typed when the game is in front: {job.route}")
                self.activity.write(f"Map pins '{job.route}' wait for WoW to be the window in front (up to "
                                    f"{PIN_WAIT_SECONDS:.0f} seconds).", "Map pins")
            else:
                self.conversation.show_note(f"For WoW, typed when the game is in front: {job.what}")
                self.activity.write(f"'{job.what}' waits for WoW to be the window in front (up to "
                                    f"{PIN_WAIT_SECONDS:.0f} seconds).", "Map pins")
            # One waiter at a time; a running one picks up what's just been added.
            if self.game_pins_waiter is None or self.game_pins_waiter.is_finished:
                self.game_pins_waiter = self.wait_to_place_map_pins()
        return {"status": "ok", "waiting": True}

    async def type_addon_jobs(self, jobs: list[AddonJob]) -> dict:
        """Types each job in turn, WoW having just been seen in front, and reports each one. Returns
        {job: error} for those that weren't (fully) typed. The caller holds game_typing_lock."""
        failures = {}
        for index, job in enumerate(jobs):
            try:
                await self.type_map_pins(job.lines, checked=index == 0)
            except OSError as error:
                failures[job] = error
                self.map_pins_failed(job, error)
                continue
            self.map_pins_placed(job)
        return failures

    async def type_map_pins(self, lines: list[str], checked: bool = True) -> None:
        """Each line as its own Enter, line, Enter batch; checked: WoW was just seen in front, so the
        first line needn't look again. OSError when WoW isn't in front for a line; for a route the addon
        then places nothing, as it only places a route at its last line. The caller holds
        game_typing_lock."""
        for index, line in enumerate(lines):
            if (index or not checked) and not await asyncio.to_thread(game_in_front):
                raise OSError("WoW stopped being the window in front part-way, so nothing was placed"
                              if index else "WoW wasn't the window in front, so nothing was typed")
            await asyncio.to_thread(type_into_game, line)
            if index < len(lines) - 1:
                await asyncio.sleep(PIN_LINE_GAP_SECONDS)

    @work(group="game-map-pins")
    async def wait_to_place_map_pins(self) -> None:
        while not self.closing and self.game_pending_pins:
            now = monotonic()
            for job in [each for each in self.game_pending_pins if now >= each.give_up]:
                self.game_pending_pins.remove(job)
                self.map_pins_gave_up(job)
            if not self.game_pending_pins:
                return
            if await asyncio.to_thread(game_in_front):
                async with self.game_typing_lock:
                    jobs, self.game_pending_pins = self.game_pending_pins, []
                    await self.type_addon_jobs(jobs)
                continue
            await asyncio.sleep(PIN_CHECK_SECONDS)

    def map_pins_placed(self, job: AddonJob) -> None:
        if job.route is None:
            self.conversation.show_note(f"Cora typed into WoW: {job.what}")
            self.activity.write(f"Typed '{job.what}' into WoW for CoraMapPins.", "Map pins")
            return
        count = len(job.lines) - 2
        plural = "s" if count != 1 else ""
        self.conversation.show_note(f"Cora put {count} map pin{plural} in WoW: {job.route}")
        self.activity.write(f"Typed {count} map pin{plural} ('{job.route}') into WoW for CoraMapPins.", "Map pins")

    def map_pins_failed(self, job: AddonJob, error: OSError) -> None:
        if job.route is None:
            self.conversation.show_note(f"Not typed into WoW: {job.what}")
            self.activity.write(f"'{job.what}' couldn't be typed into WoW: {error}", "Map pins")
            return
        self.conversation.show_note(f"Map pins not placed in WoW: {job.route}")
        self.activity.write(f"Map pins '{job.route}' couldn't be typed into WoW: {error}", "Map pins")

    def map_pins_gave_up(self, job: AddonJob) -> None:
        if job.route is None:
            self.conversation.show_note(f"Not typed into WoW (it wasn't brought to the front): {job.what}")
            self.activity.write(f"'{job.what}' not typed: WoW wasn't the window in front in time.", "Map pins")
            return
        self.conversation.show_note(f"Map pins not placed (WoW wasn't brought to the front): {job.route}")
        self.activity.write(f"Map pins '{job.route}' not placed: WoW wasn't the window in front in time.", "Map pins")

    @work(group="model-settings-save")
    async def save_game_transcription(self, choice: str) -> None:
        if choice not in TRANSCRIPTION_CHOICES:
            return
        self.game_transcription = choice
        self.activity.write("Push-to-talk now turns speech into text " +
                            ("with OpenAI." if choice == "openai" else "on this PC."), "Settings changed")
        try:
            await self.conversation_persistence.call_database(self.persistence.save_game_transcription, choice)
        except Exception as error:
            self.activity.write(f"Game setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save the speech to text choice; it applies until Cora closes.", severity="error")

    @work(group="model-settings-save")
    async def save_game_vocabulary(self, words: str) -> None:
        words = " ".join(words.split())
        if len(words) > MAX_VOCABULARY_CHARS:
            self.notify(f"Keep names and words to {MAX_VOCABULARY_CHARS} characters.", severity="warning")
            return
        self.game_vocabulary = words
        try:
            await self.conversation_persistence.call_database(self.persistence.save_game_vocabulary, words)
        except Exception as error:
            self.activity.write(f"Game setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save the names and words; they apply until Cora closes.", severity="error")
            return
        self.notify("Names and words saved." if words else "Names and words removed.")

    @work(group="model-settings-save")
    async def save_game_push_to_talk(self, on: bool) -> None:
        self.game_push_to_talk = on
        if on:
            self.start_push_to_talk()
        else:
            self.stop_push_to_talk()
            await self.restore_live_microphone()
            self.activity.write("Push-to-talk off.", "Push-to-talk off")
        try:
            await self.conversation_persistence.call_database(self.persistence.save_game_push_to_talk, on)
        except Exception as error:
            self.activity.write(f"Game setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save push-to-talk; it applies until Cora closes.", severity="error")

    @work(group="model-settings-save")
    async def save_game_chat_channel(self, channel: str) -> None:
        if channel not in CHANNELS:
            return
        self.game_channel = channel
        self.activity.write(f"Push-to-talk now speaks with {CHANNELS[channel][0]}.", "Settings changed")
        try:
            await self.conversation_persistence.call_database(self.persistence.save_game_chat_channel, channel)
        except Exception as error:
            self.activity.write(f"Game setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save the chat channel; it applies until Cora closes.", severity="error")
