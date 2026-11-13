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
from .app_settings_store import AppSettingsStore
from .key_details_store import KeyDetailsStore
from .self_image_store import SelfImageStore
from .service_settings_store import ServiceSettingsStore
from .signal_settings_store import SignalSettings, SignalSettingsStore
from .webhook_settings_store import WebhookSettings, WebhookSettingsStore
from .web_device_store import WebDevice, WebDeviceStore
from .recent_messages import recent_person_messages
from .task_store import EmailParty, EmailThread, QuietParty, TaskStore
from memory.store import MemoryStore
from sign_ups.password import sign_up_name
from sign_ups.store import SignUp, SignUpStore
from subconscious.desires.desire_store import DesireStore
from subconscious.emotions.feeling_store import FeelingStore
from subconscious.identity.identity_store import IdentityStore
from subconscious.impressions.impression_store import HeadsUp, ImpressionStore
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

    def load_self_image_context(self) -> str:
        return SelfImageStore(self._get_store().engine).get_startup_context()

    def load_relationship_context(self) -> str:
        return RelationshipStore(self._get_store().engine).get_startup_context(now=datetime.now(timezone.utc))

    def load_people_context(self) -> str:
        return ImpressionStore(self._get_store().engine).people_context(datetime.now(timezone.utc))

    def load_heads_ups(self) -> list[HeadsUp]:
        return ImpressionStore(self._get_store().engine).unreported_heads_ups()

    def mark_heads_up_reported(self, conversation_id: UUID) -> None:
        ImpressionStore(self._get_store().engine).mark_reported(conversation_id)

    def load_client_people_context(self, person_id: UUID) -> str:
        return ImpressionStore(self._get_store().engine).client_context(person_id)

    def start_task(self, goal: str, who: UUID | EmailParty, brief: str, step: str,
                   deadline: datetime | None = None) -> UUID:
        return TaskStore(self._get_store().engine).start(goal, who, brief, step, deadline)

    def task_party_id(self, task_id: UUID, address: str) -> UUID | None:
        return TaskStore(self._get_store().engine).party_id(task_id, address)

    def add_task_step(self, task_id: UUID, text: str) -> None:
        TaskStore(self._get_store().engine).add_step(task_id, text)

    def add_task_party(self, task_id: UUID, who: UUID | EmailParty, brief: str, step: str,
                       deadline: datetime | None = None) -> None:
        TaskStore(self._get_store().engine).add_party(task_id, who, brief, step, deadline)

    def load_deadlines_due(self, now: datetime):
        return TaskStore(self._get_store().engine).deadlines_due(now)

    def task_deadline_told(self, task_id: UUID, passed: bool) -> None:
        TaskStore(self._get_store().engine).deadline_told(task_id, passed)

    def task_goal(self, task_id: UUID) -> str | None:
        return TaskStore(self._get_store().engine).goal(task_id)

    def report_task(self, task_id: UUID, person_id: UUID, outcome: str, settled: bool) -> str:
        return TaskStore(self._get_store().engine).report(task_id, person_id, outcome, settled)

    def report_email_task(self, task_id: UUID, party_id: UUID, outcome: str, settled: bool) -> str:
        return TaskStore(self._get_store().engine).report_email(task_id, party_id, outcome, settled)

    def load_email_threads(self) -> list[EmailThread]:
        return TaskStore(self._get_store().engine).email_threads()

    def email_party_heard(self, party_id: UUID, at: datetime) -> None:
        TaskStore(self._get_store().engine).email_heard(party_id, at)

    def task_email_thread(self, task_id: UUID, address: str) -> str | None:
        return TaskStore(self._get_store().engine).email_thread(task_id, address)

    def task_coordination(self, task_id: UUID) -> dict | None:
        return TaskStore(self._get_store().engine).coordination(task_id)

    def task_party_heard(self, person_id: UUID) -> None:
        TaskStore(self._get_store().engine).heard_from(person_id)

    def load_task_briefs(self, person_id: UUID) -> str:
        return TaskStore(self._get_store().engine).briefs_for(person_id)

    def load_task_checks(
        self, now: datetime,
    ) -> tuple[list[QuietParty], list[tuple[UUID, str]], list[tuple[UUID, str]], list[EmailThread]]:
        store = TaskStore(self._get_store().engine)
        return store.quiet_parties(now), store.stale_tasks(now), store.unsettled_tasks(), store.quiet_email_parties(now)

    def task_email_bounced(self, party_id: UUID, step: str) -> None:
        TaskStore(self._get_store().engine).email_bounced(party_id, step)

    def record_task_nudge(self, party_id: UUID, step: str) -> None:
        TaskStore(self._get_store().engine).record_nudge(party_id, step)

    def task_needs_person(self, task_id: UUID, reason: str, unless_waiting: bool = False) -> bool:
        return TaskStore(self._get_store().engine).needs_person(task_id, reason, unless_waiting)

    def close_task(self, task_id: UUID, status: str, note: str) -> str:
        return TaskStore(self._get_store().engine).close(task_id, status, note)

    def load_tasks_context(self) -> str:
        return TaskStore(self._get_store().engine).tasks_context(datetime.now(timezone.utc))

    def load_relationship_values(self) -> RelationshipValues | None:
        return RelationshipStore(self._get_store().engine).current_values(now=datetime.now(timezone.utc))

    def load_own_name(self) -> str:
        """Her full name from the credential store (not the database), as she introduces herself."""
        return sign_up_name()

    def load_person_name(self) -> str | None:
        person = PeopleStore(self._get_store().engine).current_person()
        return person.display_name if person is not None else None

    def load_delegation_model(self) -> str:
        return ModelSettingsStore(self._get_store().engine).model_for("delegation")

    def load_voice(self) -> str:
        return AppSettingsStore(self._get_store().engine).voice()

    def save_voice(self, voice: str) -> None:
        AppSettingsStore(self._get_store().engine).set_voice(voice)

    def load_start_mode(self) -> str:
        return AppSettingsStore(self._get_store().engine).start_mode()

    def save_start_mode(self, mode: str) -> None:
        AppSettingsStore(self._get_store().engine).set_start_mode(mode)

    def load_standby_settings(self) -> dict:
        """{"idle_minutes", "farewell", "wake_word"}: each None when never set in Settings."""
        store = AppSettingsStore(self._get_store().engine)
        return {"idle_minutes": store.idle_minutes(), "farewell": store.standby_farewell(),
                "wake_word": store.wake_word()}

    def save_idle_minutes(self, minutes: int) -> None:
        AppSettingsStore(self._get_store().engine).set_idle_minutes(minutes)

    def save_standby_farewell(self, on: bool) -> None:
        AppSettingsStore(self._get_store().engine).set_standby_farewell(on)

    def save_wake_word(self, on: bool) -> None:
        AppSettingsStore(self._get_store().engine).set_wake_word(on)

    def load_conversation_gap(self) -> int | None:
        """Minutes of quiet that end a conversation; None when never set in Settings."""
        return AppSettingsStore(self._get_store().engine).conversation_gap()

    def save_conversation_gap(self, minutes: int) -> None:
        AppSettingsStore(self._get_store().engine).set_conversation_gap(minutes)

    def load_twilio_region(self) -> str | None:
        """The Twilio region for calls she places; None when never set in Settings."""
        return AppSettingsStore(self._get_store().engine).twilio_region()

    def save_twilio_region(self, code: str) -> None:
        AppSettingsStore(self._get_store().engine).set_twilio_region(code)

    def load_game_settings(self) -> dict:
        """{"push_to_talk": bool, "channel", "transcription", "vocabulary": str} from Settings > Game."""
        store = AppSettingsStore(self._get_store().engine)
        return {"push_to_talk": store.game_push_to_talk(), "channel": store.game_chat_channel(),
                "transcription": store.game_transcription(), "vocabulary": store.game_vocabulary(),
                "watch": store.game_watch(), "chattiness": store.game_chattiness()}

    def save_game_push_to_talk(self, on: bool) -> None:
        AppSettingsStore(self._get_store().engine).set_game_push_to_talk(on)

    def save_game_chat_channel(self, channel: str) -> None:
        AppSettingsStore(self._get_store().engine).set_game_chat_channel(channel)

    def save_game_chattiness(self, level: str) -> None:
        AppSettingsStore(self._get_store().engine).set_game_chattiness(level)

    def save_game_watch(self, on: bool) -> None:
        AppSettingsStore(self._get_store().engine).set_game_watch(on)

    def save_game_transcription(self, choice: str) -> None:
        AppSettingsStore(self._get_store().engine).set_game_transcription(choice)

    def save_game_vocabulary(self, words: str) -> None:
        AppSettingsStore(self._get_store().engine).set_game_vocabulary(words)

    def load_claude_code_settings(self) -> dict:
        """{"listen": bool, "port": int, "tell": str, "transcripts": bool} from Settings > Claude Code."""
        store = AppSettingsStore(self._get_store().engine)
        return {"listen": store.claude_code_listen(), "port": store.claude_code_port(),
                "tell": store.claude_code_tell(), "transcripts": store.claude_code_transcripts()}

    def save_claude_code_transcripts(self, on: bool) -> None:
        AppSettingsStore(self._get_store().engine).set_claude_code_transcripts(on)

    def save_claude_code_tell(self, choice: str) -> None:
        AppSettingsStore(self._get_store().engine).set_claude_code_tell(choice)

    def save_claude_code_listen(self, on: bool) -> None:
        AppSettingsStore(self._get_store().engine).set_claude_code_listen(on)

    def save_claude_code_port(self, port: int) -> None:
        AppSettingsStore(self._get_store().engine).set_claude_code_port(port)

    def load_backup_copy_dir(self) -> str | None:
        """The second backup folder, "" for none, or None when never set in Settings."""
        return AppSettingsStore(self._get_store().engine).backup_copy_dir()

    def save_backup_copy_dir(self, folder: str) -> None:
        AppSettingsStore(self._get_store().engine).set_backup_copy_dir(folder)

    def load_spend_project(self) -> str | None:
        return AppSettingsStore(self._get_store().engine).spend_project()

    def save_spend_project(self, project: str | None) -> None:
        AppSettingsStore(self._get_store().engine).set_spend_project(project)

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

    def save_client_signal_id(self, person_id: UUID, signal_id: str) -> None:
        ClientStore(self._get_store().engine).set_signal_id(person_id, signal_id)

    def save_signal_person_id(self, signal_id: str) -> None:
        SignalSettingsStore(self._get_store().engine).set_allowed_signal_id(signal_id)

    def save_client_may_call(self, person_id: UUID, may_call: bool) -> None:
        ClientStore(self._get_store().engine).set_may_call(person_id, may_call)

    def remove_client(self, person_id: UUID) -> None:
        ClientStore(self._get_store().engine).remove(person_id)

    def client_conversation(self, person_id: UUID) -> UUID:
        return ClientStore(self._get_store().engine).conversation_for(person_id, datetime.now(timezone.utc))

    def close_quiet_client_conversations(self) -> int:
        return ClientStore(self._get_store().engine).close_quiet(datetime.now(timezone.utc))

    def load_feelings(self) -> list:
        """Every feeling she still noticeably has, strongest first."""
        return FeelingStore(self._get_store().engine).state(now=datetime.now(timezone.utc))

    def record_feeling(self, feeling: str, strength: str, reason: str, client_id: UUID | None = None) -> None:
        """A feeling she noted, in this conversation with the person or in a client's own chat.
        Raises FeelingLimit when the limits turn it away."""
        store = self._get_store()
        if client_id is None:
            conversation_id = self.conversation_id
            store.create_conversation(conversation_id)
        else:
            conversation_id = self.client_conversation(client_id)
        FeelingStore(store.engine).record(feeling, strength, reason, now=datetime.now(timezone.utc),
                                          conversation_id=conversation_id)

    def save_client_message(self, conversation_id: UUID, role: str, text: str, channel: str = "signal") -> None:
        ClientStore(self._get_store().engine).save(conversation_id, role, text, channel)

    def load_client_memory_context(self, person_id: UUID, name: str) -> str:
        """What Cora remembers from a client's own chats, and what the person meant for them."""
        return MemoryStore(self._get_store().engine).get_startup_context(person_id, name)

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

    def load_webhook_settings(self) -> WebhookSettings:
        return WebhookSettingsStore(self._get_store().engine).settings()

    def save_webhook_enabled(self, enabled: bool) -> None:
        WebhookSettingsStore(self._get_store().engine).set_enabled(enabled)

    def save_webhook_address(self, public_url: str, port: int) -> None:
        WebhookSettingsStore(self._get_store().engine).set_address(public_url, port)

    def save_web_enabled(self, enabled: bool) -> None:
        WebhookSettingsStore(self._get_store().engine).set_web_enabled(enabled)

    def save_phone_enabled(self, enabled: bool) -> None:
        WebhookSettingsStore(self._get_store().engine).set_phone_enabled(enabled)

    def save_twilio_account(self, account_sid: str, phone_number: str) -> None:
        WebhookSettingsStore(self._get_store().engine).set_twilio_account(account_sid, phone_number)

    def load_web_devices(self) -> list[WebDevice]:
        return WebDeviceStore(self._get_store().engine).devices()

    def add_web_device(self, name: str, token_hash: str) -> WebDevice:
        return WebDeviceStore(self._get_store().engine).add(name, token_hash)

    def find_web_device(self, token_hash: str) -> WebDevice | None:
        return WebDeviceStore(self._get_store().engine).find(token_hash)

    def remove_web_device(self, device_id: UUID) -> None:
        WebDeviceStore(self._get_store().engine).remove(device_id)

    def load_recent_person_messages(self, limit: int) -> list[tuple]:
        return recent_person_messages(self._get_store().engine, limit)

    def load_service_tools(self) -> dict[str, set[str]]:
        return ServiceSettingsStore(self._get_store().engine).enabled_tools()

    def save_service_tool(self, service: str, tool: str, enabled: bool) -> None:
        ServiceSettingsStore(self._get_store().engine).set_tool_enabled(service, tool, enabled)

    def sign_up_holding(self, site: str) -> SignUp | None:
        return SignUpStore(self._get_store().engine).holding(site, datetime.now(timezone.utc))

    def sign_ups_asked_today(self) -> int:
        return SignUpStore(self._get_store().engine).asked_today(datetime.now(timezone.utc))

    def record_sign_up(self, site: str, url: str, what: str, why: str, status: str) -> SignUp:
        return SignUpStore(self._get_store().engine).record(site, url, what, why, status, datetime.now(timezone.utc))

    def sign_ups_awaiting_mail(self) -> list[SignUp]:
        return SignUpStore(self._get_store().engine).awaiting_mail(datetime.now(timezone.utc))

    def sign_up_settled_by_mail(self, sign_up_id: UUID, note: str) -> bool:
        return SignUpStore(self._get_store().engine).settled_by_mail(sign_up_id, datetime.now(timezone.utc), note)

    def claim_stalled_sign_ups(self) -> list[SignUp]:
        return SignUpStore(self._get_store().engine).claim_stalled(datetime.now(timezone.utc))

    def load_key_details_context(self) -> str:
        return KeyDetailsStore(self._get_store().engine).get_startup_context()

    def load_sign_ups_context(self) -> str:
        return SignUpStore(self._get_store().engine).context()

    def load_desire_context(self) -> str:
        return DesireStore(self._get_store().engine).get_startup_context(now=datetime.now(timezone.utc))

    def finish_sign_up(self, sign_up_id: UUID, status: str, blocked_by: str | None, note: str | None) -> None:
        SignUpStore(self._get_store().engine).finish(
            sign_up_id, status, datetime.now(timezone.utc), blocked_by=blocked_by, note=note,
        )

    def load_last_talked_at(self) -> datetime | None:
        return self._get_store().last_message_time(self.conversation_id)

    def load_last_summary(self) -> str | None:
        return self._get_store().last_conversation_summary(self.conversation_id)

    def load_recent_plan(self) -> str | None:
        return MemoryStore(self._get_store().engine).recent_plan(now=datetime.now(timezone.utc))

    def load_expectations(self) -> list[str]:
        """What she and the person are waiting on: each open task (and who it waits on), then their
        recent plans and decisions."""
        engine = self._get_store().engine
        tasks = [f"Task: {task.goal}" + (f" (waiting on {', '.join(party.name for party in task.parties if party.name)})"
                                          if any(party.name for party in task.parties) else "")
                 for task in TaskStore(engine).open_tasks()]
        return tasks + MemoryStore(engine).expectations(now=datetime.now(timezone.utc))

    def load_dashboard_snapshot(self) -> DashboardSnapshot:
        return read_dashboard_snapshot(self._get_store().engine, datetime.now(timezone.utc))

    def save_message(self, message_id: UUID, text: str) -> None:
        store = self._get_store()
        store.create_conversation(self.conversation_id)
        store.save_message(
            self.conversation_id, message_id=message_id, role="user", text=text,
            channel="keyboard", status="completed", delivery_status="pending",
        )

    def save_chat_message(self, message_id: UUID, role: str, text: str, channel: str,
                          device: str | None = None) -> None:
        # Voice records are display chunks, not API-confirmed complete turns.
        store = self._get_store()
        store.create_conversation(self.conversation_id)
        store.save_message(
            self.conversation_id, message_id=message_id, role=role, text=text,
            channel=channel, status="completed" if channel == "screen" else "incomplete",
            **({"device": device} if device is not None else {}),
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