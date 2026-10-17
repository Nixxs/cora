"""Check the model's appraisal of one finished conversation before it is saved.

The checks mirror migration 0013, so a malformed reply is rejected whole here with a
clear reason rather than failing on insert; the conversation is appraised again later.
"""

from dataclasses import dataclass

SETTINGS = ("everyday", "development")
TONES = ("warm", "neutral", "curt", "hostile")
FRICTIONS = ("none", "at_situation", "at_cora")
REPAIRS = ("none", "person", "cora", "both")
REASON_MAX_CHARS = 1000
APPRAISAL_FIELDS = {"setting", "tone", "friction", "cora_mistake", "repair", "reason"}


@dataclass(frozen=True)
class Appraisal:
    setting: str
    tone: str
    friction: str
    cora_mistake: bool
    repair: str
    reason: str


def validate_appraisal(payload: object) -> Appraisal:
    if not isinstance(payload, dict) or set(payload) != APPRAISAL_FIELDS:
        raise ValueError("Invalid appraisal fields")
    for name, allowed in (("setting", SETTINGS), ("tone", TONES), ("friction", FRICTIONS), ("repair", REPAIRS)):
        if not isinstance(payload[name], str) or payload[name] not in allowed:
            raise ValueError(f"Unknown appraisal {name}")
    if not isinstance(payload["cora_mistake"], bool):
        raise ValueError("Appraisal cora_mistake must be true or false")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("An appraisal needs a reason")
    reason = reason.strip()
    if len(reason) > REASON_MAX_CHARS:
        raise ValueError(f"Appraisal reason is longer than {REASON_MAX_CHARS} characters")
    repair = payload["repair"]
    if repair in ("person", "both") and payload["friction"] != "at_cora":
        raise ValueError("The person only makes amends for friction aimed at Cora")
    if repair in ("cora", "both") and not payload["cora_mistake"]:
        raise ValueError("Cora only makes amends for her own mistake")
    return Appraisal(payload["setting"], payload["tone"], payload["friction"], payload["cora_mistake"],
                     repair, reason)
