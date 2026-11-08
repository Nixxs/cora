"""The optional Admin key for OpenAI spend: reading costs, finding Cora's project, and Settings. Mocked APIs only."""

import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

import httpx2
from textual.widgets import Button, Input, Select, Static, TabbedContent

import openai_spend
from openai_spend import ALL_PROJECTS, AdminKeyError
from storage.app_settings_store import AppSettingsStore
from ui.model_settings import ModelSettingsScreen
import test_keyboard_persistence as keyboard_tests
from test_model_settings_screen import MODELS

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def bucket(*results):
    return {"object": "bucket", "start_time": 0, "end_time": 86400, "results": [
        {"object": "organization.costs.result", "amount": {"value": value, "currency": "usd"},
         "project_id": project_id, "project_name": name} for project_id, name, value in results
    ]}


def transport(pages, requests=None, status=200):
    pages = list(pages)

    def handle(request):
        if requests is not None:
            requests.append(request)
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": "no"}})
        return httpx2.Response(200, json=pages.pop(0))

    return httpx2.MockTransport(handle)


class CostsTests(unittest.TestCase):
    def test_costs_follow_pages_with_the_key_and_project(self):
        requests = []
        pages = [{"data": [bucket(("proj_a", "Cora", 1.5))], "has_more": True, "next_page": "page_2"},
                 {"data": [bucket(("proj_a", "Cora", 2.0))], "has_more": False, "next_page": None}]
        found = openai_spend.costs("sk-admin-key-1234", NOW, project_id="proj_a",
                                   transport=transport(pages, requests))
        self.assertEqual(len(found), 2)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer sk-admin-key-1234")
        self.assertEqual(requests[0].url.params["bucket_width"], "1d")
        self.assertEqual(requests[0].url.params["project_ids"], "proj_a")
        self.assertEqual(requests[0].url.params["start_time"], str(int(NOW.timestamp())))
        self.assertEqual(requests[1].url.params["page"], "page_2")

    def test_all_projects_isnt_a_filter(self):
        requests = []
        openai_spend.costs("sk-admin-k", NOW, project_id=ALL_PROJECTS,
                           transport=transport([{"data": [], "has_more": False}], requests))
        self.assertNotIn("project_ids", requests[0].url.params)

    def test_refusals_say_why_without_the_key(self):
        for status, expected in ((401, "Admin key"), (403, "Usage API: Read"), (500, "500")):
            with self.subTest(status=status), self.assertRaises(AdminKeyError) as caught:
                openai_spend.costs("sk-admin-secret-9999", NOW, transport=transport([], status=status))
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("9999", str(caught.exception))

    def test_projects_with_spend_are_named_and_cora_is_found_by_name(self):
        pages = [{"data": [bucket(("proj_b", "Website", 3.0), ("proj_a", "Cora", 1.0)),
                           bucket(("proj_a", "Cora", 2.0), (None, None, 0.5))], "has_more": False}]
        projects = openai_spend.projects_with_spend("sk-admin-k", now=NOW, transport=transport(pages))
        self.assertEqual(projects, [("proj_a", "Cora"), ("proj_b", "Website")])
        self.assertEqual(openai_spend.likely_cora_project(projects), "proj_a")
        self.assertIsNone(openai_spend.likely_cora_project([("proj_a", "Cora"), ("proj_c", "Cora test")]))
        self.assertEqual(openai_spend.shown_project(None, projects), "proj_a")
        self.assertEqual(openai_spend.shown_project(None, [("proj_b", "Website")]), ALL_PROJECTS)
        self.assertEqual(openai_spend.shown_project("proj_b", projects), "proj_b")

    def test_a_blank_or_spaced_key_is_refused_before_asking(self):
        for key in (" ", "sk-admin with space"):
            with self.subTest(key=key), self.assertRaises(AdminKeyError):
                openai_spend.check_admin_key(key, transport=transport([]))


class StoreTests(unittest.TestCase):
    def test_automatic_is_no_row_and_a_choice_is_saved(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_set") as saved:
            store.set_spend_project("proj_a")
            saved.assert_called_once_with("spend_project", "proj_a")
            for bad in (" ", "has space"):
                with self.assertRaises(ValueError):
                    store.set_spend_project(bad)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def open_general(self, app, pilot):
        app.persistence.load_models.return_value = MODELS
        await pilot.click("#settings-button")
        await asyncio.wait_for(app.workers.wait_for_complete(), 3)
        await pilot.pause()
        self.assertIsInstance(app.screen, ModelSettingsScreen)
        app.screen.query_one(TabbedContent).active = "settings-general"
        await pilot.pause()

    async def test_saving_an_admin_key_checks_it_and_offers_its_projects(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_general(app, pilot)
            projects = [("proj_a", "Cora"), ("proj_b", "Website")]
            with patch.object(openai_spend, "check_admin_key", return_value=projects) as check, \
                    patch.object(openai_spend, "save_admin_key") as save:
                app.screen.query_one("#admin-key", Input).value = "sk-admin-new-4321"
                app.screen.query_one("#admin-key-save", Button).press()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
            check.assert_called_once_with("sk-admin-new-4321")
            save.assert_called_once_with("sk-admin-new-4321")
            status = str(app.screen.query_one("#admin-key-status", Static).render())
            self.assertIn("(…4321)", status)
            self.assertNotIn("sk-admin-new", status)
            select = app.screen.query_one("#spend-project", Select)
            labels = dict((value, label) for label, value in select._options)
            self.assertIn("'Cora'", str(labels["auto"]))
            self.assertIn("proj_b", labels)

            select.value = "proj_b"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_spend_project.assert_called_once_with("proj_b")
            app.persistence.save_spend_project.reset_mock()
            select.value = "auto"
            await pilot.pause()
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_spend_project.assert_called_once_with(None)

    async def test_a_refused_admin_key_isnt_saved(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_general(app, pilot)
            with patch.object(openai_spend, "check_admin_key",
                              side_effect=AdminKeyError("That Admin key can't read costs: give it Usage API: Read.")), \
                    patch.object(openai_spend, "save_admin_key") as save:
                app.screen.query_one("#admin-key", Input).value = "sk-admin-weak"
                app.screen.query_one("#admin-key-save", Button).press()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
            save.assert_not_called()
            self.assertIn("Usage API: Read", str(app.screen.query_one("#admin-key-status", Static).render()))

    async def test_removing_the_admin_key_forgets_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await self.open_general(app, pilot)
            with patch.object(openai_spend, "forget_admin_key") as forget:
                app.screen.query_one("#admin-key-forget", Button).press()
                await asyncio.wait_for(app.workers.wait_for_complete(), 3)
                await pilot.pause()
            forget.assert_called_once()
            self.assertIn("No Admin key", str(app.screen.query_one("#admin-key-status", Static).render()))


if __name__ == "__main__":
    unittest.main()
