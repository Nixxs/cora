"""The chosen delegation model reaches the Live session config. Mocked APIs only."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import CoraApp
from live import CoraLive
from session_config import BACKEND_MODEL, build_session_config
from storage.keyboard_persistence import KeyboardPersistence
import test_keyboard_persistence as keyboard_tests
from test_startup_memories import fake_live_transport


CONNECT_TO_CORA = CoraApp.connect_to_cora


class DelegationConfigTests(unittest.TestCase):
    def test_session_config_uses_the_given_model_or_the_default(self):
        self.assertEqual(build_session_config([])["delegation"]["responses"]["model"], BACKEND_MODEL)
        config = build_session_config([], delegation_model="gpt-6-luna")
        self.assertEqual(config["delegation"]["responses"]["model"], "gpt-6-luna")

    def test_adapter_reads_the_delegation_area_with_the_conversation_engine(self):
        store = MagicMock()
        with patch("storage.keyboard_persistence.ModelSettingsStore") as settings_type:
            settings_type.return_value.model_for.return_value = "gpt-6-luna"
            self.assertEqual(KeyboardPersistence(store).load_delegation_model(), "gpt-6-luna")
        settings_type.assert_called_once_with(store.engine)
        settings_type.return_value.model_for.assert_called_once_with("delegation")
        store.create_conversation.assert_not_called()


class DelegationLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_run_sends_the_chosen_model(self):
        live = CoraLive()
        self.assertEqual(live.delegation_model, BACKEND_MODEL)
        live.delegation_model = "gpt-6-luna"
        with fake_live_transport() as (start, speaker, chunks):
            await asyncio.wait_for(live.run(MagicMock(), MagicMock()), 3)
        self.assertEqual(start.call_args.kwargs["session"], build_session_config([], delegation_model="gpt-6-luna"))

    async def test_every_connect_loads_the_model_before_live_starts(self):
        async with keyboard_tests.running_app() as (app, pilot):
            seen = []
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_delegation_model.return_value = "gpt-6-luna"
            app.live.run = AsyncMock(side_effect=lambda *args: seen.append(app.live.delegation_model))
            for _ in range(2):  # launch, then waking from standby
                app.connection_worker = CONNECT_TO_CORA(app)
                await asyncio.wait_for(app.connection_worker.wait(), 3)
            self.assertEqual(seen, ["gpt-6-luna"] * 2)
            self.assertEqual(app.persistence.load_delegation_model.call_count, 2)

    async def test_failure_falls_back_to_the_default_without_private_details(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.delegation_model = "gpt-6-luna"
            app.persistence.mark_interrupted_conversations.return_value = 0
            app.persistence.load_delegation_model.side_effect = RuntimeError("private SQL")
            app.live.run = AsyncMock()
            app.connection_worker = CONNECT_TO_CORA(app)
            await asyncio.wait_for(app.connection_worker.wait(), 3)
            app.live.run.assert_awaited_once()
            self.assertEqual(app.live.delegation_model, BACKEND_MODEL)
            activity = "\n".join(app.activity.entries)
            self.assertIn("Delegation model load failed (RuntimeError); connecting with the default.", activity)
            self.assertNotIn("private SQL", activity)


if __name__ == "__main__":
    unittest.main()
