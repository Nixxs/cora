"""Braille graphs draw two values per cell as filled columns, newest on the right."""

import unittest

from textual.app import App, ComposeResult

from ui.braille import BrailleGraph, braille_graph


BLANK, FULL = "\u2800", "\u28ff"


class BrailleGraphTests(unittest.TestCase):
    def test_fills_from_the_bottom_two_values_per_cell(self):
        self.assertEqual(braille_graph([1.0, 1.0], 1, 1), [FULL])
        self.assertEqual(braille_graph([0.0, 0.0], 1, 1), [BLANK])
        self.assertEqual(braille_graph([0.5, 0.5], 1, 1), ["\u28e4"])  # bottom two dot rows
        self.assertEqual(braille_graph([1.0, 0.0], 1, 1), ["\u2847"])  # left column only

    def test_spans_rows(self):
        self.assertEqual(braille_graph([1.0, 1.0], 1, 2), [FULL, FULL])
        self.assertEqual(braille_graph([0.5, 0.5], 1, 2), [BLANK, FULL])

    def test_downward_hangs_from_the_top(self):
        self.assertEqual(braille_graph([0.5, 0.5], 1, 1, downward=True), ["\u281b"])  # top two dot rows
        self.assertEqual(braille_graph([0.5, 0.5], 1, 2, downward=True), [FULL, BLANK])

    def test_keeps_the_newest_values_on_the_right(self):
        self.assertEqual(braille_graph([1.0], 2, 1), [BLANK + "\u28b8"])
        self.assertEqual(braille_graph([1.0, 1.0, 0.0, 0.0], 1, 1), [BLANK])

    def test_scales_and_clamps_to_the_maximum(self):
        self.assertEqual(braille_graph([50, 50], 1, 1, maximum=100), ["\u28e4"])
        self.assertEqual(braille_graph([5.0, -1.0], 1, 1), ["\u2847"])
        self.assertEqual(braille_graph([1.0], 1, 1, maximum=0), [BLANK])

    def test_empty_size_draws_nothing(self):
        self.assertEqual(braille_graph([1.0], 0, 3), [])
        self.assertEqual(braille_graph([1.0], 3, 0), [])


class BrailleGraphWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_redraws_when_values_change(self):
        class GraphApp(App):
            def compose(self) -> ComposeResult:
                yield BrailleGraph(id="graph")

        app = GraphApp()
        async with app.run_test(size=(4, 2)) as pilot:
            graph = app.query_one(BrailleGraph)
            self.assertEqual(graph.render_line(1).text, BLANK * 4)
            graph.values = (1.0,) * 8
            await pilot.pause()
            self.assertEqual([graph.render_line(row).text for row in range(2)], [FULL * 4] * 2)


if __name__ == "__main__":
    unittest.main()
