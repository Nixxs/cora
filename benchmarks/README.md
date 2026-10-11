# UI responsiveness baseline

[../tools/benchmark_ui.py](../tools/benchmark_ui.py) measures the existing app
without connecting to OpenAI or opening audio devices. It does not modify the
production chat or styling.

Run from the project root using the project's virtual environment:

```powershell
python tools/benchmark_ui.py --output benchmarks/ui-current.json --compare benchmarks/ui-baseline.json
```

[ui-baseline.json](ui-baseline.json) stores the initial measured run. Keep it
unchanged when testing improvements. The comparison prints the percentage change
in median time: negative is faster, positive is slower.

## Scenarios

- Idle, empty history.
- Idle, 10 code blocks.
- Idle, 30 code blocks.
- 30 code blocks with synthetic transcript updates (one update every 50 ms when
  the event loop can keep up). This is not a real audio/API simulation.

Each scenario uses a fixed 110-by-40 terminal size, one warmup click, and 20
measured clicks. Every click starts with the input unfocused and verifies that
the injected mouse event really focuses it. The metrics report median, 95th
percentile, and maximum time in milliseconds.

## Metrics and limits

- **mouse_to_focus:** posting a raw mouse event through the app to its input-focus
  notification. No test-helper pauses are included in the timed interval.
- **mouse_to_headless_refresh:** posting that event to Textual's next
  after-refresh callback. This is **not** a measurement of a visible terminal
  frame reaching the monitor.
- **event_loop_drift:** how late a 10 ms heartbeat wakes during click trials.
  The separate, deliberately triggered redraw trials are excluded.
- **history_redraw:** time spent synchronously rebuilding the log, excluding
  subsequent layout and refresh. Synthetic transcript updates have their own
  `transcript_update` timings in the saved JSON.

Results include Python/Textual/Rich versions, platform, sample count, terminal
size, and hashes of the app/widget/style sources. Use the same machine, settings,
sample count, and benchmark implementation for comparisons. Repeat runs; small
differences and individual peaks can be scheduler noise. There are intentionally
no machine-dependent pass/fail thresholds.

The baseline did not reproduce a 500 ms idle delay. A headless test excludes
VS Code terminal input/output, real microphone capture, speaker callbacks, and
network activity. Fast headless clicks do **not** prove the live application is
responsive. Persistent live lag requires a separate live-session measurement.