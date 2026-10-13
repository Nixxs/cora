"""Conversation and memory mappings; SQL migrations create the tables."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, Uuid
from sqlalchemy import text as sql_text
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