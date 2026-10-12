"""Lazy chat storage adapter; call its methods off the UI thread."""

from uuid import UUID, uuid4

from sqlalchemy import Engine

from conversation_store import ConversationStore
from database import build_engine


class KeyboardPersistence:
    def __init__(self, store: ConversationStore | None = None):
        self.conversation_id = uuid4()
        self.store = store
        self.engine: Engine | None = None

    def _get_store(self) -> ConversationStore:
        if self.store is None:
            self.engine = build_engine()
            self.store = ConversationStore(self.engine)
        self.store.create_conversation(self.conversation_id)
        return self.store

    def save_message(self, message_id: UUID, text: str) -> None:
        self._get_store().save_message(
            self.conversation_id, message_id=message_id, role="user", text=text,
            channel="keyboard", status="completed", delivery_status="pending",
        )

    def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str) -> None:
        # Voice records are display chunks, not API-confirmed complete turns.
        self._get_store().save_message(
            self.conversation_id, message_id=message_id, role=role, text=text,
            channel=channel, status="completed" if channel == "screen" else "incomplete",
        )

    def record_delivery(self, message_id: UUID, status: str) -> None:
        if self.store is None:
            raise RuntimeError("No keyboard message has been saved yet")
        self.store.set_delivery_status(self.conversation_id, message_id, status)

    def close(self) -> None:
        # Do not dispose an injected store's caller-owned engine.
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
            self.store = None