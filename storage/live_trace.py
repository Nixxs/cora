"""Temporary metadata-only diagnostics; never pass chat text or audio here."""

from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic


class LiveTrace:
    def __init__(self, path: Path):
        self.path = path
        self.started = monotonic()
        self.sequence = 0
        self.failed = False

    def write(self, stage: str, **metadata) -> None:
        if self.failed:
            return
        self.sequence += 1
        record = {
            "sequence": self.sequence,
            "time": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(monotonic() - self.started, 6),
            "stage": stage,
            **metadata,
        }
        try:
            # One log per app launch; flush each record even if the app later hangs.
            with self.path.open("w" if self.sequence == 1 else "a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            # Diagnostics must never interrupt a send, response, or shutdown.
            self.failed = True