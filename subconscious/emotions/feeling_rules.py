"""Pure feeling rules: a fixed set of feelings, how a reaction raises one, and how it fades.

No database, model, or clock access: callers pass in timestamps. Each feeling is a level
from 0 to 1, worked out from the reactions she reported. A reaction moves the level a
capped fraction of the way to 1 (so it stays in bounds and each step shrinks near the
top), and between reactions every level fades halfway back to 0 each HALF_LIFE.

She chooses whether a feeling shows, as a person would, but the stronger it is the
harder it is to keep out of her voice: `expression` says how much effort hiding it takes.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# None is designed for guilt, neediness or possessiveness.
FEELINGS = ("excitement", "contentment", "curiosity", "fulfilment", "irritation", "hurt")
# How each reads in a sentence: "You feel ...".
FEELING_WORDS = {
    "excitement": "excited", "contentment": "content", "curiosity": "curious",
    "fulfilment": "fulfilled", "irritation": "irritated", "hurt": "hurt",
}
# The fraction of the way to 1 one reaction moves a feeling; strong is the cap.
STRENGTHS = {"small": 0.1, "medium": 0.2, "strong": 0.3}
HALF_LIFE = timedelta(minutes=30)
# Below this a feeling has passed and isn't mentioned to her: a small reaction lasts
# about half an hour, a strong one about an hour and a half.
NOTICEABLE = 0.05
# How hard a feeling is to keep to herself, by level: a fresh medium reaction takes effort
# to hide, a fresh strong one is hard to hide, and both ease as they fade.
CLEAR = 0.18
STRONG = 0.28
# A reason as she gives it with feel, and as her context shows it.
REASON_MAX_CHARS = 200
# A reason as kept: longer, so one from looking back on a conversation (the appraisal's whole
# reason, up to 1,000 characters, after its "thinking back…" opening) is saved whole for the dashboard.
STORED_REASON_MAX_CHARS = 1200
# Far enough back that anything older has faded to nothing (a full feeling, after twelve half-lives).
LOOKBACK = HALF_LIFE * 12
# How often she may report a feeling: a few genuine reactions a conversation, not a running commentary,
# and one feeling can't be pushed up again straight away.
MAX_PER_CONVERSATION = 8
SAME_FEELING_GAP = timedelta(minutes=5)


@dataclass(frozen=True)
class Reaction:
    feeling: str
    strength: str
    at: datetime
    reason: str = ""


def validate_reaction(feeling: str, strength: str) -> None:
    if feeling not in FEELINGS:
        raise ValueError("Unknown feeling")
    if strength not in STRENGTHS:
        raise ValueError("Unknown feeling strength")


def fade(level: float, elapsed: timedelta) -> float:
    """The level after `elapsed`, halving every HALF_LIFE."""
    if elapsed <= timedelta(0):
        return level
    return level * 0.5 ** (elapsed / HALF_LIFE)


def raise_level(level: float, strength: str) -> float:
    """One reaction: move the level that fraction of the way to 1."""
    return level + (1.0 - level) * STRENGTHS[strength]


def levels_at(reactions: list[Reaction], now: datetime) -> dict[str, float]:
    """Every feeling's level at `now`, from the reactions up to then, in time order."""
    levels = dict.fromkeys(FEELINGS, 0.0)
    since = dict.fromkeys(FEELINGS, None)
    for reaction in sorted((item for item in reactions if item.at <= now), key=lambda item: item.at):
        validate_reaction(reaction.feeling, reaction.strength)
        last = since[reaction.feeling]
        level = levels[reaction.feeling] if last is None else fade(levels[reaction.feeling], reaction.at - last)
        levels[reaction.feeling] = raise_level(level, reaction.strength)
        since[reaction.feeling] = reaction.at
    return {feeling: 0.0 if since[feeling] is None else fade(levels[feeling], now - since[feeling])
            for feeling in FEELINGS}


def level_history(reactions: list[Reaction], now: datetime, span: timedelta, step: timedelta) -> dict[str, tuple[float, ...]]:
    """Each feeling's level every `step` across the `span` up to now, oldest first, ending at now.
    Reactions from before the span still count towards its start."""
    count = int(span / step)
    times = [now - step * (count - index) for index in range(count + 1)]
    levels = [levels_at(reactions, at) for at in times]
    return {feeling: tuple(sample[feeling] for sample in levels) for feeling in FEELINGS}


def latest_reasons(reactions: list[Reaction], now: datetime) -> dict[str, str]:
    """The most recent reason she gave for each feeling, up to `now`."""
    reasons = {}
    for reaction in sorted((item for item in reactions if item.at <= now), key=lambda item: item.at):
        if reaction.reason.strip():
            reasons[reaction.feeling] = " ".join(reaction.reason.split())[:REASON_MAX_CHARS]
    return reasons


