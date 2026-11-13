"""Push-to-talk into the game's chat (Settings > Game). Mocked microphone, Whisper and keyboard only."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from textual.widgets import Input, Select, Switch

from game import chat_line
from game.chat_line import (
    KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, MAX_MESSAGE_CHARS, VK_RETURN, chat_text, key_events,
)
from game.speech import GAME_WORDS, CloudTranscriber, Transcriber, vocabulary, wav_bytes
from storage.app_settings_store import AppSettingsStore
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS


class ChatLineTests(unittest.TestCase):
    def test_speech_becomes_one_clean_line(self):
        self.assertEqual(chat_text("  hello |there\n  friend "), "hello there friend")
        self.assertEqual(chat_text("   "), "")
        self.assertEqual(chat_line.chat_line("hi all", "party"), "/p hi all")
        self.assertEqual(chat_line.chat_line("hi", "unknown"), "/s hi")

    def test_it_is_typed_casually_but_names_keep_their_capitals(self):
        names = (*GAME_WORDS, "Ja Avd, Snazzbot")
        cases = {
            "Anyone want to group up? I'm a druid, heading to Teldrassil.":
                "anyone want to group up? I'm a druid, heading to Teldrassil",
            "Teldrassil is huge. Wait... LFG anyone?": "Teldrassil is huge wait... LFG anyone?",
            "Ja Avd, thanks! Heading there now.": "Ja Avd, thanks! heading there now",
            "Hey. Did you see that?": "hey did you see that?",
            "I think so.": "I think so",
        }
        for spoken, typed in cases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(chat_line.casual(spoken, names), typed)

    def test_long_speech_is_cut_at_a_word_within_wows_limit(self):
        text = chat_text("word " * 100)
        self.assertLessEqual(len(text), MAX_MESSAGE_CHARS)
        self.assertTrue(text.endswith("word"))
        self.assertEqual(len(chat_text("x" * 400)), MAX_MESSAGE_CHARS)

    def test_the_keys_are_enter_the_line_then_enter(self):
        events = key_events("/s hé")
        self.assertEqual(events[:2], [(VK_RETURN, 0x1C, 0), (VK_RETURN, 0x1C, KEYEVENTF_KEYUP)])
        self.assertEqual(events[-2:], events[:2])
        typed = events[2:-2]
        self.assertEqual(len(typed), 2 * len("/s hé"))
        self.assertEqual([unit for _, unit, flags in typed if not flags & KEYEVENTF_KEYUP], [ord(c) for c in "/s hé"])
        self.assertTrue(all(flags & KEYEVENTF_UNICODE for _, _, flags in typed))


class TranscriberTests(unittest.TestCase):
    def test_short_or_silent_audio_is_nothing_without_loading_the_model(self):
        transcriber = Transcriber()
        with patch.object(transcriber, "load") as load:
            self.assertEqual(transcriber.transcribe(np.zeros(1000, dtype=np.float32)), "")
            self.assertEqual(transcriber.transcribe(np.zeros(16000, dtype=np.float32)), "")
        load.assert_not_called()

    def test_the_prompt_puts_their_own_names_first(self):
        prompt = vocabulary("  Ja Avd, Snazzbot ")
        self.assertIn("Names: Ja Avd, Snazzbot.", prompt)
        self.assertLess(prompt.index("Snazzbot"), prompt.index("Teldrassil"))
        self.assertNotIn("Names:", vocabulary(""))
        self.assertTrue(all(word in prompt for word in GAME_WORDS))

    def test_openai_gets_a_wav_and_the_words(self):
        client = MagicMock()
        client.audio.transcriptions.create.return_value = MagicMock(text=" Off to Dolanaar ")
        audio = np.full(16000, 0.1, dtype=np.float32)
        self.assertEqual(CloudTranscriber(client=client).transcribe(audio, "Githyiss"), "Off to Dolanaar")
        options = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(options["model"], "gpt-4o-transcribe")
        self.assertTrue(options["file"][1].startswith(b"RIFF"))
        self.assertIn("Githyiss", options["prompt"])
        client.audio.transcriptions.create.reset_mock()
        self.assertEqual(CloudTranscriber(client=client).transcribe(np.zeros(16000, dtype=np.float32)), "")
        client.audio.transcriptions.create.assert_not_called()
        self.assertEqual(len(wav_bytes(audio)), 44 + 2 * 16000)


class StoreTests(unittest.TestCase):
    def test_push_to_talk_is_off_and_say_unless_chosen(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, False), ("off", False), ("on", True)):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertIs(store.game_push_to_talk(), expected)
        for saved, expected in ((None, "say"), ("guild", "guild"), ("whisper", "say")):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.game_chat_channel(), expected)
        with patch.object(store, "_set") as saved:
            store.set_game_push_to_talk(True)
            store.set_game_chat_channel("party")
            self.assertEqual([call.args for call in saved.call_args_list],
                             [("game_push_to_talk", "on"), ("game_chat_channel", "party")])
            with self.assertRaises(ValueError):
                store.set_game_chat_channel("/run something")

    def test_transcription_is_openai_unless_chosen_and_words_are_tidied(self):
        store = AppSettingsStore(MagicMock())
        for saved, expected in ((None, "openai"), ("local", "local"), ("whisper", "openai")):
            with self.subTest(saved=saved), patch.object(store, "_get", return_value=saved):
                self.assertEqual(store.game_transcription(), expected)
        with patch.object(store, "_get", return_value=None):
            self.assertEqual(store.game_vocabulary(), "")
        with patch.object(store, "_set") as saved:
            store.set_game_vocabulary("  Ja  Avd,\n Githyiss ")
            saved.assert_called_once_with("game_vocabulary", "Ja Avd, Githyiss")
            with self.assertRaises(ValueError):
                store.set_game_transcription("cloud")
            with self.assertRaises(ValueError):
                store.set_game_vocabulary("x" * 201)


class PushToTalkTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, app, heard="hello there", in_front=True):
        app.game_watcher = MagicMock(running=True)
        app.game_recorder = MagicMock(recording=False)
        app.game_recorder.stop.return_value = np.zeros(16000, dtype=np.float32)
        app.game_cloud_transcriber = MagicMock()
        app.game_cloud_transcriber.transcribe.return_value = heard.capitalize() + "." if heard else heard
        app.game_transcriber = MagicMock()
        app.game_transcriber.transcribe.return_value = "from this pc"
        app.live.ready = True
        app.live.microphone_muted = False

        async def mute(muted):
            app.live.microphone_muted = muted

        app.live.set_microphone_muted = AsyncMock(side_effect=mute)
        app.live.note_said_in_game = AsyncMock()
        typed = self.enterContext(patch("app_areas.game.type_into_game"))
        self.enterContext(patch("app_areas.game.game_in_front", return_value=in_front))
        return typed

    async def talk(self, app, pilot):
        app.game_button_pressed()
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        self.assertTrue(app.live.microphone_muted)
        app.game_button_released()
        await pilot.pause()
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)

    async def test_holding_mutes_her_and_letting_go_types_it_and_tells_her(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare(app)
            app.game_channel = "party"
            await self.talk(app, pilot)
            self.assertFalse(app.live.microphone_muted)
            typed.assert_called_once_with("/p hello there")
            app.live.note_said_in_game.assert_awaited_once()
            self.assertEqual(app.live.note_said_in_game.await_args.args[0], "hello there")
            self.assertIn("You in WoW (/p): hello there", "\n".join(map(str, app.conversation.log_entries)))

    async def test_openai_failing_falls_back_to_this_pc_and_their_words_go_to_both(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare(app)
            app.game_vocabulary = "Githyiss"
            app.game_cloud_transcriber.transcribe.side_effect = TimeoutError()
            await self.talk(app, pilot)
            typed.assert_called_once_with("/s from this pc")
            self.assertEqual(app.game_cloud_transcriber.transcribe.call_args.args[1], "Githyiss")
            self.assertEqual(app.game_transcriber.transcribe.call_args.args[1], "Githyiss")
            self.assertIn("OpenAI's transcription failed (TimeoutError)", "\n".join(app.activity.entries))

    async def test_choosing_this_pc_never_calls_openai(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare(app)
            app.game_transcription = "local"
            await self.talk(app, pilot)
            app.game_cloud_transcriber.transcribe.assert_not_called()
            typed.assert_called_once_with("/s from this pc")

    async def test_nothing_is_typed_unless_wow_is_in_front(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare(app, in_front=False)
            await self.talk(app, pilot)
            typed.assert_not_called()
            app.live.note_said_in_game.assert_not_awaited()
            self.assertFalse(app.live.microphone_muted)

    async def test_nothing_heard_types_nothing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            typed = self.prepare(app, heard="")
            await self.talk(app, pilot)
            typed.assert_not_called()
            self.assertIn("heard nothing", "\n".join(app.activity.entries))

    async def test_a_mic_she_already_had_muted_stays_muted(self):
        async with keyboard_tests.running_app() as (app, pilot):
            self.prepare(app)
            app.live.microphone_muted = True
            app.game_button_pressed()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.game_button_released()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.live.set_microphone_muted.assert_not_awaited()
            self.assertTrue(app.live.microphone_muted)

    async def test_saved_settings_start_it_at_launch_and_unset_ones_dont(self):
        async with keyboard_tests.running_app() as (app, pilot):
            with patch.object(app, "start_push_to_talk") as start:
                app.apply_game_settings(MagicMock())
                start.assert_not_called()
                self.assertEqual(app.game_channel, "say")
                self.assertEqual((app.game_transcription, app.game_vocabulary), ("openai", ""))
                app.apply_game_settings({"push_to_talk": True, "channel": "guild",
                                         "transcription": "local", "vocabulary": "Ja Avd"})
                start.assert_called_once()
                self.assertEqual((app.game_channel, app.game_transcription, app.game_vocabulary),
                                 ("guild", "local", "Ja Avd"))

    async def test_the_game_tab_switches_it_and_picks_the_channel(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            await pilot.click("#settings-button")
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            with patch.object(app, "start_push_to_talk") as start:
                app.screen.query_one("#game-push-to-talk", Switch).value = True
                await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                start.assert_called_once()
            app.persistence.save_game_push_to_talk.assert_called_once_with(True)
            app.screen.query_one("#game-chat-channel", Select).value = "yell"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.game_channel, "yell")
            app.persistence.save_game_chat_channel.assert_called_once_with("yell")
            app.screen.query_one("#game-transcription", Select).value = "local"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.game_transcription, "local")
            app.persistence.save_game_transcription.assert_called_once_with("local")
            app.screen.query_one("#game-vocabulary", Input).value = "Ja Avd,  Githyiss"
            app.screen.save_game_vocabulary()
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            self.assertEqual(app.game_vocabulary, "Ja Avd, Githyiss")
            app.persistence.save_game_vocabulary.assert_called_once_with("Ja Avd, Githyiss")


if __name__ == "__main__":
    unittest.main()
