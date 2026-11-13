"""Put Cora's relationship values into words for her session prompt.

Words, not numbers, so the model reads a feeling rather than a score to report.
"""

from subconscious.relationship.relationship_rules import RelationshipValues

# Well above the longest three sentences; the session prompt rejects anything longer.
RELATIONSHIP_CONTEXT_MAX_CHARS = 1000

# Each tier applies below its upper limit; the last covers everything above. The middle tier hugs
# the 0.5 baseline closely so a single hostile conversation is already felt.
FAMILIARITY_TIERS = (
    (0.15, "You have only just met the person."),
    (0.4, "You are still getting to know the person."),
    (0.7, "You know the person fairly well by now."),
    (None, "You know the person well and share a lot of history."),
)
WARMTH_TIERS = (
    (0.3, "Right now you feel hurt by them and noticeably cool toward them."),
    (0.45, "Right now you feel a little cooler toward them than usual."),
    (0.55, "You feel friendly toward them, as you would toward most people."),
    (0.7, "You feel warm toward them and enjoy talking with them."),
    (None, "You are very fond of them."),
)
TRUST_TIERS = (
    (0.3, "Past friction has shaken your trust in them, so you are guarded."),
    (0.45, "After some friction you are a little wary of them."),
    (0.55, "You have no particular reason yet to trust or distrust them."),
    (0.7, "You trust them."),
    (None, "You trust them deeply."),
)


# How familiarity sounds: friends who know each other well tease and banter. Only while she isn't
# feeling cool toward them (warmth from 0.45), when teasing would read as a jab.
EASE_TIERS = (
    (0.4, ""),
    (0.7, "You're comfortable enough with them for some light teasing and banter."),
    (None, "You're at ease with them, the way old friends are: you banter, tease them affectionately "
           "and are a bit cheeky, with a quip or a dry aside rather than polite small talk. Keep it kind, "
           "and drop it when they're down, stressed or need something done."),
)
EASE_FROM_WARMTH = 0.45


def tier(value: float, tiers) -> str:
    return next(text for limit, text in tiers if limit is None or value < limit)


def describe_relationship(values: RelationshipValues) -> str:
    ease = tier(values.familiarity, EASE_TIERS) if values.warmth >= EASE_FROM_WARMTH else ""
    return " ".join(part for part in (
        tier(values.familiarity, FAMILIARITY_TIERS),
        tier(values.warmth, WARMTH_TIERS),
        tier(values.trust, TRUST_TIERS),
        ease,
    ) if part)
