"""One background memory pass, independent of the UI worker framework."""

from collections.abc import Callable
from uuid import UUID

from storage.conversation_store import ConversationStore
from storage.database import build_engine
from subconscious.relationship.appraise_conversation import appraise_waiting_conversations

from .deduplicate_memory import deduplicate_memory
from .embed_memory import embed_memory
from .extract_memories import extract_memories
from .store import MemoryStore
from .summarize import summarize_conversation


def run_background_memory_pass(
    current_conversation_id: UUID, *,
    should_stop: Callable[[], bool],
    write_activity: Callable[[str, str], None],
    summaries_done: Callable[[], None] = lambda: None,
) -> None:
    # Own a separate engine so an in-flight request can finish during shutdown.
    engine = None
    try:
        if should_stop():
            return
        engine = build_engine()
        store = ConversationStore(engine)
        for conversation_id in store.get_unsummarized_conversation_ids(current_conversation_id):
            if should_stop():
                break
            memories = extract_memories(store, conversation_id)
            if should_stop():
                break
            write_activity(
                f"Memories extracted: conversation_id={conversation_id}, count={len(memories)}",
                "Memory extraction complete",
            )
            # Save the summary last: even zero-memory chats then leave this queue.
            summarize_conversation(store, conversation_id)
            if not should_stop():
                write_activity(
                    f"Summary saved: conversation_id={conversation_id}", "Memory summary saved",
                )
        # The launch greeting waits for this rather than for embeddings and deduplication.
        summaries_done()
        if should_stop():
            return
        memory_store = MemoryStore(engine)
        for memory_id in memory_store.get_pending_embedding_ids():
            if should_stop():
                return
            embed_memory(memory_store, memory_id)
            if not should_stop():
                write_activity(
                    f"Memory embedded: memory_id={memory_id}", "Memory embedding saved",
                )
        for memory_id in memory_store.get_pending_deduplication_ids():
            if should_stop():
                return
            duplicate = deduplicate_memory(memory_store, memory_id)
            if not should_stop():
                write_activity(
                    f"Memory checked: memory_id={memory_id}, duplicate={duplicate}",
                    "Memory deduplication checked",
                )
        # Last, so a first-run backfill never delays memories; it reports its own failures.
        if not should_stop():
            appraise_waiting_conversations(engine, should_stop=should_stop, write_activity=write_activity)
    except Exception as error:
        if not should_stop():
            write_activity(
                f"Background memory pass stopped ({type(error).__name__}).", "Memory processing failed",
            )
    finally:
        if engine is not None:
            engine.dispose()