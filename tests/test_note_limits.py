"""Live rejects a context note over 500 tokens: every note stays under NOTE_MAX_CHARS, its instructions intact."""

from datetime import datetime, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock

from greeting import finding_hook, greeting_brief, last_thread_hook, memory_hook, signal_hook
from live import NOTE_MAX_CHARS, CoraLive, fit_note
from subconscious.relationship.relationship_rules import RelationshipValues

LONG = "word " * 2000
NOW = datetime(2026, 11, 3, 10, 30).astimezone()


class NoteLimitTests(unittest.IsolatedAsyncioTestCase):
    def live(self):
        live = CoraLive(on_trace=MagicMock())
        live.ready = True
        live.connection = MagicMock()
        live.connection.session.commentary.append = AsyncMock()
        live.connection.session.thinking.append = AsyncMock()
        return live

    def sent(self, live, kind="commentary"):
        return getattr(live.connection.session, kind).append.call_args.kwargs["content"]

    def test_fit_note_cuts_with_an_ellipsis(self):
        self.assertEqual(fit_note("short", 10), "short")
        self.assertEqual(fit_note("abcdefghijk", 5), "abcd…")

    async def test_the_greeting_with_any_long_hook_keeps_its_ending(self):
        for name, hook in (("summary", last_thread_hook(LONG)), ("finding", finding_hook(LONG, LONG)),
                           ("memory", memory_hook(LONG)), ("signal", signal_hook([(LONG, LONG)] * 3))):
            for values in (RelationshipValues(0.1, 0.1, 0.5), RelationshipValues(0.9, 0.9, 0.9)):
                with self.subTest(hook=name, values=values):
                    live = self.live()
                    await live.send_greeting(greeting_brief(LONG[:80], NOW - timedelta(days=3), NOW, hook, values))
                    content = self.sent(live)
                    # Under the limit by the hooks' own caps, not by cutting the note's end.
                    self.assertLess(len(content), NOTE_MAX_CHARS)
                    self.assertFalse(content.endswith("…"))

    async def test_every_other_note_stays_under_the_limit_with_its_instructions(self):
        for method, args, kind in (
            ("send_finding", (LONG, LONG, NOW), "commentary"),
            ("send_relevant_finding", (LONG, LONG, NOW), "commentary"),
            ("send_opportunity", (LONG, LONG), "commentary"),
            ("send_message_for_person", (LONG,), "commentary"),
            ("send_observation", (LONG,), "commentary"),
            ("note_signal_exchange", (LONG, LONG), "thinking"),
        ):
            with self.subTest(method=method):
                live = self.live()
                await getattr(live, method)(*args)
                content = self.sent(live, kind)
                self.assertLess(len(content), NOTE_MAX_CHARS)
                self.assertIn("not a", content[:200])  # its framing comes first and is kept

    async def test_anything_longer_is_cut_as_a_last_resort(self):
        live = self.live()
        await live.append_commentary(live.connection, "e1", LONG)
        self.assertEqual(len(self.sent(live)), NOTE_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
