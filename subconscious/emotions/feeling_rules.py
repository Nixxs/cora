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
REASON_MAX_CHARS = 200
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


def latest_reasons(reactions: list[Reaction], now: datetime) -> dict[str, str]:
    """The most recent reason she gave for each feeling, up to `now`."""
    reasons = {}
    for reaction in sorted((item for item in reactions if item.at <= now), key=lambda item: item.at):
        if reaction.reason.strip():
            reasons[reaction.feeling] = " ".join(reaction.reason.split())[:REASON_MAX_CHARS]
    return reasons


def expression(level: float) -> str | None:
    """How hard the feeling is to keep to herself; None once it has passed."""
    if level < NOTICEABLE:
        return None
    if level < CLEAR:
        return "Let it show or keep it to yourself, as you choose."
    if level < STRONG:
        return "You can still keep it to yourself, but that takes some effort."
    return "It's hard to hide: even unspoken, it's likely to come through in your tone."


def feelings_now(reactions: list[Reaction], now: datetime) -> list[str]:
    """One line per noticeable feeling, strongest first: what she's feeling, why, and how
    hard it is to hide."""
    levels, reasons = levels_at(reactions, now), latest_reasons(reactions, now)
    lines = []
    for feeling in sorted(FEELINGS, key=lambda name: -levels[name]):
        hint = expression(levels[feeling])
        if hint is None:
            continue
        why = f" (about: {reasons[feeling]})" if feeling in reasons else ""
        lines.append(f"You feel {FEELING_WORDS[feeling]}{why}. {hint}")
    return lines


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
