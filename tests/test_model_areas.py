"""Model areas of their own: looking at images, contacts and tasks, and exploring. Mocked APIs only."""

import unittest
from unittest.mock import MagicMock, patch

from session_config import BACKEND_MODEL, MODEL_AREAS, MODEL_CHOICES
from tools import look_at_screen
from ui.model_settings import AREA_LABELS
import test_keyboard_persistence as keyboard_tests


def models(**chosen):
    return {area: chosen.get(area, BACKEND_MODEL) for area in MODEL_AREAS}


class ImageModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(look_at_screen.set_image_model, BACKEND_MODEL)

    def test_every_look_uses_the_chosen_image_model(self):
        look_at_screen.set_image_model("gpt-5.6-terra")
        with patch.object(look_at_screen, "OpenAI") as client_type:
            client = client_type.return_value.__enter__.return_value
            client.responses.create.return_value = MagicMock(status="completed", output_text="A cat.")
            self.assertEqual(look_at_screen.look("What is it?", b"jpeg"), "A cat.")
        self.assertEqual(client.responses.create.call_args.kwargs["model"], "gpt-5.6-terra")

    async def test_it_loads_at_connect_and_a_change_in_settings_applies_straight_away(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.persistence.load_models.return_value = models(images="gpt-6-luna")
            await app.conversation_persistence.load_session_context()
            self.assertEqual(look_at_screen.image_model, "gpt-6-luna")
            await app.save_model_setting("images", "gpt-5.6-terra").wait()
            self.assertEqual(look_at_screen.image_model, "gpt-5.6-terra")
            app.persistence.save_model.assert_called_with("images", "gpt-5.6-terra")

    async def test_an_unreadable_choice_keeps_the_one_it_has(self):
        async with keyboard_tests.running_app() as (app, pilot):
            look_at_screen.set_image_model("gpt-6-luna")
            app.persistence.load_models.side_effect = RuntimeError("database down")
            await app.conversation_persistence.load_session_context()
            self.assertEqual(look_at_screen.image_model, "gpt-6-luna")


class ContactsModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_contacts_and_tasks_use_their_own_model_or_delegations_when_it_cant_be_read(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.live.delegation_model = "gpt-6-astra"
            app.persistence.load_models.return_value = models(contacts="gpt-6-luna")
            self.assertEqual(await app.contacts_model(), "gpt-6-luna")
            app.persistence.load_models.return_value = models(contacts="retired-model")
            self.assertEqual(await app.contacts_model(), "gpt-6-astra")
            app.persistence.load_models.side_effect = RuntimeError("private SQL")
            self.assertEqual(await app.contacts_model(), "gpt-6-astra")
            self.assertNotIn("private SQL", "\n".join(app.activity.entries))


class AreaTests(unittest.TestCase):
    def test_every_area_has_a_label_and_what_it_covers(self):
        self.assertEqual(set(AREA_LABELS), set(MODEL_AREAS))
        for area, (label, covers) in AREA_LABELS.items():
            with self.subTest(area=area):
                self.assertTrue(label and covers)
        self.assertIn(BACKEND_MODEL, MODEL_CHOICES)


if __name__ == "__main__":
    unittest.main()
