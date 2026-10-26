"""Lazy chat storage adapter; call its methods off the UI thread."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Engine

from .client_store import Client, ClientStore
from .conversation_store import ConversationStore
from .dashboard_snapshot import DashboardSnapshot, read_dashboard_snapshot
from .database import build_engine
from .model_settings_store import ModelSettingsStore
from .people_store import PeopleStore
from .service_settings_store import ServiceSettingsStore
from .signal_settings_store import SignalSettings, SignalSettingsStore
from memory.store import MemoryStore
from subconscious.identity.identity_store import IdentityStore
from subconscious.interests.interest_store import InterestStore
from subconscious.relationship.relationship_rules import RelationshipValues
from subconscious.relationship.relationship_store import RelationshipStore


class KeyboardPersistence:
    def __init__(self, store: ConversationStore | None = None):
        self.conversation_id = uuid4()
        self.store = store
        self.engine: Engine | None = None

    def _get_store(self) -> ConversationStore:
        if self.store is None:
            self.engine = build_engine()
            self.store = ConversationStore(self.engine)
        return self.store

    def mark_interrupted_conversations(self) -> int:
        # Startup checks previous runs without creating a row for this run.
        return self._get_store().interrupt_active_conversations(self.conversation_id)

    def load_startup_memory_context(self) -> str:
        # Share the existing owned/injected engine; do not create a chat row.
        return MemoryStore(self._get_store().engine).get_startup_context()

    def load_startup_interest_context(self) -> str:
        return InterestStore(self._get_store().engine).get_startup_context(now=datetime.now(timezone.utc))

    def load_startup_identity_context(self) -> str:
        return IdentityStore(self._get_store().engine).get_startup_context()

    def load_relationship_context(self) -> str:
        return RelationshipStore(self._get_store().engine).get_startup_context(now=datetime.now(timezone.utc))

    def load_relationship_values(self) -> RelationshipValues | None:
        return RelationshipStore(self._get_store().engine).current_values(now=datetime.now(timezone.utc))

    def load_person_name(self) -> str | None:
        person = PeopleStore(self._get_store().engine).current_person()
        return person.display_name if person is not None else None

    def load_delegation_model(self) -> str:
        return ModelSettingsStore(self._get_store().engine).model_for("delegation")

    def load_models(self) -> dict:
        return ModelSettingsStore(self._get_store().engine).models()

    def save_model(self, area: str, model: str) -> None:
        ModelSettingsStore(self._get_store().engine).set_model(area, model)

    def load_enabled_services(self) -> set[str]:
        return ServiceSettingsStore(self._get_store().engine).enabled_services()

    def save_service_enabled(self, service: str, enabled: bool) -> None:
        ServiceSettingsStore(self._get_store().engine).set_enabled(service, enabled)

    def load_clients(self) -> list[Client]:
        return ClientStore(self._get_store().engine).clients()

    def add_client(self, name: str, number: str) -> Client:
        return ClientStore(self._get_store().engine).add(name, number)

    def remove_client(self, person_id: UUID) -> None:
        ClientStore(self._get_store().engine).remove(person_id)

    def client_conversation(self, person_id: UUID) -> UUID:
        return ClientStore(self._get_store().engine).conversation_for(person_id, datetime.now(timezone.utc))

    def save_client_message(self, conversation_id: UUID, role: str, text: str) -> None:
        ClientStore(self._get_store().engine).save(conversation_id, role, text)

    def load_client_history(self, conversation_id: UUID) -> list[dict]:
        return ClientStore(self._get_store().engine).history(conversation_id)

    def load_signal_settings(self) -> SignalSettings:
        return SignalSettingsStore(self._get_store().engine).settings()

    def save_signal_enabled(self, enabled: bool) -> None:
        SignalSettingsStore(self._get_store().engine).set_enabled(enabled)

    def save_signal_allowed_number(self, number: str) -> None:
        SignalSettingsStore(self._get_store().engine).set_allowed_number(number)

    def save_signal_account(self, account: str | None) -> None:
        SignalSettingsStore(self._get_store().engine).set_account(account)

    def load_service_tools(self) -> dict[str, set[str]]:
        return ServiceSettingsStore(self._get_store().engine).enabled_tools()

    def save_service_tool(self, service: str, tool: str, enabled: bool) -> None:
        ServiceSettingsStore(self._get_store().engine).set_tool_enabled(service, tool, enabled)

    def load_last_talked_at(self) -> datetime | None:
        return self._get_store().last_message_time(self.conversation_id)

    def load_last_summary(self) -> str | None:
        return self._get_store().last_conversation_summary(self.conversation_id)

    def load_recent_plan(self) -> str | None:
        return MemoryStore(self._get_store().engine).recent_plan(now=datetime.now(timezone.utc))

    def load_dashboard_snapshot(self) -> DashboardSnapshot:
        return read_dashboard_snapshot(self._get_store().engine, datetime.now(timezone.utc))

    def save_message(self, message_id: UUID, text: str) -> None:
        store = self._get_store()
        store.create_conversation(self.conversation_id)
        store.save_message(
            self.conversation_id, message_id=message_id, role="user", text=text,
            channel="keyboard", status="completed", delivery_status="pending",
        )

    def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str) -> None:
        # Voice records are display chunks, not API-confirmed complete turns.
        store = self._get_store()
        store.create_conversation(self.conversation_id)
        store.save_message(
            self.conversation_id, message_id=message_id, role=role, text=text,
            channel=channel, status="completed" if channel == "screen" else "incomplete",
        )

    def record_delivery(self, message_id: UUID, status: str) -> None:
        if self.store is None:
            raise RuntimeError("No keyboard message has been saved yet")
        self.store.set_delivery_status(self.conversation_id, message_id, status)

    def finish_conversation(self) -> None:
        # Do not create an empty conversation just to close it.
        if self.store is not None:
            self.store.close_conversation(self.conversation_id)

    def start_new_conversation(self) -> None:
        # Standby ends this conversation; anything said after waking starts the next one.
        self.finish_conversation()
        self.conversation_id = uuid4()

    def close(self) -> None:
        # Do not dispose an injected store's caller-owned engine.
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
            self.store = None