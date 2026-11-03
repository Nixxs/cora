"""Raising a finding because the conversation touches on it, judged by embedding similarity; and
giving a contact's chat only the interests their message touches on.

The conversation's latest messages and each eligible finding (its title and text) are embedded
with the memory embedding model; a finding embeds once, the first time it is needed. Only the
closest finding, if it is related enough (RELEVANCE_THRESHOLD), is claimed, within its own
limits (see exploration_store).

A contact (on Signal or in a task's email thread) never gets her whole interest list: their new
message is compared with each live interest's title and latest finding (each embedding redone
when that text changes), and at most SPARKED_LIMIT close enough are given: title, description and
latest finding, never how the interest came about.

Usage: python -m subconscious.interests.relevance "what you might be talking about"
  Shows the closest findings and their similarity, claiming none, to see what would count.
  It embeds the text and any findings not embedded yet (billable, though tiny).
"""

import argparse
from datetime import datetime, timezone
import json
import sys

from dotenv import load_dotenv
from openai import OpenAI

from memory.store import validate_memory_embedding
from storage.database import build_engine
from storage.models import MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL
from subconscious.interests.exploration_store import RELEVANCE_THRESHOLD, ExplorationStore, Mention, distance_of
from subconscious.interests.interest_store import INTEREST_CONTEXT_MAX_CHARS, InterestStore

# The latest messages compared, newest kept when cut.
RECENT_MESSAGES = 4
CONVERSATION_MAX_CHARS = 1500
EMBED_TIMEOUT_SECONDS = 10
# A contact's chat: the interests their message touches on, at most this many.
SPARKED_LIMIT = 2
MESSAGE_MAX_CHARS = 1500


def conversation_text(history: list[dict]) -> str | None:
    """The latest messages of a chat history (get_reconnect_history), or None without a person's message."""
    recent = history[-RECENT_MESSAGES:]
    if not any(message["role"] == "user" for message in recent):
        return None
    lines = [f"{'Person' if message['role'] == 'user' else 'Cora'}: "
             + " ".join(part["text"] for part in message["content"]) for message in recent]
    return "\n".join(lines)[-CONVERSATION_MAX_CHARS:]


def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    response = client.embeddings.create(
        model=MEMORY_EMBEDDING_MODEL, input=texts,
        dimensions=MEMORY_EMBEDDING_DIMENSIONS, encoding_format="float",
    )
    if response.model != MEMORY_EMBEDDING_MODEL or sorted(item.index for item in response.data) != list(range(len(texts))):
        raise ValueError("Unexpected embedding response")
    return [validate_memory_embedding(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]


def embed_new_findings(store: ExplorationStore, client: OpenAI, *, now: datetime) -> int:
    """Embed the findings that could be raised but aren't embedded yet; return how many."""
    waiting = store.unembedded_findings(now=now)
    if waiting:
        vectors = embed_texts(client, [text for _, text in waiting])
        store.save_finding_embeddings({exploration_id: vector for (exploration_id, _), vector in zip(waiting, vectors)})
    return len(waiting)


def claim_relevant_now(conversation: str) -> tuple[Mention, float] | None:
    """For the app, off its event loop: the finding closest to the conversation, claimed, if one fits."""
    engine = build_engine()
    try:
        store = ExplorationStore(engine)
        now = datetime.now(timezone.utc)
        with OpenAI(max_retries=0, timeout=EMBED_TIMEOUT_SECONDS) as client:
            embed_new_findings(store, client, now=now)
            [vector] = embed_texts(client, [conversation])
        return store.claim_relevant_mention(vector, now=now)
    finally:
        engine.dispose()


def sparked_interests(store: InterestStore, client: OpenAI, message: str, *, now: datetime) -> list[dict]:
    """The live interests a contact's message touches on, closest first, as they may see them."""
    shared = store.shareable(now=now)
    if not shared or not message.strip():
        return []
    saved = store.embeddings([item["id"] for item in shared])
    stale = [item for item in shared if saved.get(item["id"], (None,))[0] != item["text"]]
    # One request: their message, then any interest whose title or latest finding changed.
    vectors = embed_texts(client, [message[-MESSAGE_MAX_CHARS:], *(item["text"] for item in stale)])
    fresh = {item["id"]: (item["text"], vector) for item, vector in zip(stale, vectors[1:])}
    if fresh:
        store.save_embeddings(fresh)
    saved |= fresh
    scored = sorted(((1 - distance_of(saved[item["id"]][1], vectors[0]), item) for item in shared),
                    key=lambda pair: pair[0], reverse=True)
    return [{key: value for key, value in item.items() if key not in ("id", "text")}
            for similarity, item in scored[:SPARKED_LIMIT] if similarity >= RELEVANCE_THRESHOLD]


def sparked_interests_now(message: str) -> str:
    """For the app, off its event loop: the interests a contact's message touches on, as JSON ("" for none)."""
    engine = build_engine()
    try:
        with OpenAI(max_retries=0, timeout=EMBED_TIMEOUT_SECONDS) as client:
            sparked = sparked_interests(InterestStore(engine), client, message, now=datetime.now(timezone.utc))
    finally:
        engine.dispose()
    while sparked and len(json.dumps(sparked, ensure_ascii=False)) > INTEREST_CONTEXT_MAX_CHARS:
        sparked.pop()
    return json.dumps(sparked, ensure_ascii=False) if sparked else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the findings closest to some text, claiming none (billable, tiny).")
    parser.add_argument("text", help="What the conversation might be about")
    args = parser.parse_args()
    load_dotenv()
    engine = build_engine()
    try:
        store = ExplorationStore(engine)
        now = datetime.now(timezone.utc)
        with OpenAI(max_retries=0, timeout=EMBED_TIMEOUT_SECONDS) as client:
            embedded = embed_new_findings(store, client, now=now)
            [vector] = embed_texts(client, [args.text])
        closest = store.closest_findings(vector, now=now)
    except Exception as error:
        sys.exit(f"Relevance check failed ({type(error).__name__}). Check database and API settings.")
    finally:
        engine.dispose()
    if embedded:
        print(f"Embedded {embedded} new finding{'s' * (embedded != 1)}.")
    if not closest:
        print("No findings she could raise right now.")
    for similarity, mention in closest:
        mark = "would offer" if similarity >= RELEVANCE_THRESHOLD else "too far"
        print(f"{similarity:.3f} ({mark}) [{mention.interest_title}] {mention.finding[:160]}")
    print(f"Threshold: {RELEVANCE_THRESHOLD}. Only the closest is offered, within the limits.")


if __name__ == "__main__":
    main()
