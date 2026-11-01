"""Conversation and memory mappings; SQL migrations create the tables."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Double, ForeignKey, Identity, Index, Integer, LargeBinary,
    SmallInteger, Text, UniqueConstraint,
    Uuid,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MEMORY_EMBEDDING_MODEL = "text-embedding-3-small"
MEMORY_EMBEDDING_DIMENSIONS = 1536


class Base(DeclarativeBase):
    pass


class ConversationRecord(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'closed', 'interrupted')", name="conversations_status_check"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="conversations_times_check"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="active")
    # None for the primary person; a client's id for their own Signal chats.
    person_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("public.people.id", ondelete="RESTRICT"))

    messages: Mapped[list[MessageRecord]] = relationship(
        back_populates="conversation", order_by="MessageRecord.sequence_number",
        passive_deletes="all",
    )


class MessageRecord(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence_number", name="messages_conversation_sequence_key"),
        CheckConstraint("sequence_number > 0", name="messages_sequence_check"),
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
        CheckConstraint("channel IN ('voice', 'keyboard', 'screen', 'signal', 'email')", name="messages_channel_check"),
        CheckConstraint("status IN ('incomplete', 'completed', 'interrupted')", name="messages_status_check"),
        CheckConstraint(
            "delivery_status IN ('not_applicable', 'pending', 'sent', 'failed')",
            name="messages_delivery_status_check",
        ),
        CheckConstraint("btrim(text) <> ''", name="messages_text_check"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.conversations.id", name="messages_conversation_id_fkey", ondelete="RESTRICT")
    )
    sequence_number: Mapped[int]
    role: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    status: Mapped[str] = mapped_column(Text, server_default="incomplete")
    delivery_status: Mapped[str] = mapped_column(Text, server_default="not_applicable")

    conversation: Mapped[ConversationRecord] = relationship(back_populates="messages")


class ConversationSummaryRecord(Base):
    __tablename__ = "conversation_summaries"
    __table_args__ = (
        CheckConstraint("btrim(text) <> ''", name="conversation_summaries_text_check"),
        {"schema": "public"},
    )

    # The conversation UUID is also the primary key: one summary per discussion.
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.conversations.id", name="conversation_summaries_conversation_id_fkey", ondelete="RESTRICT"),
        primary_key=True,
    )
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class MemoryRecord(Base):
    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("source_message_id", "kind", "text", name="memories_source_kind_text_key"),
        CheckConstraint("kind IN ('fact', 'preference', 'decision', 'task')", name="memories_kind_check"),
        CheckConstraint("btrim(text) <> '' AND char_length(text) <= 500", name="memories_text_check"),
        CheckConstraint("btrim(evidence) <> ''", name="memories_evidence_check"),
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL AND embedding_created_at IS NULL) "
            "OR (embedding IS NOT NULL AND embedding_model IS NOT NULL "
            "AND embedding_model = 'text-embedding-3-small' AND embedding_created_at IS NOT NULL)",
            name="memories_embedding_metadata_check",
        ),
        CheckConstraint("embedding IS NULL OR vector_norm(embedding) > 0", name="memories_embedding_nonzero_check"),
        CheckConstraint(
            "duplicate_of IS NULL OR (duplicate_of <> id AND dedup_checked_at IS NOT NULL)",
            name="memories_duplicate_check",
        ),
        CheckConstraint(
            "corrects IS NULL OR (corrects <> id AND correction_checked_at IS NOT NULL)",
            name="memories_correction_check",
        ),
        CheckConstraint("fades_at IS NULL OR fade_checked_at IS NOT NULL", name="memories_fade_check"),
        CheckConstraint(
            "(share_audience IS NULL AND shared_with_name IS NULL) "
            "OR (share_audience = 'anyone' AND shared_with_name IS NULL AND share_checked_at IS NOT NULL) "
            "OR (share_audience = 'person' AND btrim(shared_with_name) <> '' AND share_checked_at IS NOT NULL)",
            name="memories_share_check",
        ),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    source_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.messages.id", name="memories_source_message_id_fkey", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(MEMORY_EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_of: Mapped[UUID | None] = mapped_column(
        ForeignKey("public.memories.id", name="memories_duplicate_of_fkey", ondelete="RESTRICT"),
    )
    dedup_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corrects: Mapped[UUID | None] = mapped_column(
        ForeignKey("public.memories.id", name="memories_corrects_fkey", ondelete="RESTRICT"),
    )
    correction_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fades_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fade_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Whether the person meant it to reach a client: 'anyone', or 'person' with the name they used.
    share_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    share_audience: Mapped[str | None] = mapped_column(Text)
    shared_with_name: Mapped[str | None] = mapped_column(Text)


class PersonRecord(Base):
    """Someone Cora talks with; display_name is NULL until she is told it."""

    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint(
            "display_name IS NULL OR (display_name = btrim(display_name) "
            "AND display_name <> '' AND char_length(display_name) <= 100)",
            name="people_display_name_check",
        ),
        CheckConstraint("kind IN ('primary', 'client')", name="people_kind_check"),
        CheckConstraint("kind = 'primary' OR display_name IS NOT NULL", name="people_client_named_check"),
        UniqueConstraint("signal_number", name="people_signal_number_key"),
        UniqueConstraint("signal_id", name="people_signal_id_key"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    display_name: Mapped[str | None] = mapped_column(Text)
    first_met_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    # 'primary' for the person Cora works with; 'client' for others she may talk with on Signal.
    kind: Mapped[str] = mapped_column(Text, server_default="primary")
    signal_number: Mapped[str | None] = mapped_column(Text)
    # Their Signal ID once learned: still identifies them if they hide their number.
    signal_id: Mapped[str | None] = mapped_column(Text)


# The person Cora works with, never a client; and conversations with them, never a client's.
PRIMARY_PERSON = PersonRecord.kind == "primary"
WITH_PRIMARY_PERSON = ConversationRecord.person_id.is_(None)


class InterestRecord(Base):
    """One of Cora's own interests; strength is a snapshot taken at strength_at."""

    __tablename__ = "interests"
    __table_args__ = (
        CheckConstraint("btrim(title) <> '' AND char_length(title) <= 200", name="interests_title_check"),
        CheckConstraint(
            "btrim(description) <> '' AND char_length(description) <= 1000", name="interests_description_check",
        ),
        CheckConstraint(
            "btrim(why_emerged) <> '' AND char_length(why_emerged) <= 1000", name="interests_why_emerged_check",
        ),
        CheckConstraint("strength >= 0 AND strength <= 1", name="interests_strength_check"),
        CheckConstraint("status IN ('emerging', 'active', 'dormant')", name="interests_status_check"),
        CheckConstraint(
            "parent_interest_id IS NULL OR parent_interest_id <> id", name="interests_parent_check",
        ),
        CheckConstraint("origin IN ('conversation', 'own')", name="interests_origin_check"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    why_emerged: Mapped[str] = mapped_column(Text)
    strength: Mapped[float] = mapped_column(Double)
    strength_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    parent_interest_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("public.interests.id", name="interests_parent_interest_id_fkey", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    last_reinforced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 'conversation': sparked by the person; 'own': by something she came across exploring.
    origin: Mapped[str] = mapped_column(Text, default="conversation", server_default="conversation")


class InterestEventRecord(Base):
    """Append-only history of an interest changing; strength_before is NULL only for spawn."""

    __tablename__ = "interest_events"
    __table_args__ = (
        CheckConstraint("kind IN ('spawn', 'reinforce', 'fade', 'revive')", name="interest_events_kind_check"),
        CheckConstraint("strength_after >= 0 AND strength_after <= 1", name="interest_events_strength_check"),
        CheckConstraint(
            "(kind = 'spawn') = (strength_before IS NULL) "
            "AND (strength_before IS NULL OR (strength_before >= 0 AND strength_before <= 1))",
            name="interest_events_before_check",
        ),
        CheckConstraint("btrim(reason) <> '' AND char_length(reason) <= 1000", name="interest_events_reason_check"),
        Index("interest_events_interest_id_created_at_idx", "interest_id", "created_at"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    interest_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.interests.id", name="interest_events_interest_id_fkey", ondelete="RESTRICT"),
    )
    kind: Mapped[str] = mapped_column(Text)
    strength_before: Mapped[float | None] = mapped_column(Double)
    strength_after: Mapped[float] = mapped_column(Double)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class InterestEventMemoryRecord(Base):
    """Links an interest event to the memories that informed it."""

    __tablename__ = "interest_event_memories"
    __table_args__ = {"schema": "public"}

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.interest_events.id", name="interest_event_memories_event_id_fkey", ondelete="RESTRICT"),
        primary_key=True,
    )
    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.memories.id", name="interest_event_memories_memory_id_fkey", ondelete="CASCADE"),
        primary_key=True,
    )


class InterestWorkerStateRecord(Base):
    """At most one row: how far the interests worker has read through memories."""

    __tablename__ = "interest_worker_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="interest_worker_state_singleton_check"),
        CheckConstraint(
            "(memory_cursor_created_at IS NULL) = (memory_cursor_id IS NULL)",
            name="interest_worker_state_cursor_check",
        ),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default="1")
    memory_cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_cursor_id: Mapped[UUID | None] = mapped_column(Uuid)
    last_evolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class InterestExplorationRecord(Base):
    """One attempt by Cora to explore an interest; only 'found' attempts carry a finding."""

    __tablename__ = "interest_explorations"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'found', 'failed')", name="interest_explorations_status_check"),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL) "
            "AND (finished_at IS NULL OR finished_at >= started_at)",
            name="interest_explorations_finished_check",
        ),
        CheckConstraint(
            "(status = 'found') = (finding IS NOT NULL) "
            "AND (finding IS NULL OR (btrim(finding) <> '' AND char_length(finding) <= 2000))",
            name="interest_explorations_finding_check",
        ),
        CheckConstraint(
            "cardinality(next_questions) <= 3 AND (status = 'found' OR cardinality(next_questions) = 0)",
            name="interest_explorations_next_questions_check",
        ),
        CheckConstraint(
            "cardinality(sources) <= 10 AND (status = 'found' OR cardinality(sources) = 0)",
            name="interest_explorations_sources_check",
        ),
        CheckConstraint("mentioned_at IS NULL OR status = 'found'", name="interest_explorations_mentioned_check"),
        CheckConstraint(
            "reaction IS NULL OR (status = 'found' AND btrim(reaction) <> '' AND char_length(reaction) <= 500)",
            name="interest_explorations_reaction_check",
        ),
        CheckConstraint(
            "jsonb_typeof(tangents) = 'array' AND jsonb_array_length(tangents) <= 2 "
            "AND (status = 'found' OR jsonb_array_length(tangents) = 0)",
            name="interest_explorations_tangents_check",
        ),
        CheckConstraint(
            "pull IS NULL OR (status = 'found' AND pull IN ('none', 'some', 'strong'))",
            name="interest_explorations_pull_check",
        ),
        CheckConstraint(
            "kind IN ('interest', 'wander') AND (kind = 'interest') = (interest_id IS NOT NULL)",
            name="interest_explorations_kind_check",
        ),
        CheckConstraint(
            "(kind = 'wander' OR (topic IS NULL AND wandered_from IS NULL)) "
            "AND (topic IS NULL OR (btrim(topic) <> '' AND char_length(topic) <= 200)) "
            "AND (kind = 'interest' OR status <> 'found' OR topic IS NOT NULL)",
            name="interest_explorations_topic_check",
        ),
        CheckConstraint(
            "(reach_out IS NULL) = (reach_out_why IS NULL) "
            "AND (reach_out IS NULL OR (status = 'found' AND btrim(reach_out) <> '' "
            "AND char_length(reach_out) <= 600 AND btrim(reach_out_why) <> '' "
            "AND char_length(reach_out_why) <= 500)) "
            "AND (reached_out_at IS NULL OR reach_out IS NOT NULL)",
            name="interest_explorations_reach_out_check",
        ),
        Index("interest_explorations_started_at_idx", "started_at"),
        Index("interest_explorations_interest_id_started_at_idx", "interest_id", "started_at"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    # None for a wander, which isn't tied to any interest.
    interest_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("public.interests.id", name="interest_explorations_interest_id_fkey", ondelete="RESTRICT"),
    )
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finding: Mapped[str | None] = mapped_column(Text)
    next_questions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    mentioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reaction: Mapped[str | None] = mapped_column(Text)
    # Up to two other things she noticed and would like to look into: {"title", "why"} each.
    tangents: Mapped[list[dict]] = mapped_column(JSONB, default=list, server_default=sql_text("'[]'::jsonb"))
    pull: Mapped[str | None] = mapped_column(Text)  # none, some or strong: how much she wants to keep going
    sparks_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(Text, default="interest", server_default="interest")  # or 'wander'
    topic: Mapped[str | None] = mapped_column(Text)  # what a wander was about
    # The exploration whose tangent a wander followed.
    wandered_from: Mapped[UUID | None] = mapped_column(
        ForeignKey("public.interest_explorations.id", name="interest_explorations_wandered_from_fkey",
                   ondelete="RESTRICT"),
    )
    # A message for the person when something really excited her, and why; sent at reached_out_at.
    reach_out: Mapped[str | None] = mapped_column(Text)
    reach_out_why: Mapped[str | None] = mapped_column(Text)
    reached_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Explored with her own browser rather than web search.
    browsed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"))


