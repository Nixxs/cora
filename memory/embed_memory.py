"""Manually generate an embedding for one existing memory, not a whole chat."""

import argparse
import sys
from uuid import UUID

from dotenv import load_dotenv
from openai import OpenAI

from storage.database import build_engine
from .store import MemoryStore, validate_memory_embedding
from storage.models import MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL, MemoryRecord


def generate_embedding(client: OpenAI, text: str) -> list[float]:
    response = client.embeddings.create(
        model=MEMORY_EMBEDDING_MODEL, input=text,
        dimensions=MEMORY_EMBEDDING_DIMENSIONS, encoding_format="float",
    )
    if response.model != MEMORY_EMBEDDING_MODEL or len(response.data) != 1 or response.data[0].index != 0:
        raise ValueError("Unexpected embedding response")
    return validate_memory_embedding(response.data[0].embedding)


def embed_memory(store: MemoryStore, memory_id: UUID, *, client: OpenAI | None = None) -> MemoryRecord:
    memory = store.get_memory(memory_id)
    if memory is None:
        raise LookupError("Memory does not exist")
    if memory.embedding is not None:
        if memory.embedding_model != MEMORY_EMBEDDING_MODEL:
            raise ValueError("Memory uses a different embedding model")
        return memory
    # The read session is closed before requesting; send only this memory's text.
    if client is None:
        with OpenAI(max_retries=0) as api:
            vector = generate_embedding(api, memory.text)
    else:
        vector = generate_embedding(client, memory.text)
    return store.save_embedding(memory_id, memory.text, vector)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and save one memory embedding (billable unless cached).")
    parser.add_argument("memory_id", type=UUID, help="An existing memories.id UUID, not a conversation UUID")
    args = parser.parse_args()
    load_dotenv()
    try:
        engine = build_engine()
        try:
            memory = embed_memory(MemoryStore(engine), args.memory_id)
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Embedding not confirmed ({type(error).__name__}). Check memory, database and API settings.")
    print(f"Embedding ready: memory_id={memory.id}, model={memory.embedding_model}, dimensions={MEMORY_EMBEDDING_DIMENSIONS}")


if __name__ == "__main__":
    main()