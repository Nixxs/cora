"""Conversation and memory mappings; SQL migrations create the tables."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Double, ForeignKey, Index, SmallInteger, Text, UniqueConstraint, Uuid
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY
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
        CheckConstraint("channel IN ('voice', 'keyboard', 'screen')", name="messages_channel_check"),
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


class PersonRecord(Base):
    """Someone Cora talks with; display_name is NULL until she is told it."""

    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint(
            "display_name IS NULL OR (display_name = btrim(display_name) "
            "AND display_name <> '' AND char_length(display_name) <= 100)",
            name="people_display_name_check",
        ),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    display_name: Mapped[str | None] = mapped_column(Text)
    first_met_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"))


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
        Index("interest_explorations_started_at_idx", "started_at"),
        Index("interest_explorations_interest_id_started_at_idx", "interest_id", "started_at"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, server_default=sql_text("gen_random_uuid()"))
    interest_id: Mapped[UUID] = mapped_column(
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