class IdentityTraitRecord(Base):
    """A trait Cora formed from experience; held while active with confidence >= 0.5."""

    __tablename__ = "identity_traits"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('value', 'preference', 'perspective', 'tendency')", name="identity_traits_kind_check",
        ),
        CheckConstraint(
            "btrim(statement) <> '' AND char_length(statement) <= 300", name="identity_traits_statement_check",
        ),
        CheckConstraint(
            "(kind = 'preference') = (polarity IS NOT NULL) "
            "AND (kind = 'preference') = (subject IS NOT NULL) "
            "AND (polarity IS NULL OR polarity IN ('like', 'dislike')) "
            "AND (subject IS NULL OR (btrim(subject) <> '' AND char_length(subject) <= 200))",
            name="identity_traits_preference_check",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="identity_traits_confidence_check"),
        CheckConstraint("status IN ('active', 'retired')", name="identity_traits_status_check"),
        CheckConstraint(
            "(status = 'retired') = (retired_at IS NOT NULL) "
            "AND (retired_at IS NULL OR retired_at >= created_at)",
            name="identity_traits_retired_check",
        ),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    kind: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    polarity: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdentityEventRecord(Base):
    """Append-only history of a trait; statement is its wording after the event."""

    __tablename__ = "identity_events"
    __table_args__ = (
        CheckConstraint("kind IN ('form', 'revise', 'retire')", name="identity_events_kind_check"),
        CheckConstraint(
            "btrim(statement) <> '' AND char_length(statement) <= 300", name="identity_events_statement_check",
        ),
        CheckConstraint(
            "confidence_after >= 0 AND confidence_after <= 1", name="identity_events_confidence_check",
        ),
        CheckConstraint(
            "(kind = 'form') = (confidence_before IS NULL) "
            "AND (confidence_before IS NULL OR (confidence_before >= 0 AND confidence_before <= 1))",
            name="identity_events_before_check",
        ),
        CheckConstraint("btrim(reason) <> '' AND char_length(reason) <= 1000", name="identity_events_reason_check"),
        Index("identity_events_trait_id_created_at_idx", "trait_id", "created_at"),
        Index("identity_events_created_at_idx", "created_at"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    trait_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.identity_traits.id", name="identity_events_trait_id_fkey", ondelete="RESTRICT"),
    )
    kind: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    confidence_before: Mapped[float | None] = mapped_column(Double)
    confidence_after: Mapped[float] = mapped_column(Double)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class IdentityEventTraitRecord(Base):
    """Links an identity event to other traits it relied on."""

    __tablename__ = "identity_event_traits"
    __table_args__ = {"schema": "public"}

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.identity_events.id", name="identity_event_traits_event_id_fkey", ondelete="RESTRICT"),
        primary_key=True,
    )
    trait_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.identity_traits.id", name="identity_event_traits_trait_id_fkey", ondelete="RESTRICT"),
        primary_key=True,
    )


