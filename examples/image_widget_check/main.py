"""Show a screenshot in a Textual app to check terminal image support.

Run it in each terminal (Windows Terminal, VS Code terminal, Kitty) and
note which method it reports and whether the image looks sharp.
"""

from pathlib import Path

# Importing the widget queries the terminal, so it must happen before the app starts.
from textual_image.renderable import Image as AutoRenderable
from textual_image.widget import Image

from textual.app import App, ComposeResult
from textual.widgets import Footer, Static

IMAGE = Path(__file__).resolve().parents[1] / "image_input_check" / "Screenshot.png"
METHOD = AutoRenderable.__module__.rsplit(".", 1)[-1]


class ImageCheckApp(App[None]):
    BINDINGS = [("q", "quit", "Quit")]
    CSS = "Image { width: auto; height: auto; }"

    def compose(self) -> ComposeResult:
        yield Static(f"Rendering method: {METHOD}")
        yield Image(IMAGE)
        yield Footer()


if __name__ == "__main__":
    ImageCheckApp().run()
