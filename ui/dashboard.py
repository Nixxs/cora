from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalGroup, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from context_window import BACKEND, VOICE, short_tokens
from devices.audio_bands import BAND_COUNT
from memory.store import fade_weight
from session_config import BACKEND_CONTEXT_TOKENS
from storage.task_store import TASK_NUDGES_MAX
from subconscious.emotions.feeling_rules import FEELING_WORDS, FEELINGS, band
from subconscious.identity.identity_rules import is_held
from ui.braille import BrailleGraph
from ui.chat_widgets import BorderLine, SelectableRichLog, TitleBar
from ui.composer import ChatComposer
from ui.context_view import ContextScreen
from ui.meters import LABEL_STYLE, TRACK_STYLE, CalendarHeatmap, ContextMeters, Meters, SpendChart, ToneRibbon


# A bar jumps straight up to a louder sound, then falls this much per tick (20 a second).
FALL_PER_TICK = 0.08

# How often the open dashboard re-reads the database.
REFRESH_SECONDS = 30
# OpenAI's costs move slowly (a few hours behind), so the spend chart asks far less often.
SPEND_REFRESH_SECONDS = 30 * 60

RELATIONSHIP_VALUES = ("trust", "warmth", "familiarity")

# A task's dot: blue while it waits on others, amber once it waits on the person.
TASK_STATUS_STYLES = {"open": "#7fb8d8", "waiting": "#7fb8d8", "needs_person": "#e0b45c"}
# A sign-up's dot, and how its state reads.
SIGN_UP_STYLES = {"done": "#8fc49a", "signing_up": "#7fb8d8", "confirm_email": "#7fb8d8", "blocked": "#e0b45c",
                  "failed": "#d27a7a"}
SIGN_UP_STATES = {"done": "signed up", "signing_up": "signing up", "confirm_email": "confirm email",
                  "blocked": "blocked", "failed": "failed"}
# A desire's strength: bright once she's working toward it, softer while held, dim while forming.
DESIRE_STYLES = {"pursuing": "#e0b45c", "held": "#c9a86a"}
DESIRE_STATES = {"pursuing": "pursuing", "held": "held", "candidate": "forming"}
# Each feeling's colour: warm for the good ones, red-orange for irritation and hurt.
FEELING_STYLES = {"excitement": "#e0b45c", "contentment": "#8fcca6", "curiosity": "#7fb8d8",
                  "fulfilment": "#c9a86a", "irritation": "#e0905c", "hurt": "#d27a7a"}
# The top of each feelings graph: one strong reaction (0.3) fills most of its row, and more is clipped.
FEELING_GRAPH_MAX = 0.4
# A task's latest step is cut to this, so one long message doesn't fill the box.
STEP_PREVIEW_CHARS = 120


# Box id -> border title, in the btop-style layout: a wide voice band with the chat beside
# it, then the database panels below.
BOX_TITLES = {
    "chat-box": "chat",
    "relationship-box": "relationship",
    "feelings-box": "feelings",
    "memories-box": "memories",
    "interests-box": "interests",
    "identity-box": "identity",
    "desires-box": "desires",
    "conversations-box": "conversations",
    "tasks-box": "tasks",
    "context-box": "context",
    "background-box": "background",
}


def whole_unit(seconds: float) -> str | None:
    """The largest whole unit, like '5m', '3h' or '2d'; None under a minute."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return None


def ago(then, now) -> str:
    """How long ago: 'never', 'just now', '5m ago', '3h ago', '2d ago'."""
    if then is None:
        return "never"
    span = whole_unit((now - then).total_seconds())
    return f"{span} ago" if span else "just now"


def until(when, now) -> str:
    """How long until: 'due' once the time has come, else 'soon', 'in 5m', 'in 3h'."""
    if when is None or when <= now:
        return "due"
    span = whole_unit((when - now).total_seconds())
    return f"in {span}" if span else "soon"


def next_check(when, now) -> str:
    """When the hourly check acts on something: 'in 6h', or 'next check' once due (nudges wait for daytime)."""
    return "next check" if when <= now else until(when, now)


def party_state(party, now) -> tuple[str, str]:
    """What's next for one person on a task, and its style."""
    if party.status == "done":
        return "answered", LABEL_STYLE
    if party.due_at is None:
        # The task waits on the person, so nobody is nudged.
        return "asked", LABEL_STYLE
    if party.nudges >= TASK_NUDGES_MAX:
        return f"no answer · back to you {next_check(party.due_at, now)}", ""
    return f"nudge {party.nudges + 1}/{TASK_NUDGES_MAX} {next_check(party.due_at, now)}", ""


