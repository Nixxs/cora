"""Push-to-talk into the game's chat (Settings > Game): part of CoraApp (see app.py).

Holding the middle mouse button mutes Live's microphone and records; letting go transcribes it
(with OpenAI, or on this PC) and, if World of Warcraft is the window in front, types it into the game's chat. Cora is
told quietly what was said, so she knows it was for the game, not her."""

import asyncio

from textual import work

from game.chat_line import CHANNELS, DEFAULT_CHANNEL, casual, chat_line, chat_text, game_in_front, type_into_game
from game.mouse_button import MiddleButtonWatcher
from game.speech import (
    DEFAULT_TRANSCRIPTION, GAME_WORDS, MAX_VOCABULARY_CHARS, TRANSCRIPTION_CHOICES, CloudTranscriber, Recorder, Transcriber,
)


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
        self.game_lock = asyncio.Lock()

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
        async with self.game_lock:
            if not self.game_talking:
                return
            self.game_talking = False
            audio = await asyncio.to_thread(self.game_recorder.stop)
            await self.restore_live_microphone()
        spoken = await self.transcribe_game_speech(audio)
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
            await asyncio.to_thread(type_into_game, chat_line(text, self.game_channel))
        except OSError as error:
            self.conversation.show_note(f"Not sent to WoW: {text}")
            self.activity.write(f"Push-to-talk couldn't type into WoW: {error}", "Push-to-talk")
            return
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
            try:
                return await asyncio.to_thread(self.game_cloud_transcriber.transcribe, audio, self.game_vocabulary)
            except Exception as error:
                self.activity.write(f"Push-to-talk: OpenAI's transcription failed ({type(error).__name__}); "
                                    "using the model on this PC.", "Push-to-talk")
        try:
            return await asyncio.to_thread(self.game_transcriber.transcribe, audio, self.game_vocabulary)
        except Exception as error:
            self.activity.write(f"Push-to-talk couldn't transcribe ({type(error).__name__}).", "Push-to-talk")
            return None

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
