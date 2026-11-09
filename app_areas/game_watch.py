"""Watching the game through the Decisions API (Settings > Game): part of CoraApp (see app.py).

Step 2 of game awareness only records: every answered frame goes to game-watch.log (one per
launch, next to app.py), notable ones get a line in Activity, and what it costs is summed up
there every minute. Nothing reaches Cora yet."""

from datetime import datetime, timezone
import json
from pathlib import Path

from textual import work

from game.chat_line import game_in_front
from game.watcher import QUESTIONS, Frame, GameWatcher

LOG_PATH = Path(__file__).resolve().parents[1] / "game-watch.log"
SUMMARY_SECONDS = 60
# A predicate this likely, or "worth_mentioning" at this level or above, makes a frame notable.
NOTABLE_PROBABILITY = 0.6
NOTABLE_MENTION = 2.0
HEALTH_LABELS = [level["label"] for question in QUESTIONS if question["name"] == "health"
                 for level in question["levels"]]


def frame_line(frame: Frame) -> str:
    """A frame's answers in a few words: "combat · health low · close_call 0.82"."""
    answers = frame.answers
    parts = [str(answers.get("scene") or "?")]
    health = answers.get("health")
    if isinstance(health, (int, float)):
        parts.append(f"health {HEALTH_LABELS[min(round(health), len(HEALTH_LABELS) - 1)]} ({health:.1f})")
    mention = answers.get("worth_mentioning")
    if isinstance(mention, (int, float)) and mention >= NOTABLE_MENTION:
        parts.append(f"worth mentioning {mention:.1f}")
    parts += [f"{name} {value:.2f}" for name, value in answers.items()
              if name not in ("health", "worth_mentioning") and isinstance(value, float)
              and value >= NOTABLE_PROBABILITY]
    return " · ".join(parts)


def notable(frame: Frame, previous_scene: str | None) -> bool:
    answers = frame.answers
    mention = answers.get("worth_mentioning")
    return (answers.get("scene") != previous_scene
            or (isinstance(mention, (int, float)) and mention >= NOTABLE_MENTION)
            or any(isinstance(value, float) and value >= NOTABLE_PROBABILITY
                   for name, value in answers.items() if name not in ("health", "worth_mentioning")))


class GameWatchMixin:
    """Watching the game through the Decisions API."""

    def init_game_watch(self) -> None:
        self.game_watch = False
        self.game_screen_watcher: GameWatcher | None = None
        self.game_watch_worker = None
        self.game_watch_timer = None
        self.game_watch_scene: str | None = None
        self.game_watch_logged = False

    def start_game_watch(self) -> None:
        if self.closing or self.game_watch_worker is not None:
            return
        self.game_screen_watcher = GameWatcher(self.game_frame_seen, self.game_frame_failed, in_front=game_in_front)
        self.game_watch_scene = None
        self.game_watch_worker = self.run_worker(self.game_screen_watcher.run(), group="game-watch", exclusive=True)
        self.game_watch_timer = self.set_interval(SUMMARY_SECONDS, self.report_game_watch)
        self.activity.write("Watching the game: a frame every 2 seconds while WoW is in front, answers to "
                            f"{LOG_PATH.name} (logging only).", "Game watch on")

    def stop_game_watch(self) -> None:
        worker, self.game_watch_worker = self.game_watch_worker, None
        if worker is not None:
            worker.cancel()
        timer, self.game_watch_timer = self.game_watch_timer, None
        if timer is not None:
            timer.stop()
        if self.game_screen_watcher is not None and self.game_screen_watcher.totals.sent:
            self.activity.write(f"Game watch stopped: {self.game_screen_watcher.totals.summary()}.", "Game watch off")
        self.game_screen_watcher = None

    def report_game_watch(self) -> None:
        watcher = self.game_screen_watcher
        if watcher is not None and (watcher.totals.sent or watcher.totals.failed):
            self.activity.write(f"Game watch: {watcher.totals.summary()}.", "Game watch")

    def game_frame_seen(self, frame: Frame) -> None:
        self.log_game_frame(frame)
        if notable(frame, self.game_watch_scene):
            self.activity.write(f"Game: {frame_line(frame)}", "Game watch")
        scene = frame.answers.get("scene")
        if isinstance(scene, str):
            self.game_watch_scene = scene

    def game_frame_failed(self, error: Exception) -> None:
        failed = self.game_screen_watcher.totals.failed if self.game_screen_watcher is not None else 1
        # The first failure, then one in ten, so a lost connection doesn't fill Activity.
        if failed == 1 or failed % 10 == 0:
            self.activity.write(f"Game watch: a frame failed ({type(error).__name__}; {failed} so far).", "Game watch")

    def log_game_frame(self, frame: Frame) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(), "seconds": round(frame.seconds, 3),
                  "input_tokens": frame.input_tokens, "answers": frame.answers}
        try:
            # A new log each launch.
            with LOG_PATH.open("a" if self.game_watch_logged else "w", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")
            self.game_watch_logged = True
        except OSError:
            pass

    @work(group="model-settings-save")
    async def save_game_watch(self, on: bool) -> None:
        self.game_watch = on
        if on:
            self.start_game_watch()
        else:
            self.stop_game_watch()
        try:
            await self.conversation_persistence.call_database(self.persistence.save_game_watch, on)
        except Exception as error:
            self.activity.write(f"Game setting save failed ({type(error).__name__}).", "Settings error")
            self.notify("Could not save watching the game; it applies until Cora closes.", severity="error")
