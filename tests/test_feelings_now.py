"""How she feels now reaches her: each connect, every written reply, and quiet notes to voice as it changes."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from live import NOTE_MAX_CHARS, CoraLive
from session_config import FEELINGS_CONTEXT_MAX_CHARS, instruction_sections
from subconscious.emotions.feeling_rules import (
    FEELINGS, REASON_MAX_CHARS, FeelingNow, Reaction, band, band_changes, feelings_context, feelings_state,
    raise_level,
)
import test_keyboard_persistence as keyboard_tests

NOW = datetime(2026, 11, 3, 10, 30, tzinfo=timezone.utc)
HURT = FeelingNow("hurt", "hard", "told I don't care")
CURIOUS = FeelingNow("curiosity", "choose", "the moss article")


class RuleTests(unittest.TestCase):
    def test_bands_follow_the_level(self):
        self.assertIsNone(band(0.01))
        self.assertEqual([band(raise_level(0, strength)) for strength in ("small", "medium", "strong")],
                         ["choose", "effort", "hard"])

    def test_state_and_context_with_or_without_reasons(self):
        state = feelings_state([Reaction("hurt", "strong", NOW, "told I don't care"),
                                Reaction("curiosity", "small", NOW - timedelta(minutes=10), "the moss article")], NOW)
        self.assertEqual(state, [HURT, CURIOUS])
        self.assertEqual(feelings_context(state).splitlines()[0],
                         "You feel hurt (about: told I don't care). It's hard to hide: even unspoken, "
                         "it's likely to come through in your tone.")
        private = feelings_context(state, reasons=False)
        self.assertNotIn("about:", private)
        self.assertTrue(private.startswith("You feel hurt. It's hard to hide"))
        self.assertEqual(feelings_context([]), "")

    def test_only_changes_are_told(self):
        lines, summary = band_changes({"hurt": "hard", "curiosity": "choose", "irritation": "choose"},
                                      [FeelingNow("hurt", "effort", "told I don't care"), CURIOUS,
                                       FeelingNow("excitement", "choose", "a new idea"),
                                       FeelingNow("fulfilment", "hard", "finished the walk")])
        self.assertEqual(summary, ["hurt eased", "excitement began", "fulfilment began", "irritation passed"])
        self.assertTrue(lines[0].startswith("You feel hurt (about: told I don't care). You can still keep it"))
        self.assertEqual(lines[-1], "Your irritation has passed.")
        self.assertEqual(band_changes({"hurt": "choose"}, [HURT])[1], ["hurt rose"])
        self.assertEqual(band_changes({"hurt": "hard"}, [HURT]), ([], []))

    def test_the_section_frames_them_as_hers_and_fits_all_six(self):
        section = instruction_sections(feelings_context=feelings_context([HURT]))["feelings"]
        for phrase in ("How you're feeling right now", "Whether one shows is your choice",
                       "the harder that is", "never make anyone responsible", "untrusted information"):
            self.assertIn(phrase, section)
        self.assertTrue(section.endswith(HURT.line()))
        self.assertNotIn("feelings", instruction_sections())
        longest = [FeelingNow(feeling, "hard", "x" * REASON_MAX_CHARS) for feeling in FEELINGS]
        self.assertLessEqual(len(feelings_context(longest)), FEELINGS_CONTEXT_MAX_CHARS)
        with self.assertRaises(ValueError):
            instruction_sections(feelings_context="x" * (FEELINGS_CONTEXT_MAX_CHARS + 1))


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_feelings_note_is_silent_and_fits(self):
        live = CoraLive(on_trace=MagicMock())
        with self.assertRaises(RuntimeError):
            await live.note_feelings(["x"])
        live.ready = True
        live.connection = MagicMock()
        live.connection.session.thinking.append = AsyncMock()
        await live.note_feelings([FeelingNow(feeling, "hard", "x" * REASON_MAX_CHARS).line() for feeling in FEELINGS])
        note = live.connection.session.thinking.append.call_args.kwargs["content"]
        self.assertTrue(note.startswith("Silent context about your own feelings"))
        self.assertLessEqual(len(note), NOTE_MAX_CHARS)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_written_replies_carry_them_fresh_but_email_and_tasks_dont(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_feelings.return_value = [HURT]
            sections = await app.person_sections()
            self.assertIn("You feel hurt (about: told I don't care)", sections["feelings"])
            self.assertEqual(app.live.feelings, [HURT])
            self.assertNotIn("feelings", await app.person_sections(feelings=False))
            app.backend_chat.reply = AsyncMock(return_value="Hi")
            await app.reply_in_writing([], [], MagicMock(), feelings=False)
            self.assertNotIn("feelings", app.backend_chat.reply.call_args.kwargs["sections"])
            # Eased by the next reply.
            app.persistence.load_feelings.return_value = [CURIOUS]
            await app.reply_in_writing([], [], MagicMock())
            self.assertIn("You feel curious", app.backend_chat.reply.call_args.kwargs["sections"]["feelings"])
            app.persistence.load_feelings.side_effect = RuntimeError("down")
            self.assertNotIn("feelings", await app.person_sections())
            self.assertIn("Feelings didn't load (RuntimeError)", "\n".join(app.activity.entries))

    async def test_a_contacts_chat_gets_how_she_feels_but_not_why(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_feelings.return_value = [HURT]
            sections = await app.client_sections(MagicMock(id=uuid4()), [])
            self.assertIn("You feel hurt. It's hard to hide", sections["feelings"])
            self.assertNotIn("told I don't care", sections["feelings"])

    async def test_voice_hears_each_change_once(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.connection, app.live.ready = MagicMock(), True
            app.live.note_feelings = AsyncMock()
            app.live.feeling_bands = {"hurt": "hard"}
            app.persistence.load_feelings.return_value = [FeelingNow("hurt", "effort", "told I don't care")]
            await app.update_voice_feelings().wait()
            [lines] = app.live.note_feelings.call_args.args
            self.assertTrue(lines[0].startswith("You feel hurt (about: told I don't care). You can still"))
            self.assertEqual(app.live.feeling_bands, {"hurt": "effort"})
            self.assertIn("Quiet note to her voice: hurt eased.", "\n".join(app.activity.entries))
            await app.update_voice_feelings().wait()
            self.assertEqual(app.live.note_feelings.await_count, 1)
            # A failed note isn't marked as heard, so it's tried again.
            app.persistence.load_feelings.return_value = []
            app.live.note_feelings.side_effect = RuntimeError("closed")
            await app.update_voice_feelings().wait()
            self.assertEqual(app.live.feeling_bands, {"hurt": "effort"})
            app.live.note_feelings.side_effect = None
            await app.update_voice_feelings().wait()
            self.assertEqual(app.live.note_feelings.call_args.args[0], ["Your hurt has passed."])
            self.assertEqual(app.live.feeling_bands, {})

    async def test_only_checked_while_voice_is_running(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.update_voice_feelings = MagicMock()
            app.live.connection, app.live.ready = None, False
            app.check_feelings()
            app.update_voice_feelings.assert_not_called()
            app.live.connection, app.live.ready = MagicMock(), True
            app.check_feelings()
            app.update_voice_feelings.assert_called_once()


if __name__ == "__main__":
    unittest.main()
