"""The dashboard's boxes minimised to chips on its bottom border, the rows on shared columns, and
remembered."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from storage.app_settings_store import AppSettingsStore
from ui.dashboard import MINIMISABLE, DashboardScreen, column_spans, column_widths, shared_widths
import test_keyboard_persistence as keyboard_tests


async def open_dashboard(app, pilot):
    await pilot.click("#view-button")
    await pilot.pause()
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


class MinimiseTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicking_a_title_minimises_it_to_a_chip_and_the_chip_opens_it(self):
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await open_dashboard(app, pilot)
            await pilot.click("#feelings-box", offset=(3, 0))
            await pilot.pause()
            self.assertFalse(screen.query_one("#feelings-box").display)
            chip = screen.query_one("#chip-feelings-box")
            self.assertEqual(str(chip.render()), " ▸ feelings ")
            # On the bottom border, beside its corner.
            tray = screen.query_one("#dashboard-tray")
            self.assertEqual(chip.region.y, tray.region.y)
            self.assertEqual(chip.region.right + 2, tray.region.right)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_dashboard_minimised.assert_called_with(["feelings-box"])
            self.assertEqual(app.dashboard_minimised, ["feelings-box"])
            await pilot.click("#chip-feelings-box")
            await pilot.pause()
            self.assertTrue(screen.query_one("#feelings-box").display)
            self.assertEqual(len(screen.query(".box-chip")), 0)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_dashboard_minimised.assert_called_with([])

    async def test_a_click_inside_a_box_or_on_the_top_row_does_nothing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await open_dashboard(app, pilot)
            await pilot.click("#feelings-box", offset=(3, 2))
            await pilot.click("#chat-box", offset=(3, 0))
            await pilot.pause()
            self.assertEqual(screen.minimised, set())
            self.assertTrue(screen.query_one("#chat-box").display)

    async def test_the_rows_line_up_on_shared_columns_and_the_chips_go_in_the_rows_order(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
            screen = await open_dashboard(app, pilot)
            for box in ("tasks-box", "background-box", "interests-box"):
                screen.minimise_box(box)
            await pilot.pause()
            await pilot.pause()
            # Four open in the middle row: the spare column to identity. Three in the bottom (home
            # isn't set up): the two spare to conversations, then context.
            middle = [screen.query_one(f"#{box}").region for box in ("feelings-box", "memories-box",
                                                                     "identity-box", "desires-box")]
            bottom = [screen.query_one(f"#{box}").region for box in ("relationship-box", "conversations-box",
                                                                     "context-box")]
            row = screen.query_one("#dashboard-middle").region
            column_edges = {row.x + round(row.width * index / 5) for index in range(1, 6)}
            for regions in (middle, bottom):
                self.assertLessEqual({region.right for region in regions}, column_edges)
                self.assertEqual(regions[-1].right, row.right)
            # One and two columns (give or take the rounding of their edges).
            for region, columns in zip(bottom, (1, 2, 2)):
                self.assertAlmostEqual(region.width, row.width * columns / 5, delta=1)
            self.assertEqual([chip.box_id for chip in screen.query(".box-chip")],
                             ["interests-box", "tasks-box", "background-box"])
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            # Saved in the rows' order.
            app.persistence.save_dashboard_minimised.assert_called_with(
                ["interests-box", "tasks-box", "background-box"])

    async def test_minimising_resizes_the_row_before_it_is_drawn_again(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
            screen = await open_dashboard(app, pilot)
            await pilot.pause()
            before = screen.query_one("#interests-box").styles.width.cells
            screen.minimise_box("memories-box")
            # Set at once, not after a layout: the row doesn't draw its old widths first. The spare
            # column goes to interests.
            self.assertAlmostEqual(screen.query_one("#interests-box").styles.width.cells, 2 * before, delta=1)

    async def test_with_more_boxes_than_columns_the_row_is_shared(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
            app.sensibo_ready = lambda: True
            app.home_shown = []
            screen = await open_dashboard(app, pilot)
            await pilot.pause()
            await pilot.pause()
            row = screen.query_one("#dashboard-bottom").region
            boxes = [screen.query_one(f"#{box}").region for box in MINIMISABLE["dashboard-bottom"]]
            self.assertEqual(boxes[-1].right, row.right)
            # The background box keeps to its list; the others share the rest.
            self.assertLess(boxes[-1].width, boxes[0].width)
            widths = [box.width for box in boxes[:-1]]
            self.assertLessEqual(max(widths) - min(widths), 1)

    async def test_it_opens_as_it_was_left(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.dashboard_minimised = ["tasks-box", "not-a-box"]
            screen = await open_dashboard(app, pilot)
            self.assertEqual(screen.minimised, {"tasks-box"})
            self.assertFalse(screen.query_one("#tasks-box").display)
            self.assertEqual([chip.box_id for chip in screen.query(".box-chip")], ["tasks-box"])

    def test_every_minimisable_box_has_a_title(self):
        from ui.dashboard import BOX_TITLES
        for boxes in MINIMISABLE.values():
            for box in boxes:
                self.assertIn(box, BOX_TITLES)


class ColumnTests(unittest.TestCase):
    def test_spare_columns_go_to_the_widened_first_and_too_many_boxes_share(self):
        self.assertEqual(column_spans(["a", "b", "c", "d"], ("c", "a")), [1, 1, 2, 1])
        self.assertEqual(column_spans(["a", "b"], ("b", "a")), [2, 3])
        self.assertEqual(column_spans(["a"], ()), [5])
        self.assertIsNone(column_spans(list("abcdef"), ()))
        self.assertIsNone(column_spans([], ()))

    def test_widths_sit_on_the_same_edges_in_every_row(self):
        self.assertEqual(column_widths(222, [1, 1, 1, 1, 1]), [44, 45, 44, 45, 44])
        self.assertEqual(column_widths(222, [1, 2, 1, 1]), [44, 89, 45, 44])
        self.assertEqual(sum(column_widths(101, [3, 2])), 101)
        self.assertEqual(shared_widths(100, {"x": 18}, ["a", "b", "c"]), {"x": 18, "a": 28, "b": 27, "c": 27})


class StoreTests(unittest.TestCase):
    def test_minimised_boxes_are_saved_by_id_and_cleared_when_none(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_get", return_value="feelings-box,tasks-box,Bad Box"):
            self.assertEqual(store.dashboard_minimised(), ["feelings-box", "tasks-box"])
        with patch.object(store, "_get", return_value=None):
            self.assertEqual(store.dashboard_minimised(), [])
        with patch.object(store, "_set") as saved:
            store.set_dashboard_minimised(["feelings-box", "tasks-box"])
            saved.assert_called_once_with("dashboard_minimised", "feelings-box,tasks-box")
            with self.assertRaises(ValueError):
                store.set_dashboard_minimised(["../x"])
        with patch.object(store, "_set") as saved, patch("storage.app_settings_store.Session") as session:
            store.set_dashboard_minimised([])
            saved.assert_not_called()
            session.return_value.__enter__.return_value.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
