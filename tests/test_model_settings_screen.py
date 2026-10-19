"""The header settings button opens per-area model pickers that save on change. Mocked APIs only."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from textual.widgets import Select

from session_config import BACKEND_MODEL, MODEL_AREAS
from storage.keyboard_persistence import KeyboardPersistence
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests


MODELS = {area: BACKEND_MODEL for area in MODEL_AREAS}


class ModelSettingsAdapterTests(unittest.TestCase):
    def test_adapter_loads_and_saves_with_the_conversation_engine(self):
        store = MagicMock()
        with patch("storage.keyboard_persistence.ModelSettingsStore") as settings_type:
            settings_type.return_value.models.return_value = MODELS
            persistence = KeyboardPersistence(store)
            self.assertEqual(persistence.load_models(), MODELS)
            persistence.save_model("memory", "gpt-6-luna")
        settings_type.assert_called_with(store.engine)
        settings_type.return_value.set_model.assert_called_once_with("memory", "gpt-6-luna")
        store.create_conversation.assert_not_called()


class ModelSettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def open_settings(self, app, pilot):
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()

    async def test_opens_with_saved_models_and_saves_only_real_changes(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = {**MODELS, "memory": "gpt-6-luna"}
            await self.open_settings(app, pilot)
            self.assertIsInstance(app.screen, ModelSettingsScreen)
            self.assertEqual(app.screen.query_one("#model-memory", Select).value, "gpt-6-luna")
            self.assertEqual(app.screen.query_one("#model-utility", Select).value, BACKEND_MODEL)
            app.persistence.save_model.assert_not_called()

            app.screen.query_one("#model-utility", Select).value = "gpt-5.6-terra"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_model.assert_called_once_with("utility", "gpt-5.6-terra")
            self.assertIn("The utility model is now gpt-5.6-terra.", "\n".join(app.activity.entries))

            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModelSettingsScreen)

    async def test_only_a_delegation_change_on_a_ready_live_reconnects(self):
        cases = [  # area, ready, standby, reconnects
            ("delegation", True, False, True),
            ("memory", True, False, False),
            ("delegation", False, False, False),
            ("delegation", True, True, False),
        ]
        for area, ready, standby, reconnects in cases:
            with self.subTest(area=area, ready=ready, standby=standby):
                async with keyboard_tests.running_app() as (app, pilot):
                    app.live.ready, app.standby = ready, standby
                    with patch.object(app, "request_live_transition") as transition:
                        await app.save_model_setting(area, "gpt-6-luna").wait()
                    app.persistence.save_model.assert_called_once_with(area, "gpt-6-luna")
                    if reconnects:
                        transition.assert_called_once_with(restart=True)
                    else:
                        transition.assert_not_called()

    async def test_failed_save_does_not_reconnect(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.ready, app.standby = True, False
            app.persistence.save_model.side_effect = RuntimeError("private SQL")
            with patch.object(app, "request_live_transition") as transition, patch.object(app, "notify"):
                await app.save_model_setting("delegation", "gpt-6-luna").wait()
            transition.assert_not_called()

    async def test_close_button_dismisses(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            await self.open_settings(app, pilot)
            await pilot.click("#model-settings-close")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ModelSettingsScreen)

    async def test_load_failure_reports_without_private_details(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.side_effect = RuntimeError("private SQL")
            with patch.object(app, "notify") as notify:
                await self.open_settings(app, pilot)
                notify.assert_called_once()
            self.assertNotIsInstance(app.screen, ModelSettingsScreen)
            activity = "\n".join(app.activity.entries)
            self.assertIn("Model settings load failed (RuntimeError).", activity)
            self.assertNotIn("private SQL", activity)

    async def test_save_failure_reports_without_private_details(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = MODELS
            app.persistence.save_model.side_effect = RuntimeError("private SQL")
            await self.open_settings(app, pilot)
            with patch.object(app, "notify") as notify:
                app.screen.query_one("#model-memory", Select).value = "gpt-6-luna"
                await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                notify.assert_called_once()
            activity = "\n".join(app.activity.entries)
            self.assertIn("Model setting save failed (RuntimeError).", activity)
            self.assertNotIn("private SQL", activity)


if __name__ == "__main__":
    unittest.main()
