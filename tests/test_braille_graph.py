"""Braille graphs draw two values per cell as filled columns, newest on the right."""

import unittest
from unittest.mock import patch

from textual.app import App, ComposeResult

from ui.braille import GROW_SECONDS, PULSE_SECONDS, WHITE, BrailleGraph, braille_bars, braille_graph


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

    def test_bars_spread_across_the_width_with_gaps(self):
        # Four dot columns: full bar, gap, half bar, gap.
        self.assertEqual(braille_bars([1.0, 0.5], 2, 1), ["\u2847\u2844"])
        self.assertEqual(braille_bars([], 2, 1), [BLANK * 2])

    def test_narrow_bars_drop_the_gaps(self):
        self.assertEqual(braille_bars([1.0, 1.0, 1.0], 1, 1), [FULL])


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

    async def test_a_pulsing_graph_brightens_and_fades_its_newest_cell(self):
        class GraphApp(App):
            def compose(self) -> ComposeResult:
                yield BrailleGraph(id="graph", pulse=True)

        app = GraphApp()
        async with app.run_test(size=(4, 2)) as pilot:
            graph = app.query_one(BrailleGraph)
            self.assertIsInstance(graph.render(), str)  # nothing to pulse yet
            graph.values = (1.0,) * 8
            await pilot.pause()
            base = graph.styles.color
            with patch("ui.braille.monotonic", return_value=0.0):
                faded = graph.render()
            with patch("ui.braille.monotonic", return_value=PULSE_SECONDS / 2):
                bright = graph.render()
            # Only the last cell of each row is styled.
            self.assertEqual([(span.start, span.end) for span in bright.spans], [(3, 4), (8, 9)])
            self.assertEqual(faded.spans[0].style.color, base.rich_color)
            self.assertEqual(bright.spans[0].style.color, WHITE.rich_color)

    async def test_a_shaded_graph_dims_older_cells_by_age(self):
        class GraphApp(App):
            def compose(self) -> ComposeResult:
                yield BrailleGraph(id="graph", shade=lambda age: 1.0 if age == 0 else 0.5)

        app = GraphApp()
        async with app.run_test(size=(4, 2)) as pilot:
            graph = app.query_one(BrailleGraph)
            graph.values = (1.0,) * 8
            await pilot.pause()
            text = graph.render()
            colors = {span.start: span.style.color for span in text.spans}
            dim = graph.styles.color.blend(graph.background_colors[1], 0.5).rich_color
            # Each row: three older cells dimmed, the newest at full colour.
            for start in (0, 5):
                self.assertEqual([colors[start + column] for column in range(4)],
                                 [dim] * 3 + [graph.styles.color.rich_color])

    async def test_a_growing_graph_rises_to_its_first_values(self):
        class GraphApp(App):
            def compose(self) -> ComposeResult:
                yield BrailleGraph(id="graph", grow=True)

        app = GraphApp()
        async with app.run_test(size=(4, 1)):
            graph = app.query_one(BrailleGraph)
            with patch("ui.braille.monotonic", return_value=100.0):
                graph.values = (1.0,) * 8
                # The first frame drawn starts the clock.
                self.assertEqual(graph.render(), BLANK * 4)
            with patch("ui.braille.monotonic", return_value=100.0 + GROW_SECONDS / 2):
                self.assertNotIn(graph.render(), (BLANK * 4, FULL * 4))
            with patch("ui.braille.monotonic", return_value=100.0 + GROW_SECONDS):
                self.assertEqual(graph.render(), FULL * 4)
            self.assertIsNone(graph.grow_timer)
            # Later values are drawn straight away rather than growing again.
            with patch("ui.braille.monotonic", return_value=200.0):
                graph.values = (0.5,) * 8
                self.assertEqual(graph.render(), "\u28e4" * 4)


if __name__ == "__main__":
    unittest.main()