class IdentityEventEvidenceRecord(Base):
    """One piece of experience an identity event relied on: exactly one source per row."""

    __tablename__ = "identity_event_evidence"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(interest_event_id, exploration_id, conversation_id) = 1",
            name="identity_event_evidence_source_check",
        ),
        Index("identity_event_evidence_event_id_idx", "event_id"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.identity_events.id", name="identity_event_evidence_event_id_fkey", ondelete="RESTRICT"),
    )
    interest_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "public.interest_events.id", name="identity_event_evidence_interest_event_id_fkey", ondelete="RESTRICT",
        ),
    )
    exploration_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "public.interest_explorations.id", name="identity_event_evidence_exploration_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "public.conversation_summaries.conversation_id", name="identity_event_evidence_conversation_id_fkey",
            ondelete="RESTRICT",
        ),
    )


class IdentityWorkerStateRecord(Base):
    """At most one row: when Cora last reflected on who she is."""

    __tablename__ = "identity_worker_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="identity_worker_state_singleton_check"),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default="1")
    last_reflected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class InteractionAppraisalRecord(Base):
    """How one finished conversation went with the person; 'development' marks working on Cora herself."""

    __tablename__ = "interaction_appraisals"
    __table_args__ = (
        CheckConstraint("setting IN ('everyday', 'development')", name="interaction_appraisals_setting_check"),
        CheckConstraint("tone IN ('warm', 'neutral', 'curt', 'hostile')", name="interaction_appraisals_tone_check"),
        CheckConstraint(
            "friction IN ('none', 'at_situation', 'at_cora')", name="interaction_appraisals_friction_check",
        ),
        CheckConstraint(
            "repair IN ('none', 'person', 'cora', 'both') "
            "AND (repair NOT IN ('person', 'both') OR friction = 'at_cora') "
            "AND (repair NOT IN ('cora', 'both') OR cora_mistake)",
            name="interaction_appraisals_repair_check",
        ),
        CheckConstraint(
            "btrim(reason) <> '' AND char_length(reason) <= 1000", name="interaction_appraisals_reason_check",
        ),
        Index("interaction_appraisals_person_id_appraised_at_idx", "person_id", "appraised_at"),
        {"schema": "public"},
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.conversations.id", name="interaction_appraisals_conversation_id_fkey", ondelete="RESTRICT"),
        primary_key=True,
    )
    person_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.people.id", name="interaction_appraisals_person_id_fkey", ondelete="RESTRICT"),
    )
    setting: Mapped[str] = mapped_column(Text)
    tone: Mapped[str] = mapped_column(Text)
    friction: Mapped[str] = mapped_column(Text)
    cora_mistake: Mapped[bool] = mapped_column(Boolean)
    repair: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    appraised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"),
    )