def task_lines(tasks, now) -> Text:
    """Each open task: its goal, where it stands, each person's next check and its latest step."""
    lines = Text()
    for index, task in enumerate(tasks):
        lines.append(("\n\n" if index else "") + "● ", style=TASK_STATUS_STYLES.get(task.status, LABEL_STYLE))
        lines.append(task.goal)
        state = "needs you" if task.status == "needs_person" else "waiting"
        stale = f" · back to you {next_check(task.stale_at, now)}" if task.stale_at else ""
        lines.append(f"\n  {state} · started {ago(task.created_at, now)}{stale}", style=LABEL_STYLE)
        width = max((len(party.name) for party in task.parties), default=0)
        for party in task.parties:
            lines.append(f"\n  {party.name:<{width}}  ", style=LABEL_STYLE)
            lines.append(*party_state(party, now))
        if task.latest_step:
            step = " ".join(task.latest_step.split())
            if len(step) > STEP_PREVIEW_CHARS:
                step = step[:STEP_PREVIEW_CHARS - 1] + "…"
            lines.append(f"\n  ↳ {step}", style=TRACK_STYLE)
    return lines or Text("no open tasks", style=TRACK_STYLE)


def desire_lines(desires) -> Text:
    """Each live desire: its strength and state, then its wish; ones still forming are dim."""
    lines = Text()
    for index, (wish, strength, status) in enumerate(desires):
        style = DESIRE_STYLES.get(status, LABEL_STYLE)
        lines.append(("\n\n" if index else "") + f"{strength:.0%} ", style=style)
        lines.append(DESIRE_STATES[status], style=LABEL_STYLE)
        lines.append("\n" + wish, style="" if status in DESIRE_STYLES else LABEL_STYLE)
    return lines or Text("no desires yet", style=TRACK_STYLE)


def latest_feeling_line(reaction, now) -> Text:
    """Her newest reaction: the feeling, how strong, how long ago, then why."""
    if reaction is None:
        return Text("nothing felt lately", style=TRACK_STYLE)
    return Text.assemble(
        (FEELING_WORDS[reaction.feeling], FEELING_STYLES[reaction.feeling]),
        (f" ({reaction.strength}) · {ago(reaction.at, now)}", LABEL_STYLE), "\n", reaction.reason,
    )


def sign_up_lines(sign_ups, now) -> Text:
    """Her latest sign-ups: the site, how it stands and when she asked."""
    lines = Text()
    for index, sign_up in enumerate(sign_ups):
        lines.append(("\n" if index else "") + "● ", style=SIGN_UP_STYLES.get(sign_up.status, LABEL_STYLE))
        lines.append(sign_up.site)
        state = SIGN_UP_STATES.get(sign_up.status, sign_up.status)
        if sign_up.blocked_by:
            state += f" ({sign_up.blocked_by.replace('_', ' ')})"
        lines.append(f"  {state} · {ago(sign_up.asked_at, now)}", style=LABEL_STYLE)
    return lines


