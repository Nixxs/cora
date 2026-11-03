"""Cora's feelings in the app: saving one she notes with the feel tool, showing it in Activity, and
keeping her voice session up to date as her feelings ease, rise or pass."""

from textual import work

from subconscious.emotions.feeling_rules import FEELING_WORDS, band_changes
from subconscious.emotions.feeling_store import FeelingLimit

# How often a running voice session is checked for a feeling that has changed band. A database
# read and no model call; a note only goes when something changed.
FEELING_CHECK_SECONDS = 60


class FeelingsMixin:
    async def note_feeling(self, client, feeling: str, strength: str, reason: str) -> str | None:
        """Save a feeling she noted: client is (id, name) in a contact's chat, None with the person.
        None once saved, else why it wasn't, for her."""
        try:
            await self.conversation_persistence.call_database(
                self.persistence.record_feeling, feeling, strength, reason, None if client is None else client[0],
            )
        except FeelingLimit as limit:
            return str(limit)
        except Exception as error:
            self.activity.write(f"A feeling Cora noted wasn't saved ({type(error).__name__}).", "Feeling")
            return "It couldn't be noted just now; let it be."
        where = "" if client is None else f" in {client[1]}'s chat"
        self.activity.write(f"Cora feels {FEELING_WORDS[feeling]} ({strength}){where}: {reason}", "Feeling")
        return None

    def check_feelings(self) -> None:
        if self.closing or self.live.connection is None or not self.live.ready:
            return
        worker = getattr(self, "feeling_worker", None)
        if worker is not None and not worker.is_finished:
            return
        self.feeling_worker = self.update_voice_feelings()

    @work(group="feelings", exit_on_error=False)
    async def update_voice_feelings(self) -> None:
        """Quietly tell the voice session about each feeling that began, rose, eased or passed since it
        last heard, including ones she noted in a written chat meanwhile."""
        try:
            feelings = await self.conversation_persistence.call_database(self.persistence.load_feelings)
        except Exception as error:
            self.activity.write(f"Feelings didn't load ({type(error).__name__}); her voice wasn't updated.",
                                "Feeling")
            return
        lines, summary = band_changes(self.live.feeling_bands, feelings)
        self.live.feelings = feelings
        if not lines or self.closing:
            return
        try:
            await self.live.note_feelings(lines)
        except Exception as error:
            # Not marked as heard, so the next check tries again.
            self.activity.write(f"Her voice wasn't told how she feels ({type(error).__name__}).", "Feeling")
            return
        self.live.feeling_bands = {item.feeling: item.band for item in feelings}
        self.activity.write(f"Quiet note to her voice: {', '.join(summary)}.", "Feeling")
