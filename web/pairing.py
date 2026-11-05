"""Pairing a device with the web interface: a short code shown in Settings, swapped once for a long token.

A code exists only while the person has the pairing screen open in Settings. It works once, lasts a
few minutes, and a few wrong guesses cancel it, so it can't be guessed from outside. The device
then holds a long random token in a cookie; Cora keeps only the token's SHA-256.
"""

from dataclasses import dataclass
import hashlib
import hmac
import secrets
from time import monotonic

# No 0/O, 1/I/L or 5/S: easy to read off a screen and type.
CODE_ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ2346789"
CODE_LENGTH = 8
CODE_SECONDS = 5 * 60
CODE_ATTEMPTS = 5


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def shown_code(code: str) -> str:
    """K7QFM2XP -> K7QF-M2XP, as Settings shows it."""
    return f"{code[:4]}-{code[4:]}"


def typed_code(text: str) -> str:
    """What someone typed, made comparable: case, spaces and dashes don't matter."""
    return "".join(character for character in str(text).upper() if character.isalnum())[:CODE_LENGTH * 2]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class OpenCode:
    code: str
    expires_at: float
    attempts_left: int = CODE_ATTEMPTS


# What try_code found.
PAIRED, WRONG, NO_CODE = "paired", "wrong", "no code"


class Pairing:
    """The one pairing code open now, if any. clock is monotonic seconds (replaceable in tests)."""

    def __init__(self, clock=monotonic) -> None:
        self.clock = clock
        self.open: OpenCode | None = None

    def start(self) -> str:
        """A fresh code, replacing any open one."""
        self.open = OpenCode(new_code(), self.clock() + CODE_SECONDS)
        return self.open.code

    def cancel(self) -> None:
        self.open = None

    def current(self) -> OpenCode | None:
        """The open code, or None once it has expired."""
        if self.open is not None and self.clock() >= self.open.expires_at:
            self.open = None
        return self.open

    def seconds_left(self) -> int:
        code = self.current()
        return max(0, round(code.expires_at - self.clock())) if code is not None else 0

    def try_code(self, typed: str) -> str:
        """PAIRED uses the code up; WRONG counts against it; NO_CODE when none is open."""
        code = self.current()
        if code is None:
            return NO_CODE
        if hmac.compare_digest(typed_code(typed).encode(), code.code.encode()):
            self.open = None
            return PAIRED
        code.attempts_left -= 1
        if code.attempts_left <= 0:
            self.open = None
        return WRONG
