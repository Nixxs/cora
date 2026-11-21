"""The dashboard's boxes: one list flowing through one row or two, minimised to chips on its bottom
border, opened again last, and remembered."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from storage.app_settings_store import AppSettingsStore
from ui.dashboard import BOXES, FIRST_ROW_HEIGHT, MIN_COLUMN, DashboardScreen, arrange
import test_keyboard_persistence as keyboard_tests

# Every box but home, which isn't set up in these tests.
SHOWN = [box for box in BOXES if box != "home-box"]


async def open_dashboard(app, pilot, opened=None):
    if opened is not None:
        app.dashboard_boxes = (opened, [box for box in BOXES if box not in opened])
    await pilot.click("#view-button")
    await pilot.pause()
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


def rows_of(screen, boxes):
    """The boxes' regions, grouped by the row they're in, top first and left to right."""
    regions = [screen.query_one(f"#{box}").region for box in boxes]
    rows = {}
    for box, region in zip(boxes, regions):
        rows.setdefault(region.y, []).append((box, region))
    return [sorted(rows[y], key=lambda item: item[1].x) for y in sorted(rows)]


class ArrangeTests(unittest.TestCase):
    def test_one_tall_row_until_four_then_two_with_the_odd_one_first(self):
        self.assertEqual(arrange([]), (0, 0, []))
        self.assertEqual(arrange(["a"]), (1, 1, [1]))
        self.assertEqual(arrange(["a", "b", "c"]), (3, 1, [1, 1, 1]))
        self.assertEqual(arrange(list("abcd")), (2, 2, [1, 1, 1, 1]))
        self.assertEqual(arrange(list("abcdef")), (3, 2, [1] * 6))

    def test_a_shorter_second_row_widens_the_box_with_most_to_show(self):
        boxes = ["feelings-box", "memories-box", "interests-box", "tasks-box", "conversations-box"]
        self.assertEqual(arrange(boxes), (3, 2, [1, 1, 1, 1, 2]))
        # Neither ranked: the first of them.
        self.assertEqual(arrange(list("abcde")), (3, 2, [1, 1, 1, 2, 1]))
        self.assertEqual(arrange(list(BOXES))[2][6], 2)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicking_a_title_minimises_it_to_a_chip_and_the_chip_opens_it_last(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
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
            app.persistence.save_dashboard_boxes.assert_called_with(
                [box for box in BOXES if box != "feelings-box"], ["feelings-box"])
            self.assertEqual(app.dashboard_boxes[1], ["feelings-box"])
            await pilot.click("#chip-feelings-box")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(len(screen.query(".box-chip")), 0)
            # Back as the last box: bottom right.
            last = rows_of(screen, SHOWN)[-1][-1]
            self.assertEqual(last[0], "feelings-box")
            self.assertEqual(last[1].right, screen.query_one("#dashboard-boxes").region.right)
            await asyncio.wait_for(app.workers.wait_for_complete(), 3)
            app.persistence.save_dashboard_boxes.assert_called_with(
                [box for box in BOXES if box != "feelings-box"] + ["feelings-box"], [])

    async def test_a_click_inside_a_box_or_on_the_top_row_does_nothing(self):
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await open_dashboard(app, pilot)
            await pilot.click("#feelings-box", offset=(3, 2))
            await pilot.click("#chat-box", offset=(3, 0))
            await pilot.pause()
            self.assertEqual(screen.minimised, set())
            self.assertTrue(screen.query_one("#chat-box").display)

    async def test_up_to_three_share_one_tall_row(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
            screen = await open_dashboard(app, pilot, ["tasks-box", "feelings-box"])
            await pilot.pause()
            rows = rows_of(screen, ["tasks-box", "feelings-box"])
            self.assertEqual(len(rows), 1)
            (_, tasks), (_, feelings) = rows[0]
            area = screen.query_one("#dashboard-boxes").region
            # In their order, side by side, filling the width and both rows' height.
            self.assertLess(tasks.x, feelings.x)
            self.assertEqual((tasks.x, feelings.right), (area.x, area.right))
            self.assertLessEqual(abs(tasks.width - feelings.width), 1)
            self.assertEqual(tasks.height, area.height)

    async def test_from_four_they_split_into_two_rows_with_the_columns_lined_up(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
            opened = ["feelings-box", "memories-box", "interests-box", "relationship-box", "conversations-box"]
            screen = await open_dashboard(app, pilot, opened)
            await pilot.pause()
            first, second = rows_of(screen, opened)
            self.assertEqual([box for box, _ in first], opened[:3])
            self.assertEqual({region.height for _, region in first}, {FIRST_ROW_HEIGHT})
            # Every edge of the second row meets one in the first, and both reach the last column.
            self.assertLessEqual({region.right for _, region in second}, {region.right for _, region in first})
            self.assertEqual(second[-1][1].right, first[-1][1].right)
            # The spare column to conversations.
            self.assertEqual(second[1][1].x, first[1][1].x)

    async def test_minimising_moves_the_boxes_before_they_are_drawn_again(self):
        async with keyboard_tests.running_app() as (app, pilot):
            await pilot.resize_terminal(228, 50)
            screen = await open_dashboard(app, pilot, ["feelings-box", "memories-box", "interests-box", "tasks-box"])
            await pilot.pause()
            grid = screen.query_one("#dashboard-boxes")
            self.assertEqual((grid.styles.grid_size_columns, grid.styles.grid_size_rows), (2, 2))
            screen.minimise_box("memories-box")
            # Set at once, not after a layout: no step drawn in between.
            self.assertEqual((grid.styles.grid_size_columns, grid.styles.grid_size_rows), (3, 1))
            self.assertFalse(screen.query_one("#memories-box").display)

    async def test_too_narrow_pans_sideways_rather_than_squashing_them(self):
        async with keyboard_tests.running_app() as (app, pilot):
            screen = await open_dashboard(app, pilot)
            await pilot.pause()
            scroll = screen.query_one("#dashboard-scroll")
            # 80 columns can't hold five 34-wide columns.
            self.assertEqual(screen.query_one("#feelings-box").outer_size.width, MIN_COLUMN)
            self.assertGreater(scroll.max_scroll_x, 0)
            self.assertFalse(scroll.can_focus)

    async def test_it_opens_as_it_was_left(self):
        async with keyboard_tests.running_app() as (app, pilot):
            app.dashboard_boxes = (["tasks-box", "not-a-box", "feelings-box"], ["context-box", "nor-this"])
            screen = await open_dashboard(app, pilot)
            self.assertEqual(screen.minimised, {"context-box"})
            # The saved order first, then the boxes it didn't name in their first order.
            self.assertEqual(screen.order[:3], ["tasks-box", "feelings-box", "memories-box"])
            self.assertNotIn("context-box", screen.order)
            self.assertFalse(screen.query_one("#context-box").display)
            self.assertEqual([chip.box_id for chip in screen.query(".box-chip")], ["context-box"])


class StoreTests(unittest.TestCase):
    def test_the_open_boxes_in_order_and_the_minimised_are_saved_together(self):
        store = AppSettingsStore(MagicMock())
        with patch.object(store, "_get", return_value="tasks-box,feelings-box,Bad Box|context-box"):
            self.assertEqual(store.dashboard_boxes(), (["tasks-box", "feelings-box"], ["context-box"]))
        # As the migration carried a list of minimised boxes over.
        with patch.object(store, "_get", return_value="|tasks-box,background-box"):
            self.assertEqual(store.dashboard_boxes(), ([], ["tasks-box", "background-box"]))
        with patch.object(store, "_get", return_value=None):
            self.assertEqual(store.dashboard_boxes(), ([], []))
        with patch.object(store, "_set") as saved:
            store.set_dashboard_boxes(["tasks-box", "feelings-box"], [])
            saved.assert_called_once_with("dashboard_boxes", "tasks-box,feelings-box|")
            with self.assertRaises(ValueError):
                store.set_dashboard_boxes(["../x"], [])
        with patch.object(store, "_set") as saved, patch("storage.app_settings_store.Session") as session:
            store.set_dashboard_boxes([], [])
            saved.assert_not_called()
            session.return_value.__enter__.return_value.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