class RelationshipRecord(Base):
    """How Cora currently feels about one person; each value runs from 0 to 1."""

    __tablename__ = "relationships"
    __table_args__ = (
        CheckConstraint(
            "trust >= 0 AND trust <= 1 AND warmth >= 0 AND warmth <= 1 "
            "AND familiarity >= 0 AND familiarity <= 1",
            name="relationships_values_check",
        ),
        {"schema": "public"},
    )

    person_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.people.id", name="relationships_person_id_fkey", ondelete="RESTRICT"),
        primary_key=True,
    )
    trust: Mapped[float] = mapped_column(Double)
    warmth: Mapped[float] = mapped_column(Double)
    familiarity: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class RelationshipEventRecord(Base):
    """Append-only relationship history; values are the state after the event, so each is a snapshot."""

    __tablename__ = "relationship_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="relationship_events_conversation_id_key"),
        CheckConstraint(
            "kind IN ('appraisal', 'reset') "
            "AND (kind = 'appraisal') = (conversation_id IS NOT NULL) "
            "AND (kind = 'reset') = (reason IS NOT NULL)",
            name="relationship_events_kind_check",
        ),
        CheckConstraint(
            "trust >= 0 AND trust <= 1 AND warmth >= 0 AND warmth <= 1 "
            "AND familiarity >= 0 AND familiarity <= 1",
            name="relationship_events_values_check",
        ),
        CheckConstraint(
            "reason IS NULL OR (btrim(reason) <> '' AND char_length(reason) <= 1000)",
            name="relationship_events_reason_check",
        ),
        Index("relationship_events_person_id_created_at_idx", "person_id", "created_at"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    person_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.relationships.person_id", name="relationship_events_person_id_fkey", ondelete="RESTRICT"),
    )
    kind: Mapped[str] = mapped_column(Text)
    # The appraisal this event applied; each appraisal is applied at most once.
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "public.interaction_appraisals.conversation_id", name="relationship_events_conversation_id_fkey",
            ondelete="RESTRICT",
        ),
    )
    trust: Mapped[float] = mapped_column(Double)
    warmth: Mapped[float] = mapped_column(Double)
    familiarity: Mapped[float] = mapped_column(Double)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class ModelSettingRecord(Base):
    """The model chosen for one area of Cora; areas without a row use the default."""

    __tablename__ = "model_settings"
    __table_args__ = (
        CheckConstraint(
            "area IN ('delegation', 'memory', 'subconscious', 'utility')", name="model_settings_area_check",
        ),
        CheckConstraint("btrim(model) <> ''", name="model_settings_model_check"),
        {"schema": "public"},
    )

    area: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class ServiceSettingRecord(Base):
    """Whether one outside service is switched on; services without a row are off."""

    __tablename__ = "service_settings"
    __table_args__ = (
        CheckConstraint("btrim(service) <> ''", name="service_settings_service_check"),
        {"schema": "public"},
    )

    service: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class SignalSettingRecord(Base):
    """Cora's own Signal account and who she answers on it; at most one row."""

    __tablename__ = "signal_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="signal_settings_single_row"),
        CheckConstraint("account IS NULL OR account <> allowed_number", name="signal_settings_numbers_differ"),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=sql_text("1"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    account: Mapped[str | None] = mapped_column(Text)
    allowed_number: Mapped[str | None] = mapped_column(Text)
    allowed_signal_id: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class WebhookSettingRecord(Base):
    """Whether Cora listens for webhooks, the public address that reaches her, and her local port; at most one row."""

    __tablename__ = "webhook_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="webhook_settings_single_row"),
        CheckConstraint("public_url IS NULL OR public_url LIKE 'https://%'", name="webhook_settings_https"),
        CheckConstraint("port BETWEEN 1024 AND 65535", name="webhook_settings_port"),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=sql_text("1"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    public_url: Mapped[str | None] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer, server_default=sql_text("8000"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class SelfImageRecord(Base):
    """An image Cora chose to represent her, and her own description of it."""

    __tablename__ = "self_image"
    __table_args__ = (
        CheckConstraint("kind IN ('self', 'work')", name="self_image_kind_check"),
        CheckConstraint("btrim(description) <> '' AND char_length(description) <= 1000",
                        name="self_image_description_check"),
        CheckConstraint("(image IS NULL) = (image_type IS NULL)", name="self_image_image_check"),
        {"schema": "public"},
    )

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    image: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_type: Mapped[str | None] = mapped_column(Text)
    chosen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class ImpressionRecord(Base):
    """Cora's own view of someone she talks with as a client, and a one-line opinion fit for anyone."""

    __tablename__ = "impressions"
    __table_args__ = (
        CheckConstraint("btrim(impression) <> '' AND char_length(impression) <= 1200",
                        name="impressions_impression_check"),
        CheckConstraint("btrim(opinion) <> '' AND char_length(opinion) <= 200", name="impressions_opinion_check"),
        {"schema": "public"},
    )

    person_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.people.id", name="impressions_person_id_fkey", ondelete="CASCADE"), primary_key=True,
    )
    impression: Mapped[str] = mapped_column(Text)
    opinion: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


class ImpressionUpdateRecord(Base):
    """A finished client conversation that has shaped their impression."""

    __tablename__ = "impression_updates"
    __table_args__ = (
        CheckConstraint("conduct IS NULL OR conduct IN ('fine', 'rude', 'abusive', 'threatening')",
                        name="impression_updates_conduct_check"),
        CheckConstraint(
            "(conduct_note IS NULL) = (conduct IS NULL OR conduct = 'fine') "
            "AND (conduct_note IS NULL OR (btrim(conduct_note) <> '' AND char_length(conduct_note) <= 500))",
            name="impression_updates_conduct_note_check",
        ),
        {"schema": "public"},
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.conversations.id", name="impression_updates_conversation_id_fkey", ondelete="CASCADE"),
        primary_key=True,
    )
    person_id: Mapped[UUID] = mapped_column(
        ForeignKey("public.people.id", name="impression_updates_person_id_fkey", ondelete="CASCADE"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    # How they treated her; anything but 'fine' carries her note and is reported to the person.
    conduct: Mapped[str | None] = mapped_column(Text)
    conduct_note: Mapped[str | None] = mapped_column(Text)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskRecord(Base):
    """An errand for the person that needs other people: their goal in their words, and how it stands."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("btrim(goal) <> '' AND char_length(goal) <= 1000", name="tasks_goal_check"),
        CheckConstraint("status IN ('open', 'waiting', 'needs_person', 'done', 'cancelled')", name="tasks_status_check"),
        CheckConstraint("result IS NULL OR char_length(result) <= 4000", name="tasks_result_check"),
        CheckConstraint("(closed_at IS NOT NULL) = (status IN ('done', 'cancelled'))", name="tasks_closed_check"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="waiting")
    result: Mapped[str | None] = mapped_column(Text)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When she told the person the deadline was a day away, and that it had passed: once each.
    deadline_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskPartyRecord(Base):
    """Someone a task needs, with their brief: what to find out or agree, and what she may share."""

    __tablename__ = "task_parties"
    __table_args__ = (
        CheckConstraint("channel IN ('signal', 'email')", name="task_parties_channel_check"),
        CheckConstraint(
            "(channel = 'signal' AND person_id IS NOT NULL AND address IS NULL) "
            "OR (channel = 'email' AND person_id IS NULL AND btrim(address) <> '' AND char_length(address) <= 320)",
            name="task_parties_who_check",
        ),
        CheckConstraint("btrim(brief) <> '' AND char_length(brief) <= 2000", name="task_parties_brief_check"),
        CheckConstraint("status IN ('waiting', 'done')", name="task_parties_status_check"),
        CheckConstraint("nudges >= 0", name="task_parties_nudges_check"),
        CheckConstraint("outcome IS NULL OR char_length(outcome) <= 3000", name="task_parties_outcome_check"),
        CheckConstraint("name IS NULL OR (btrim(name) <> '' AND char_length(name) <= 100)", name="task_parties_name_check"),
        CheckConstraint("thread_id IS NULL OR char_length(thread_id) <= 500", name="task_parties_thread_check"),
        UniqueConstraint("task_id", "person_id", name="task_parties_person_key"),
        UniqueConstraint("task_id", "address", name="task_parties_address_key"),
        Index("task_parties_person_id_idx", "person_id", postgresql_where=sql_text("person_id IS NOT NULL")),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("public.tasks.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("public.people.id", ondelete="CASCADE"))
    address: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="waiting")
    contacted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    last_heard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nudges: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    nudged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text)
    # Email parties only: who they are, as she knows them, and her email thread with them.
    name: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)


class TaskStepRecord(Base):
    """One thing that happened in a task, in order."""

    __tablename__ = "task_steps"
    __table_args__ = (
        CheckConstraint("btrim(text) <> '' AND char_length(text) <= 4000", name="task_steps_text_check"),
        Index("task_steps_task_id_idx", "task_id", "id"),
        {"schema": "public"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("public.tasks.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    text: Mapped[str] = mapped_column(Text)


class ServiceToolRecord(Base):
    """One service tool Cora may use; tools without a row are off."""

    __tablename__ = "service_tools"
    __table_args__ = (
        CheckConstraint("btrim(service) <> ''", name="service_tools_service_check"),
        CheckConstraint("btrim(tool) <> ''", name="service_tools_tool_check"),
        {"schema": "public"},
    )

    service: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))

class SignUpRecord(Base):
    """A site Cora asked to sign up to, for an account of her own, and how it went."""

    __tablename__ = "sign_ups"
    __table_args__ = (
        CheckConstraint("btrim(site) <> '' AND char_length(site) <= 253", name="sign_ups_site_check"),
        CheckConstraint("url LIKE 'http%' AND char_length(url) <= 2000", name="sign_ups_url_check"),
        CheckConstraint("btrim(what) <> '' AND char_length(what) <= 300", name="sign_ups_what_check"),
        CheckConstraint("btrim(why) <> '' AND char_length(why) <= 500", name="sign_ups_why_check"),
        CheckConstraint(
            "status IN ('declined', 'timed_out', 'signing_up', 'confirm_email', 'done', 'blocked', 'failed')",
            name="sign_ups_status_check",
        ),
        CheckConstraint(
            "(blocked_by IS NOT NULL) = (status = 'blocked') AND (blocked_by IS NULL OR blocked_by IN "
            "('captcha', 'phone', 'payment', 'personal_details', 'other'))",
            name="sign_ups_blocked_by_check",
        ),
        CheckConstraint("note IS NULL OR char_length(note) <= 1000", name="sign_ups_note_check"),
        Index("sign_ups_one_account_per_site_idx", "site", unique=True,
              postgresql_where=sql_text("status IN ('signing_up', 'confirm_email', 'done')")),
        Index("sign_ups_asked_at_idx", "asked_at"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    site: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    what: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    blocked_by: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
