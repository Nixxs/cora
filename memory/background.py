"""One background memory pass, independent of the UI worker framework."""

from collections.abc import Callable
from uuid import UUID

from storage.conversation_store import ConversationStore
from storage.database import build_engine
from storage.model_settings_store import ModelSettingsStore
from subconscious.impressions.form_impression import update_waiting_impressions
from subconscious.relationship.appraise_conversation import appraise_waiting_conversations
from subconscious.relationship.relationship_store import update_relationships

from .correct_memory import correct_memory
from .deduplicate_memory import deduplicate_memory
from .embed_memory import embed_memory
from .extract_memories import extract_memories
from .fade_memory import fade_memory
from .share_memory import share_memory
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
        # Read once per pass, so a change in settings applies from the next pass.
        models = ModelSettingsStore(engine).models()
        store = ConversationStore(engine)
        for conversation_id in store.get_unsummarized_conversation_ids(current_conversation_id):
            if should_stop():
                break
            memories = extract_memories(store, conversation_id, model=models["memory"])
            if should_stop():
                break
            write_activity(
                f"Memories extracted: conversation_id={conversation_id}, count={len(memories)}",
                "Memory extraction complete",
            )
            # Save the summary last: even zero-memory chats then leave this queue.
            summarize_conversation(store, conversation_id, model=models["memory"])
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
            duplicate = deduplicate_memory(memory_store, memory_id, model=models["utility"])
            if not should_stop():
                write_activity(
                    f"Memory checked: memory_id={memory_id}, duplicate={duplicate}",
                    "Memory deduplication checked",
                )
        for memory_id in memory_store.get_pending_correction_ids():
            if should_stop():
                return
            corrects = correct_memory(memory_store, memory_id, model=models["utility"])
            if not should_stop():
                write_activity(
                    f"Memory correction checked: memory_id={memory_id}, corrects={corrects}",
                    "Memory correction checked",
                )
        for memory_id in memory_store.get_pending_fade_ids():
            if should_stop():
                return
            fades_at = fade_memory(memory_store, memory_id, model=models["utility"])
            if not should_stop():
                write_activity(
                    f"Memory fade checked: memory_id={memory_id}, "
                    f"fades_at={None if fades_at is None else fades_at.isoformat()}",
                    "Memory fade checked",
                )
        for memory_id in memory_store.get_pending_share_ids():
            if should_stop():
                return
            audience, _ = share_memory(memory_store, memory_id, model=models["utility"])
            if not should_stop():
                # Who it was meant for stays out of Activity; only whether it was meant for anyone.
                write_activity(
                    f"Memory share checked: memory_id={memory_id}, shared={audience is not None}",
                    "Memory share checked",
                )
        # Last, so a first-run backfill never delays memories; it reports its own failures.
        if not should_stop():
            appraise_waiting_conversations(
                engine, model=models["subconscious"], should_stop=should_stop, write_activity=write_activity,
            )
        # Applies whatever appraisals are saved, even if appraising stopped early.
        if not should_stop():
            update_relationships(engine, write_activity=write_activity)
        # Her view of each client, from their finished chats; the person's relationship is above.
        if not should_stop():
            update_waiting_impressions(
                engine, model=models["subconscious"], should_stop=should_stop, write_activity=write_activity,
            )
    except Exception as error:
        if not should_stop():
            write_activity(
                f"Background memory pass stopped ({type(error).__name__}).", "Memory processing failed",
            )
    finally:
        if engine is not None:
            engine.dispose()