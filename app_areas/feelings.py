"""Cora's feelings in the app: saving one she notes with the feel tool, and showing it in Activity."""

from subconscious.emotions.feeling_rules import FEELING_WORDS
from subconscious.emotions.feeling_store import FeelingLimit


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