class DashboardScreen(Screen):
    """A read-only view of what Cora has stored, switched to from the chat."""

    def __init__(self, voice_bands, conversation, audio_status, connection, load_snapshot, activity, context,
                 load_spend=None) -> None:
        super().__init__()
        # Reads (Spend or None, message, hint) for the spend chart off the UI thread; None leaves it out.
        self.load_spend = load_spend
        # Returns (Cora's playback bands, the person's microphone bands), each 0.0 to 1.0,
        # or an empty tuple for silence.
        self.voice_bands = voice_bands
        self.conversation = conversation
        # Owns the chat's mic and speaker buttons, which the voice box's toggles press.
        self.audio_status = audio_status
        # The chat's connection button, which enters standby or resumes; the voice box's toggle presses it.
        self.connection = connection
        # Reads a DashboardSnapshot off the UI thread; returns None if the database failed.
        self.load_snapshot = load_snapshot
        # The last good snapshot; a failed refresh keeps showing it.
        self.snapshot = None
        # The chat's activity panel, whose latest summary the voice box shows.
        self.activity = activity
        # What Live's last connection started with; kept in memory, so read every second.
        self.context = context

    def compose(self) -> ComposeResult:
        yield TitleBar("chat")
        with Vertical(id="dashboard-panel"):
            with Horizontal(id="dashboard-top", classes="dashboard-row"):
                with Vertical(id="voice-column"):
                    # The box's top border, drawn as widgets so the standby toggle can sit at its right.
                    with HorizontalGroup(id="voice-header"):
                        yield Static("╭─", classes="voice-line")
                        yield Static("voice", id="voice-title")
                        yield BorderLine(id="voice-header-fill")
                        yield Button("○ disconnected", id="dashboard-standby")
                        yield Static("─╮", classes="voice-line")
                    # Cora's voice rises above the line and the person's hangs below it.
                    with Vertical(id="voice-box", classes="dashboard-box"):
                        yield BrailleGraph(id="speaker-graph", bars=True)
                        yield BorderLine(id="voice-axis")
                        yield BrailleGraph(id="mic-graph", downward=True, bars=True)
                    # The box's bottom border, drawn as widgets so the toggles can sit on the line.
                    with HorizontalGroup(id="voice-controls"):
                        yield Static("╰─", classes="voice-line")
                        yield Static(id="dashboard-activity")
                        yield BorderLine(id="voice-controls-fill")
                        yield Button("🔊 speaker", id="dashboard-speaker")
                        yield Static("─", classes="voice-line")
                        yield Button("🎙 mic", id="dashboard-mic")
                        yield Static("─╯", classes="voice-line")
                with Vertical(id="chat-box", classes="dashboard-box"):
                    yield SelectableRichLog(id="dashboard-chat", wrap=True, min_width=1)
                    yield BorderLine(id="dashboard-input-line")
                    # Posts the same Submitted message as the chat's composer, which the app sends.
                    yield ChatComposer(
                        placeholder="Type a message to Cora...",
                        id="dashboard-input",
                        soft_wrap=True,
                        show_line_numbers=False,
                        highlight_cursor_line=False,
                    )
            # Pans sideways when the boxes don't fit; unfocusable so clicks leave typing alone.
            with HorizontalScroll(id="dashboard-middle", can_focus=False):
                with Vertical(id="feelings-box", classes="dashboard-box"):
                    # One line per feeling over the last few hours; a line pulses while it has just jumped.
                    for feeling in FEELINGS:
                        with Horizontal(classes="feeling-row"):
                            yield Static(FEELING_WORDS[feeling], id=f"{feeling}-name", classes="feeling-name")
                            yield BrailleGraph(id=f"{feeling}-feeling", maximum=FEELING_GRAPH_MAX, pulse=True)
                    yield Static("latest", classes="dashboard-label")
                    # Scrolls, so a long reason can be read in full.
                    with VerticalScroll(id="feeling-latest-scroll"):
                        yield Static(id="feeling-latest")
                with Vertical(id="memories-box", classes="dashboard-box"):
                    yield Meters(id="memory-kinds", placeholder="loading…")
                    yield Static("new per day", classes="dashboard-label")
                    # Older days dim as recall fades them.
                    yield BrailleGraph(id="memory-days", pulse=True, grow=True,
                                       shade=lambda days: fade_weight(timedelta(days=days)))
                    yield Static("waiting", classes="dashboard-label")
                    yield Static(id="memory-pending")
                with Vertical(id="interests-box", classes="dashboard-box"):
                    yield Meters(id="interest-strengths", placeholder="loading…")
                    yield Static("latest finding", classes="dashboard-label")
                    # Scrolls, so a long finding can be read in full.
                    with VerticalScroll(id="interest-finding-scroll"):
                        yield Static(id="interest-finding")
                with Vertical(id="identity-box", classes="dashboard-box"):
                    # Scrolls, so every trait can be read in full.
                    with VerticalScroll(id="identity-scroll"):
                        yield Static(id="identity-traits")
                with Vertical(id="desires-box", classes="dashboard-box"):
                    # Scrolls, so every desire can be read in full.
                    with VerticalScroll(id="desires-scroll"):
                        yield Static(id="desire-list")
            # Pans sideways like the middle row once the boxes reach their minimum widths.
            with HorizontalScroll(id="dashboard-bottom", classes="dashboard-row", can_focus=False):
                # Under her feelings: the slower-moving relationship.
                with Vertical(id="relationship-box", classes="dashboard-box"):
                    yield Meters(id="relationship-meters", placeholder="loading…")
                    yield Static("history", classes="dashboard-label")
                    for name in RELATIONSHIP_VALUES:
                        with Horizontal(classes="relationship-history"):
                            yield Static(name, classes="relationship-history-name")
                            yield BrailleGraph(id=f"{name}-history", pulse=True, grow=True)
                    yield Static("recent tone", classes="dashboard-label")
                    yield ToneRibbon(id="relationship-tones")
                with Vertical(id="conversations-box", classes="dashboard-box"):
                    if self.load_spend is not None:
                        yield SpendChart(id="spend-chart")
                    yield Static("your messages per day", classes="dashboard-label")
                    yield CalendarHeatmap(id="conversation-days")
                with Vertical(id="tasks-box", classes="dashboard-box"):
                    # Scrolls, so every open task can be read.
                    with VerticalScroll(id="tasks-scroll"):
                        yield Static(id="task-list")
                        yield Static("sign-ups", id="sign-up-label", classes="dashboard-label")
                        yield Static(id="sign-up-list")
                with Vertical(id="context-box", classes="dashboard-box"):
                    yield Static("connect to measure", id="context-placeholder")
                    # Each item's share of its model's startup context; scrolls if the box is short.
                    with VerticalScroll(id="context-scroll"):
                        # Each model's measured fill, as the API reports it.
                        yield Meters(id="context-totals")
                        for side in (VOICE, BACKEND):
                            yield Static(side, classes="dashboard-label")
                            yield ContextMeters(id=f"context-{side}")
                with Vertical(id="background-box", classes="dashboard-box"):
                    yield Static("last ran · next", classes="dashboard-label")
                    yield Static(id="background-runs")

    def on_mount(self) -> None:
        for box_id, title in BOX_TITLES.items():
            self.query_one(f"#{box_id}").border_title = title
        self.set_interval(0.05, self.sample_voice)
        self.sync_audio_controls()
        self.set_interval(0.1, self.sync_audio_controls)
        self.chat_log = self.query_one("#dashboard-chat", SelectableRichLog)
        self.conversation.add_mirror(self.chat_log)
        self.query_one("#dashboard-input", ChatComposer).focus()
        self.refresh_snapshot()
        self.set_interval(REFRESH_SECONDS, self.refresh_snapshot)
        if self.load_spend is not None:
            self.refresh_spend()
            self.set_interval(SPEND_REFRESH_SECONDS, self.refresh_spend)
        self.show_context()
        self.set_interval(1, self.show_context)

    @work(group="dashboard-spend", exclusive=True)
    async def refresh_spend(self) -> None:
        spend, message, hint = await self.load_spend()
        chart = self.query_one("#spend-chart", SpendChart)
        # A failed refresh keeps the last chart it had rather than blanking it.
        if spend is not None or chart.spend is None:
            chart.show(spend, message, hint)

    @work(group="dashboard-snapshot")
    async def refresh_snapshot(self) -> None:
        snapshot = await self.load_snapshot()
        if snapshot is not None:
            self.snapshot = snapshot
            self.show_snapshot()

    def show_snapshot(self) -> None:
        meters = self.query_one("#relationship-meters", Meters)
        relationship = self.snapshot.relationship
        if relationship is None:
            meters.placeholder = "no relationship yet"
            meters.rows = ()
        else:
            meters.rows = tuple((name, getattr(relationship, name)) for name in RELATIONSHIP_VALUES)
        # Ends at the current value, so each graph meets its meter.
        history = self.snapshot.history + ((relationship,) if relationship else ())
        for name in RELATIONSHIP_VALUES:
            self.query_one(f"#{name}-history", BrailleGraph).values = tuple(
                getattr(values, name) for values in history
            )
        self.query_one("#relationship-tones", ToneRibbon).tones = self.snapshot.tones
        for feeling, levels, jumped in self.snapshot.feelings:
            graph = self.query_one(f"#{feeling}-feeling", BrailleGraph)
            graph.pulse = jumped
            graph.values = levels
            # Bright while she still feels it, dim once it has passed.
            self.query_one(f"#{feeling}-name").set_class(bool(levels) and band(levels[-1]) is not None, "feeling-felt")
        kinds = self.query_one("#memory-kinds", Meters)
        total = sum(self.snapshot.memory_kinds.values())
        kinds.placeholder = "no memories yet"
        kinds.rows = tuple(
            (kind, count / total, str(count)) for kind, count in self.snapshot.memory_kinds.items()
        ) if total else ()
        days = self.query_one("#memory-days", BrailleGraph)
        # Scaled to the busiest day on screen, two days per cell.
        days.maximum = max(self.snapshot.memory_days[-days.size.width * 2:], default=0) or 1
        days.values = self.snapshot.memory_days
        waiting = {step: count for step, count in self.snapshot.memory_pending.items() if count}
        pending = self.query_one("#memory-pending", Static)
        pending.update(" · ".join(f"{count} {step}" for step, count in waiting.items()) or "nothing")
        pending.set_class(bool(waiting), "memory-waiting")
        strengths = self.query_one("#interest-strengths", Meters)
        strengths.placeholder = "no interests yet"
        strengths.rows = tuple(
            (title, strength, f"{'↑' if rising else '↓'} {strength:.0%}")
            for title, strength, rising in self.snapshot.interests
        )
        latest = self.snapshot.latest_finding
        # Text rather than a string, so brackets in the model's finding are not read as markup.
        self.query_one("#interest-finding", Static).update(
            Text.assemble((latest[0], "#7fb8d8"), ": ", latest[1]) if latest
            else Text("nothing found yet", style=TRACK_STYLE)
        )
        traits = Text()
        for index, trait in enumerate(self.snapshot.traits):
            held = is_held(trait)
            # A forming trait is dimmed: it doesn't reach conversations until it is held.
            traits.append(("\n\n" if index else "") + f"{trait.confidence:.0%} ", style="#d8a6c8" if held else LABEL_STYLE)
            traits.append(trait.kind + ("" if held else " · forming"), style=LABEL_STYLE)
            traits.append("\n" + trait.statement, style="" if held else LABEL_STYLE)
        self.query_one("#identity-traits", Static).update(traits or Text("no traits yet", style=TRACK_STYLE))
        # Text, so brackets in a wish are not read as markup.
        self.query_one("#desire-list", Static).update(desire_lines(self.snapshot.desires))
        now = datetime.now(timezone.utc)
        # Text, so brackets in a reason are not read as markup.
        self.query_one("#feeling-latest", Static).update(latest_feeling_line(self.snapshot.latest_feeling, now))
        heatmap = self.query_one("#conversation-days", CalendarHeatmap)
        heatmap.today_row = now.astimezone().weekday()
        heatmap.days = self.snapshot.message_days
        # Text, so brackets in a goal or a step are not read as markup.
        self.query_one("#task-list", Static).update(task_lines(self.snapshot.tasks, now))
        self.query_one("#tasks-box").border_subtitle = f"{len(self.snapshot.tasks)} open" if self.snapshot.tasks else ""
        self.query_one("#sign-up-label").display = bool(self.snapshot.sign_ups)
        self.query_one("#sign-up-list", Static).update(sign_up_lines(self.snapshot.sign_ups, now))
        width = max(map(len, self.snapshot.last_runs), default=0)
        runs = Text()
        for index, (job, then) in enumerate(self.snapshot.last_runs.items()):
            runs.append(("\n" if index else "") + f"{job:<{width}}  ", style=LABEL_STYLE)
            last = ago(then, now)
            runs.append(last, style=TRACK_STYLE if then is None else "")
            # Only the timed jobs have a next time; the rest run when there is work.
            if job in self.snapshot.next_due:
                runs.append(" " * (10 - len(last)) + until(self.snapshot.next_due[job], now), style=LABEL_STYLE)
        self.query_one("#background-runs", Static).update(runs)

    def show_context(self) -> None:
        started = bool(self.context.startup)
        self.query_one("#context-placeholder", Static).display = not started
        self.query_one("#context-scroll", VerticalScroll).display = started
        # Standby or a dropped connection keeps showing what the last one held.
        self.query_one("#context-box").border_subtitle = "" if self.context.active or not started else "last session"
        ratio = self.context.voice_ratio
        tokens = self.context.backend_tokens
        limit = BACKEND_CONTEXT_TOKENS.get(self.context.backend_model)
        if tokens is None:
            backend = ("backend", 0.0, "after a request")
        elif limit:
            backend = ("backend", tokens / limit, f"{tokens / limit:.0%} · {short_tokens(tokens)}")
        else:
            backend = ("backend", 0.0, short_tokens(tokens))
        self.query_one("#context-totals", Meters).rows = (
            ("voice", ratio or 0.0, "measuring…" if ratio is None else f"{ratio:.0%}"), backend,
        )
        for side in (VOICE, BACKEND):
            items = self.context.items(side)
            total = sum(item.tokens for item in items) or 1
            self.query_one(f"#context-{side}", Meters).rows = tuple(
                (item.name, item.tokens / total, f"~{short_tokens(item.tokens)}") for item in items
            )

    def on_context_meters_selected(self, event: ContextMeters.Selected) -> None:
        items = self.context.items(event.meters.id.removeprefix("context-"))
        # The rows are redrawn every second, so an item may have gone since the click.
        if event.index < len(items):
            self.app.push_screen(ContextScreen(items[event.index]))

    def on_unmount(self) -> None:
        self.conversation.remove_mirror(self.chat_log)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        chat_button = {
            "dashboard-speaker": self.audio_status.speaker,
            "dashboard-mic": self.audio_status.mic,
            "dashboard-standby": self.connection,
        }.get(event.button.id)
        if chat_button is None:
            return
        event.stop()
        # The chat's buttons own the mute logic, so both screens stay in step.
        chat_button.press()
        self.query_one("#dashboard-input", ChatComposer).focus()

    def sync_audio_controls(self) -> None:
        live = self.audio_status.live
        # The connection shows its state as the chat does, like "● active" or "○ standby".
        connection_label = str(self.connection.label).lower()
        for button_id, chat_button, label, classes in (
            ("#dashboard-speaker", self.audio_status.speaker,
             "🔇 muted" if live.speaker_muted else "🔊 speaker", {"audio-muted": live.speaker_muted}),
            ("#dashboard-mic", self.audio_status.mic,
             "🎙 muted" if live.microphone_muted else "🎙 mic", {"audio-muted": live.microphone_muted}),
            ("#dashboard-standby", self.connection, connection_label, {
                f"state-{state}": self.connection.has_class(f"state-{state}")
                for state in ("active", "standby", "connecting", "disconnecting")
            }),
        ):
            button = self.query_one(button_id, Button)
            if str(button.label) != label:
                button.label = label
                # Button.label repaints but does not invalidate its cached auto width.
                button.refresh(layout=True)
            for name, on in classes.items():
                button.set_class(on, name)
            button.tooltip = chat_button.tooltip
            button.disabled = chat_button.disabled
        summary = self.query_one("#dashboard-activity", Static)
        panel = self.activity.panel
        if str(summary.render()) != panel.title:
            # Text, so brackets in an error summary are not read as markup.
            summary.update(Text(panel.title))
        summary.set_class(panel.has_class("activity-error"), "activity-error")

    def sample_voice(self) -> None:
        speaker, mic = self.voice_bands()
        for graph_id, bands in (("#speaker-graph", speaker), ("#mic-graph", mic)):
            graph = self.query_one(graph_id, BrailleGraph)
            current = graph.values or (0.0,) * BAND_COUNT
            latest = bands or (0.0,) * BAND_COUNT
            graph.values = tuple(max(new, old - FALL_PER_TICK) for old, new in zip(current, latest))
