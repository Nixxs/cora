"""Offline UI timings. No OpenAI connection, microphone, or speaker is started."""

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from textual import events
from textual.widgets import Input

from app import CoraApp
from chat_widgets import ChatMarkdown


class TimingApp(CoraApp):
    CSS_PATH = str(ROOT / "cora.tcss")

    def connect_to_cora(self):
        # Stub the actual startup method: overriding on_mount wouldn't suppress
        # Textual's base-class on_mount, which starts the real network worker.
        return None

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if event.widget.id == "chat-input" and self.focus_result is not None:
            handled = perf_counter()
            result, self.focus_result = self.focus_result, None
            self.call_after_refresh(lambda: result.set_result((handled, perf_counter())))


def summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }


async def measure(blocks, streaming, samples):
    app = TimingApp()
    app.focus_result = None
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        for i in range(blocks):
            app.log_entries.extend([
                f"You: Show example {i}", "Cora · on screen:",
                ChatMarkdown("```python\ndef even_numbers(values):\n"
                             "    return [n for n in values if n % 2 == 0]\n```"), "",
            ])
        app.redraw_log()
        await pilot.pause()

        focus, refresh, drift, redraw, transcript_updates = [], [], [], [], []
        loop = asyncio.get_running_loop()

        async def heartbeat():
            while True:
                due = perf_counter() + 0.01
                await asyncio.sleep(0.01)
                drift.append(max(0, (perf_counter() - due) * 1000))

        async def transcript():
            while True:
                await asyncio.sleep(0.05)
                start = perf_counter()
                app.append_transcript("Cora", " fragment")
                transcript_updates.append((perf_counter() - start) * 1000)

        tasks = [asyncio.create_task(heartbeat())]
        if streaming:
            tasks.append(asyncio.create_task(transcript()))
        try:
            # One warmup click followed by measured clicks. Pilot's click helper
            # adds pauses; post raw events instead so those aren't timed as lag.
            for index in range(samples + 1):
                app.set_focus(None)
                await pilot.pause()
                field = app.query_one("#chat-input", Input)
                x, y = field.content_region.x + 1, field.content_region.y
                app.focus_result = loop.create_future()
                result = app.focus_result
                args = dict(widget=None, x=x, y=y, delta_x=0, delta_y=0,
                            button=1, shift=False, meta=False, ctrl=False,
                            screen_x=x, screen_y=y)
                start = perf_counter()
                app.post_message(events.MouseDown(**args))
                handled, painted = await asyncio.wait_for(result, timeout=5)
                app.post_message(events.MouseUp(**args))
                if not field.has_focus:
                    raise AssertionError("Injected mouse event did not focus the input")
                if index:
                    focus.append((handled - start) * 1000)
                    refresh.append((painted - start) * 1000)
            # Don't count our deliberate history rebuilds as *idle* loop lag.
            click_drift = drift.copy()
            # A separate, synchronous rebuild metric: excludes layout/paint.
            for _ in range(samples):
                start = perf_counter()
                app.redraw_log()
                redraw.append((perf_counter() - start) * 1000)
                await pilot.pause()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            app.flush_transcript()
        result = {
            "code_blocks": blocks,
            "synthetic_transcripts": streaming,
            "mouse_to_focus": summary(focus),
            "mouse_to_headless_refresh": summary(refresh),
            "event_loop_drift": summary(click_drift),
            "history_redraw": summary(redraw),
        }
        if transcript_updates:
            result["transcript_update"] = summary(transcript_updates)
        return result


async def run(samples):
    scenarios = {}
    for name, blocks, streaming in (
        ("idle_empty", 0, False),
        ("idle_10_blocks", 10, False),
        ("idle_30_blocks", 30, False),
        ("streaming_30_blocks", 30, True),
    ):
        print(f"Measuring {name}...", flush=True)
        scenarios[name] = await measure(blocks, streaming, samples)
    return {
        "schema": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "textual": version("textual"),
        "rich": version("rich"),
        "screen_size": [110, 40],
        "samples_per_scenario": samples,
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("app.py", "chat_widgets.py", "cora.tcss")},
        "scope": "Headless; no API/audio/terminal IO. Refresh means Textual's "
                 "refresh callback, not a visible terminal frame. Synthetic "
                 "transcripts target one delta every 50ms; slow rendering reduces that rate.",
        "scenarios": scenarios,
    }


def report(result, previous=None):
    print("\nMilliseconds: median / p95 / maximum (lower is better)")
    for name, scenario in result["scenarios"].items():
        print(f"\n{name}")
        for metric in ("mouse_to_focus", "mouse_to_headless_refresh", "event_loop_drift", "history_redraw"):
            values = scenario[metric]
            comparison = ""
            if previous:
                old = previous["scenarios"][name][metric]["p50_ms"]
                if old:
                    comparison = f"  median change: {(values['p50_ms'] / old - 1) * 100:+.1f}%"
            print(f"  {metric:27} {values['p50_ms']:8.3f} / "
                  f"{values['p95_ms']:8.3f} / {values['max_ms']:8.3f}{comparison}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, help="Save metrics as JSON")
    parser.add_argument("--compare", type=Path, help="Compare with a previous JSON run")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    previous = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
    if previous and (previous.get("schema") != 1 or previous["samples_per_scenario"] != args.samples):
        parser.error("Compare runs with the same schema and sample count")
    result = asyncio.run(run(args.samples))
    report(result, previous)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"\nSaved metrics: {args.output}")


if __name__ == "__main__":
    main()