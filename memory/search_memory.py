"""Manually search embedded memories; this is not yet an application tool."""

import argparse
from datetime import timezone
import sys

from dotenv import load_dotenv
from openai import OpenAI

from .embed_memory import generate_embedding
from storage.database import build_engine
from .store import MemoryMatch, MemoryStore, validate_search_limit


def search_memory(
    store: MemoryStore, query: str, *, limit: int = 8, client: OpenAI | None = None,
) -> list[MemoryMatch]:
    validate_search_limit(limit)
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > 1000:
        raise ValueError("Memory search query must contain 1 to 1000 characters")
    # Only the question goes to OpenAI; stored memories are compared locally.
    if client is None:
        with OpenAI(max_retries=0) as api:
            vector = generate_embedding(api, query.strip())
    else:
        vector = generate_embedding(client, query.strip())
    return store.search_memories(vector, limit)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search embedded memories (one billable query-embedding request).")
    parser.add_argument("query", help="A search question, enclosed in quotes")
    parser.add_argument("--limit", type=int, default=8, help="Maximum results, 1 to 20 (default: 8)")
    args = parser.parse_args()
    load_dotenv()
    try:
        engine = build_engine()
        try:
            matches = search_memory(MemoryStore(engine), args.query, limit=args.limit)
        finally:
            engine.dispose()
    except Exception as error:
        sys.exit(f"Search not confirmed ({type(error).__name__}). Check query, database and API settings.")
    if not matches:
        print("No embedded memories matched. Unembedded memories are not searched.")
    for rank, match in enumerate(matches, 1):
        date = match.source_created_at.astimezone(timezone.utc).date().isoformat()
        print(f"{rank}. [{match.kind}; source={date}; cosine_distance={match.distance:.4f}] {match.text}")
        print(f"   memory_id={match.memory_id}, source_message_id={match.source_message_id}, conversation_id={match.conversation_id}")


if __name__ == "__main__":
    main()