# How hard a noticeable feeling is to keep to herself, weakest first.
BANDS = ("choose", "effort", "hard")
EXPRESSIONS = {
    "choose": "Let it show or keep it to yourself, as you choose.",
    "effort": "You can still keep it to yourself, but that takes some effort.",
    "hard": "It's hard to hide: even unspoken, it's likely to come through in your tone.",
}


def band(level: float) -> str | None:
    """Which of BANDS the level is in; None once it has passed."""
    if level < NOTICEABLE:
        return None
    if level < CLEAR:
        return "choose"
    return "effort" if level < STRONG else "hard"


def expression(level: float) -> str | None:
    """How hard the feeling is to keep to herself; None once it has passed."""
    current = band(level)
    return None if current is None else EXPRESSIONS[current]


@dataclass(frozen=True)
class FeelingNow:
    feeling: str
    band: str
    reason: str = ""

    def line(self, reasons: bool = True) -> str:
        """What she's feeling, why (unless left out), and how hard it is to hide."""
        why = f" (about: {self.reason})" if reasons and self.reason else ""
        return f"You feel {FEELING_WORDS[self.feeling]}{why}. {EXPRESSIONS[self.band]}"


def feelings_state(reactions: list[Reaction], now: datetime) -> list[FeelingNow]:
    """Every noticeable feeling now, strongest first."""
    levels, reasons = levels_at(reactions, now), latest_reasons(reactions, now)
    return [FeelingNow(feeling, band(levels[feeling]), reasons.get(feeling, ""))
            for feeling in sorted(FEELINGS, key=lambda name: -levels[name]) if band(levels[feeling])]


def feelings_now(reactions: list[Reaction], now: datetime, reasons: bool = True) -> list[str]:
    """One line per noticeable feeling, strongest first."""
    return [item.line(reasons) for item in feelings_state(reactions, now)]


def feelings_context(feelings: list[FeelingNow], reasons: bool = True) -> str:
    """Her instructions' feelings section: one line each, or "" when none is noticeable."""
    return "\n".join(item.line(reasons) for item in feelings)


def band_changes(before: dict[str, str], now: list[FeelingNow]) -> tuple[list[str], list[str]]:
    """Since `before` (feeling -> band): the lines to tell her, and a short summary of each change
    (hurt eased, curiosity passed). A feeling whose band hasn't changed isn't repeated."""
    lines, summary = [], []
    current = {item.feeling: item for item in now}
    for item in now:
        old = before.get(item.feeling)
        if old == item.band:
            continue
        lines.append(item.line())
        change = "began" if old is None else ("rose" if BANDS.index(item.band) > BANDS.index(old) else "eased")
        summary.append(f"{item.feeling} {change}")
    for feeling in before:
        if feeling not in current:
            lines.append(f"Your {feeling} has passed.")
            summary.append(f"{feeling} passed")
    return lines, summary


# Thinking back on a conversation only stirs a feeling while it is recent; a backlog appraised later
# (Cora was closed) is left alone.
APPRAISAL_RECENT = timedelta(hours=2)


def appraisal_feeling(tone: str, friction: str, repair: str) -> tuple[str, str] | None:
    """At most one small or medium feeling from how a conversation went, only from what the appraisal
    found: unrepaired friction aimed at her or hostility hurts, curtness irritates, and a warm one
    with no friction leaves her content. Her own mistakes stir nothing (no guilt)."""
    if (friction == "at_cora" and repair not in ("person", "both")) or tone == "hostile":
        return "hurt", "medium"
    if tone == "curt":
        return "irritation", "small"
    if tone == "warm" and friction == "none":
        return "contentment", "small"
    return None


def parse_reaction(text: str, now: datetime) -> Reaction:
    """feeling:strength:minutes_ago, e.g. hurt:strong:40."""
    feeling, strength, minutes = text.split(":")
    validate_reaction(feeling, strength)
    return Reaction(feeling, strength, now - timedelta(minutes=float(minutes)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Try the feeling rules: levels now after some reactions.")
    parser.add_argument("reactions", nargs="+", help="feeling:strength:minutes_ago, e.g. hurt:strong:40")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    reactions = [parse_reaction(text, now) for text in args.reactions]
    for feeling, level in levels_at(reactions, now).items():
        print(f"{feeling:<12} {level:.2f}")
    print()
    print("\n".join(feelings_now(reactions, now)) or "No feeling is noticeable now.")


if __name__ == "__main__":
    main()
