# cora
Conversation Optimized Relational Assistant

Cora is a terminal-based voice and text assistant. The long-term goal is a
consistent identity across interfaces, with persistent memory, evolving interests,
and carefully controlled initiative.

Development proceeds in small, understandable steps: implement one change, test
it, then build on the result.

## Project layout

- [ui/](ui/): conversation and activity display, chat widgets, and text composer.
- [storage/](storage/): database engine, ORM models, conversation storage, and
	keyboard persistence.
- Root entrypoints: [app.py](app.py) runs Cora, [migrate.py](migrate.py) applies
	migrations and defines `database_settings()`, [summarize.py](summarize.py)
	generates a summary for one selected conversation, and
	[extract_memories.py](extract_memories.py) manually extracts its memories.
- [tools/](tools/): custom tool registration and handlers.
- [tests/](tests/): unit, headless UI, and opt-in database tests.
- [examples/](examples/): standalone examples, kept in place with their own CSS
	where applicable.
- [migrations/](migrations/): numbered SQL schema migrations.

## Local database — memory step 1

[compose.yaml](compose.yaml) runs PostgreSQL 17 with pgvector 0.8.7 installed.
Only the database runs in Docker; Cora still runs directly on Windows. Cora now
saves typed user messages and the displayed conversation to the database.
Migrations enable the `vector` extension and create conversation/message tables.

Prerequisite: Docker Desktop running with Linux containers and Docker Compose.
Run the following commands from the repository root.

Database settings live in the ignored `.env.database` file, separate from Cora's
existing `.env` API settings. On a fresh checkout, copy
[.env.database.example](.env.database.example) to `.env.database`. The supplied
password is **local-development-only**, not a production credential. Choose a
private password before storing personal conversations. Do not publish the port
outside localhost or commit credentials.

Start the database and wait for its health check:

```powershell
docker compose --env-file .env.database up -d --wait database
docker compose --env-file .env.database ps
```

Default connection settings: host `127.0.0.1`, port `5432`, database `cora`, user
`cora`; password comes from `.env.database`. If port 5432 is already in use,
change `CORA_DB_PORT` in that file before starting the container.

Open an SQL console (with the default database/user settings):

```powershell
docker compose --env-file .env.database exec database psql -U cora -d cora
```

Inspect startup logs or stop the database:

```powershell
docker compose --env-file .env.database logs --tail 50 database
docker compose --env-file .env.database down
```

The named volume `cora_database-data` keeps data when the container is stopped,
removed, or recreated. Start it again with the same `up` command. **Do not use
`down --volumes` / `down -v`, delete the volume, or reset Docker Desktop's data:
these can erase the database.** A persistent volume is not a backup.

Postgres uses the database name, user, and password to initialise an **empty**
volume. Changing the environment file afterwards does not rename the existing
database/user or change its password. Existing credentials must be changed with
SQL; do not delete saved conversations to change configuration. Changing the
Postgres major version also requires a planned database upgrade, not just an
image-tag edit.

## Database migrations — memory step 2

[migrate.py](migrate.py) applies numbered SQL files in [migrations](migrations).
The first, [0001_enable_vector.sql](migrations/0001_enable_vector.sql), enables
pgvector. The runner's `public.schema_migrations` table records each applied
filename, checksum, and timestamp. Later migrations add the application tables.

With the existing Python virtual environment activated and the database running:

```powershell
python -m pip install -r requirements.txt
python migrate.py
```

The runner reads the separate local database settings, not the API settings.
Environment variables named `CORA_DB_*` override file values. The host defaults
to `127.0.0.1`; `CORA_DB_HOST` can override it if needed. Migrations are explicit:
starting Cora does **not** run them. A startup database check tags unfinished
conversations; the new conversation row is created only when a message is saved.

Run the command again safely: already-applied files are checked and skipped.
All pending migrations and their history commit in one transaction. If SQL fails,
the whole run rolls back; correct the **unapplied** migration and retry. A database
lock prevents two runners applying the same batch concurrently. Migration SQL
must be transactional and must not contain its own `BEGIN`/`COMMIT`/`ROLLBACK`.

Never edit, remove, or rename an applied migration: checksums/history are checked
before pending SQL runs. Add a higher-numbered file for the next change instead
(for example, `0002_...sql`). This runner is forward-only; it does not implement
automatic downgrade scripts. Back up real data before future schema changes.

In DBeaver, refresh `cora → Schemas → public → Tables` to see `schema_migrations`.
The `vector` extension is also now enabled in `cora`.

Migration tests include an opt-in database check. It creates and drops its own
temporary database, never resets Cora's database, and needs permission to create
databases (the current local development user has it):

```powershell
$env:CORA_RUN_DB_TESTS = "1"
try { python -m unittest discover -s tests -p test_migrations.py -v }
finally { Remove-Item Env:CORA_RUN_DB_TESTS }
```

Normal test discovery still runs file/configuration tests but skips the database
tests unless explicitly enabled. No microphone or API connection is required.

## SQLAlchemy connection — next small step

[storage/database.py](storage/database.py) builds a SQLAlchemy engine using the existing Psycopg
driver and the same database settings as the migration runner. Creating an engine
does not connect; running the standalone check opens a connection and reports the
database and user, then closes the connection and disposes the engine:

```powershell
python -m pip install -r requirements.txt
python -m storage.database
```

Expected default result: `SQLAlchemy connected: database=cora, user=cora`.
The check only runs a `SELECT`: it does not create tables, change migration
history, or save messages. SQLAlchemy models are defined separately below.
This engine is synchronous. The standalone check runs directly; Cora's keyboard
adapter runs database operations on worker threads, not the UI event loop.

[tests/test_database.py](tests/test_database.py) checks configuration, connection
cleanup, and failures without a database. Its read-only live connection test is
enabled by `CORA_RUN_DB_TESTS=1`, using the same opt-in as the migration tests.
Keep using the existing SQL migrations; SQLAlchemy does not replace or run them.

## Conversation and message schema — memory step 3

[storage/models.py](storage/models.py) defines `ConversationRecord` and `MessageRecord` using
SQLAlchemy. [0002_create_conversations_and_messages.sql](migrations/0002_create_conversations_and_messages.sql)
creates the matching tables through the existing migration runner. Importing the
models does not connect, run migrations, or call `create_all()`.

- `conversations`: UUID, start/end timestamps, and status (`active`, `closed`,
	`interrupted`). This is a logical discussion, not a model transport session.
- `messages`: UUID, conversation ID, positive sequence number, role (`user` or
	`assistant`), text, channel (`voice`, `keyboard`, `screen`), and timestamp.
	Completion status is `incomplete`, `completed`, or `interrupted`; it defaults
	to `incomplete`. Separate delivery status is `not_applicable`, `pending`,
	`sent`, or `failed`, so submission failure need not erase the original text.

Voice and keyboard share a conversation ID and one sequence. UUIDs and unique
sequence positions prevent duplicate records; retries must reuse their message
ID (the storage service below handles same-ID retries). Foreign keys reject orphan
messages and prevent deleting a conversation while messages still reference it.
Timestamps are timezone-aware; end times cannot precede start times.

Run `python migrate.py`, then refresh DBeaver's table list. You should see
`conversations`, `messages`, `conversation_summaries`, and `schema_migrations`. The application tables
start empty and now receive typed user messages and displayed conversation text.
Summary storage is described below; generating summaries, extracting memories,
processing checkpoints, and resumption remain later steps.

[tests/test_models.py](tests/test_models.py) tests ORM/database agreement,
voice/keyboard ordering, defaults, constraints, and migration reruns. With
`CORA_RUN_DB_TESTS=1`, writes happen only in a disposable test database that is
removed afterwards; Cora's own tables are not used for test records.

## Conversation storage service — memory step 4

[storage/conversation_store.py](storage/conversation_store.py) provides `ConversationStore`:

- `create_conversation(conversation_id)` creates and commits a conversation;
	retrying its UUID returns the existing record without resetting its metadata.
- `get_conversation(conversation_id)` returns its saved scalar fields or `None`.
- `close_conversation(conversation_id)` marks an active conversation `closed` and
	sets its end timestamp. Repeating it preserves the original end time; missing
	IDs do not create rows and already-finished conversations remain unchanged.
- `interrupt_active_conversations(current_conversation_id)` tags other active
	conversations `interrupted`, returning the number changed. Messages and
	timestamps are untouched, and the current conversation is excluded.
- `save_message(conversation_id, message_id=..., role=..., text=..., channel=...)`
	commits one message and assigns the next sequence number in that conversation.
	Supply `status="completed"` only when completion is confirmed; the default is
	`incomplete`. Delivery status defaults to `not_applicable`.
- `get_messages(conversation_id)` returns saved messages in sequence order.

The caller generates conversation/message UUIDs **before** calling the service
and reuses them when retrying an uncertain save. An identical message-ID retry
returns the existing row, preserving its sequence and timestamp. Different text
or metadata under that ID is rejected, not silently overwritten.
`set_delivery_status(conversation_id, message_id, delivery_status)` updates only
delivery metadata; updates must not be disguised as save retries.

Each write commits before returning; failures roll back. A conversation row lock
serialises saves for that discussion, protecting ordering and duplicate retries.
The service creates a fresh SQLAlchemy session per operation and returns detached
records with loaded scalar fields; use `get_messages()` rather than lazy-loading
the conversation's relationship. The caller owns/disposes the engine.

The service remains synchronous and independently testable, with no automatic
retries or schema changes. The keyboard integration below calls it off-thread.

[tests/test_conversation_store.py](tests/test_conversation_store.py) verifies
committed persistence through an engine reopen and a fresh Python process,
mixed-channel history, rollback, ID conflicts and concurrent saves/retries. Its
write tests use a uniquely named disposable database under `CORA_RUN_DB_TESTS=1`,
not Cora's real tables. Run with the existing virtual environment activated:

```powershell
$env:CORA_RUN_DB_TESTS = "1"
try { python -m unittest discover -s tests -p test_conversation_store.py -v }
finally { Remove-Item Env:CORA_RUN_DB_TESTS }
```

## Live keyboard persistence — memory step 5

[storage/keyboard_persistence.py](storage/keyboard_persistence.py) gives each app run one logical
conversation UUID, independent of its model connection. It opens the database
for the startup check described in step 8; no conversation row is created until
the first message is saved.
[app.py](app.py) runs submissions in a managed Textual worker and uses
`asyncio.to_thread()` for storage calls so database work does not block the UI.

Start the database, apply migrations explicitly, then run Cora normally. Send a
harmless test phrase and refresh DBeaver: `conversations` should contain the new
discussion and `messages` its typed user text, `channel=keyboard`, `role=user`,
and an increasing sequence number. Displayed voice transcripts and assistant
replies are covered in step 6 below. No saved database history is injected into
Cora's model context yet.

Typed text is saved as `completed` (the submitted text is final) with delivery
`pending` **before** the existing model send. If saving fails, the draft is kept
and nothing is sent; Activity and a notification show the problem without SQL
parameters or credentials. Retrying the unchanged draft after a database-save
failure reuses its message UUID, protecting against an uncertain commit.

Successful send calls update delivery to `sent`; failed calls update it to
`failed` and keep the draft. `sent` means the send routine completed, not that a
model response or timeline acknowledgement was received. A failed send can have
partially reached the backend, so there is no automatic resend. Manually sending
again after a send failure creates a new attempt with a new message UUID, while
the old record remains available. If the delivery-status update cannot be
confirmed, saved text remains available and the app warns; an already-sent draft
is cleared to avoid encouraging accidental resends. A crash may leave `pending`
delivery, which is an uncertain outcome, not a confirmed failure.

Shutdown rejects new submissions, waits for active keyboard work, then closes
the model transport and disposes the adapter's database engine. The existing
close-tool delay and microphone/speaker cleanup remain unchanged. Conversation
closing is covered in step 7 below; resumption/recovery are not implemented.
Keep Docker running while chatting;
choose a private database password before entering personal conversation data.

[tests/test_keyboard_persistence.py](tests/test_keyboard_persistence.py) tests
save-before-send, draft retention, delivery errors, a responsive UI during a
slow database save, and shutdown ordering. Its actual app-to-Postgres write test
uses a disposable database with the OpenAI send mocked (`CORA_RUN_DB_TESTS=1`).
Unit/headless tests stub persistence so ordinary test runs never write to Cora's
real tables. A real voice/API smoke test remains a manual check.

## Displayed conversation persistence — memory step 6

For memory, Cora records the conversation visible in the chat, not a separate
archive of Luna's internal events. [ui/conversation.py](ui/conversation.py) records
grouped user and assistant voice text when the existing display grouping ends
(speaker change, two-second pause, or shutdown). Displayed written answers are
saved once, retaining their Markdown/code but removing the internal screen marker.
Hidden backend answers, reasoning, tool calls, Activity entries, blank separators,
and redraws do not create additional records. Typed messages keep their existing
save-before-send path and are not saved again by the display callback.

All text uses the same conversation UUID and existing `messages` table. Voice
chunks have `channel=voice` and conservatively retain `status=incomplete`: the
SDK provides no transcript-done event, and display grouping is approximate.
They are still useful memory text, not confirmed complete speech turns. Screen
answers have `channel=screen`, `status=completed`; both use delivery
`not_applicable`. Sequence numbers follow save/flush order, not exact audio timing.
No new schema, dependencies, backend archive, or turn-reconstruction system is added.

Database calls remain off-thread and are serialised. Shutdown closes Live, flushes
the last displayed transcript, waits for queued chat saves, then closes the engine.
A failed voice/screen save warns without interrupting the conversation or hiding
the text. There is no automatic retry; a crash can lose an unsaved display chunk.
Conversation closing is covered below; resumption and memory retrieval remain later steps.

Manual check: speak a harmless question, wait for the reply and display pause,
then refresh `messages` in DBeaver. Look for `role=user/assistant`, `channel=voice`,
and the same conversation ID as typed input. Ask for a short code example to check
that its displayed answer also appears once with `channel=screen`. Quit while text
is still active to check that the final chunk is saved.

[tests/test_displayed_chat_persistence.py](tests/test_displayed_chat_persistence.py)
checks display-only capture, duplicate redraw/flush protection, unchanged keyboard
saving, failure warnings, shutdown draining, and mixed-channel readback after an
engine reopen. Database writes use a disposable database, not personal chat data.

## Conversation shutdown — memory step 7

Normal shutdown now marks the current saved conversation `closed` and sets its
timezone-aware `ended_at` using the database clock, after pending message saves
have finished and before the database engine is disposed. The close button,
Ctrl+X, and `close_app` all use this same shutdown path. Existing messages and
their completion/delivery statuses are not changed. Shutting down an unused app
run does not create an empty conversation just to close it.

The update is safe to repeat: an already-finished conversation keeps its original
end time. A database failure warns but does not prevent cleanup or quitting; its
row may remain `active`. A crash or forced process termination may likewise leave
an active row, which the startup check below tags as interrupted. There is no
automatic recovery, summary generation, new migration, or new dependency in this
step. `closed` means the app ended the conversation, not that every possible
transcript was saved.

Manual check: start Cora, send or speak a harmless message, then quit normally.
Refresh its row in DBeaver's `conversations` table: `status` should be `closed`
and `ended_at` populated. Its saved messages should still be present.

## Interrupted conversations — memory step 8

Before connecting Live, Cora checks the database off-thread and changes leftover
`active` conversations to `interrupted`, excluding this app run's conversation UUID.
Already-closed/interrupted rows and all saved messages stay unchanged. The actual
crash time is unknown, so this check does not invent an `ended_at` value or change
uncertain message delivery statuses. It creates no empty conversation, resumes
nothing, and does not recover unsaved text. Old active rows from earlier versions
are also treated as unfinished conversations.

This simple check assumes **only one Cora instance is running**. Do not launch a
second instance alongside a live one: its active conversation would look unfinished.
If the database check fails, Cora warns and continues connecting Live; those rows
remain unchanged until a later successful startup. Normal message-save failure
handling is unchanged. Shutdown waits for an in-progress check before disposing
the engine. There is no new schema, dependency, retry loop, or recovery system.

[tests/test_interrupted_conversations.py](tests/test_interrupted_conversations.py)
checks startup ordering, failure warnings, shutdown during a slow check, and a
simulated unclosed run in a disposable database. To check manually after an actual
crash, restart Cora and refresh `conversations` in DBeaver: the previous active row
should now show `interrupted`, with its saved messages still present.

## Summary storage — memory step 9

[0003_create_conversation_summaries.sql](migrations/0003_create_conversation_summaries.sql)
adds `conversation_summaries`. It stores just the conversation UUID, summary text,
and timezone-aware creation timestamp. The conversation UUID is its primary key,
so there can be only one summary per conversation. A foreign key links it to the
original conversation and prevents orphan summaries or deleting a linked source.
Blank summary text is rejected. [storage/models.py](storage/models.py) maps this table with
`ConversationSummaryRecord`; importing the model still does not create tables.

[storage/conversation_store.py](storage/conversation_store.py) adds:

- `save_summary(conversation_id, text)`: commits a summary for a `closed` or
	`interrupted` conversation. Missing/active conversations are rejected by the
	service. Identical retries return the existing summary and preserve its timestamp;
	different text for the same conversation is rejected rather than overwritten.
- `get_summary(conversation_id)`: returns its saved summary, or `None`.

The same conversation-row lock used by message storage serialises summary saves.
Existing conversations and messages are not changed. No summary model, model call,
background processor, embedding, recall, or UI wiring is added in this step.
The new table remains empty until a summary is explicitly saved; closing a chat
does not generate one yet. No new Python dependencies are needed.

Apply the new migration with the existing runner, then refresh DBeaver's tables.
[tests/test_conversation_summaries.py](tests/test_conversation_summaries.py)
checks finished-conversation eligibility, committed readback after engine reopen,
same-text retries, conflicting saves, concurrent saves, database constraints,
and migration reruns. Tests write only to a disposable database, not saved chats.

## Standalone summariser — memory step 10

[summarize.py](summarize.py) manually generates a short summary for **one selected
finished conversation**. The CLI remains manual; the same summariser is also
called by the background pass below. Choose a `closed` or
`interrupted` conversation UUID from DBeaver, then
run `python summarize.py YOUR_CONVERSATION_UUID` in the existing environment,
replacing the placeholder with that UUID. The script uses the existing `.env`
API settings and `.env.database` database settings. Migrations must already be applied.

The default is Cora's existing backend model, `gpt-6-luna`; `--model MODEL_NAME`
overrides it for this run. This makes a separate, billable Responses API request
and sends that conversation's saved text to OpenAI. No web search or application
tools are supplied. The request asks for at most 200 words covering topics,
user-stated preferences/facts, decisions, outcomes, and unresolved tasks. Recorded
conversation text is explicitly untrusted data, not instructions for the summariser.
Approximate/incomplete voice chunks are included, with completion and delivery
metadata so uncertain exchanges are not presented as confirmed outcomes.

An existing summary is returned without another model call. Missing, active, or
empty conversations are rejected before contacting the model. Failed, incomplete,
blank, or refusal responses are not saved. Input is not silently truncated; an
oversized conversation may fail rather than generate a summary of a hidden subset.
There is no automatic API retry, chunking pipeline, or summary replacement policy.
The script prints the resulting summary and saves it through `save_summary()`;
refresh `conversation_summaries` in DBeaver to inspect the row.

Database/API clients are cleaned up, and errors do not print private transcript
text or credentials. If saving fails after generation, a later run may need to
generate again; check for an existing summary before retrying. No new dependency,
migration, background processor, chat-context injection, or Cora UI change is added.

[tests/test_summarize.py](tests/test_summarize.py) validates the response fixtures
against the installed SDK, mocks model calls, and checks saved readback in a
disposable database. These tests do not send personal conversations to OpenAI;
an actual summary-quality check requires the manually selected conversation.

## Background summaries — memory step 11

This describes the original step 11 pass; step 14 extends it with extraction before summarisation.

After the startup interruption check, Cora starts **one background pass** over
older `closed`/`interrupted` conversations with saved messages and no summary,
oldest first. The current conversation UUID, active chats, empty chats, and already
summarised chats are excluded. The pass does not hold up the Live connection or
wait until you finish the current chat. A conversation closed on this run is
eligible on the **next startup**, not during shutdown.

The worker simply calls the existing `summarize_conversation()` in a background
thread, one conversation at a time. This enables automatic, separate billable
`gpt-6-luna` requests and sends those saved transcripts to OpenAI. The existing
instructions, validation, cache, and no-retry behavior remain unchanged. Progress
appears in Activity. An error stops this pass with only its exception type logged;
missing summaries can be attempted on a later startup.

The worker owns a separate database engine, so it does not hold the app's database
lock or use an engine disposed by chat shutdown. Closing stops it from starting
another summary, but an already-running synchronous request can finish and save
its result; final process exit may wait for that request. Its engine is disposed
when the worker finishes. There is no polling loop, async summariser rewrite,
shutdown summary, new dependency/schema, extraction, embedding, or chat-context
injection. Run only one Cora instance; simultaneous manual/automatic requests can
still duplicate model work, though storage prevents overwriting a saved summary.

[tests/test_background_summaries.py](tests/test_background_summaries.py) exercises
the pass with mocked model calls and disposable database records. For a manual
check, close a chat normally, restart Cora, and watch Activity for its summary save;
then refresh `conversation_summaries` in DBeaver. Normal chat should remain usable.

## Manual memory extraction — memory step 12

[extract_memories.py](extract_memories.py) extracts concise facts, preferences,
decisions, and tasks from one `closed` or `interrupted` conversation's **raw
original messages, not summaries**. The original 200-word summariser is unchanged.
Use `python extract_memories.py UUID [--model MODEL]`, replacing UUID with the
conversation ID; the optional model defaults to the existing `gpt-6-luna` backend.
This makes a separate, billable request sending the raw saved-message transcript
to OpenAI, with no tools, automatic API retry, or input truncation.

`python migrate.py` applies the new
[0004_create_memories.sql](migrations/0004_create_memories.sql) migration.
The `memories` table has `id`, `source_message_id`, `kind`, `text`, `evidence`,
and timezone-aware `created_at`; `kind` is `fact`, `preference`, `decision`, or
`task`, and `text` is at most 500 characters. The source conversation is resolved
through the source message: there is no duplicate conversation column.
[storage/models.py](storage/models.py) maps it; [storage/memory_store.py](storage/memory_store.py)
validates user-message sources and exact, verbatim evidence quotes.

The extraction policy allows explicit personal facts such as “I live in Perth”,
but skips temporary visits and never infers home/personal location from a weather
question. Assistant assertions are not user facts; qualifiers and corrections
must be preserved. Valid quotes and schema do **not** guarantee model semantic
fact correctness. Mocked tests check plumbing only; manual quality review is required.

Existing nonempty memories return from storage as a cache without another model
request. Empty extraction returns no memories, but saves no processed marker:
repeating it can request again. Saves are batch-atomic and never overwrite rows;
exact retries deduplicate by source message, kind, and text. Cross-conversation
semantic deduplication and updates are deferred.

Step 12 introduced manual extraction only, with no context injection, extraction
worker, search, embeddings, or consolidation. The manual CLI remains available;
step 13 adds the startup read and step 14 adds automatic extraction.

## Startup memory context — memory step 13

[storage/memory_store.py](storage/memory_store.py)'s `MemoryStore.get_startup_context()`
selects at most 10 SQL rows: facts/preferences first, then decisions/tasks;
within each bucket, newest **source-message timestamp** first, then memory ID descending.
Whole-record JSON is capped at **3,000 characters**, including metadata and escaping;
selection stops before the next record would overflow, without truncating or skipping it.
This is a first simple heuristic, not smart scoring or an exact token budget.

[session_config.py](session_config.py) adds the same dated historical-data context to
Live and backend sessions. `source_date` is the original statement's UTC date, not
extraction time. A current-UTC-date prompt guard treats ages/plans/tasks as potentially
outdated, prefers fresh explicit corrections, and treats memories as untrusted data,
never instructions or permission to invoke tools. Static safety instructions sit
outside the 3,000-character data cap; empty context leaves default prompts unchanged.

[app.py](app.py) reads off-thread in the existing startup worker, before Live connects,
using the persistence adapter's existing engine without creating a new chat row.
Database failure warns with only the exception class (no private details) and continues
with empty context; shutdown waits for the read before disposing the engine.
Loading adds **no extra model request**; normal Live/backend sessions include the
context with their usual token usage. This limited startup snapshot has no dynamic
refresh, cross-chat semantic deduplication/correction filtering, pinned memories,
index/search, schema changes, or new dependencies. At step 13 there was no automatic
extraction; see step 14 for the later background extension.

Manual check with the three existing actual memories: freshly restart and ask for
your home city in both spoken and typed input **without telling Cora Perth again**.
Check age is treated as a dated source statement and fresh current corrections win.
Mocked tests make no actual Live or microphone calls; this manual check is still needed.

## Automatic memory extraction — memory step 14

[app.py](app.py) extends the same threaded `summarize_previous_conversations` worker.
SQL selects finished, nonempty previous chats without summaries, oldest first;
current, active, empty, and already-summarised chats are excluded, so there is no
automatic backfill of already-summarised chats; use the manual extraction CLI for those.
For each chat it calls the original `extract_memories(store, id)`, checks closing,
then calls the original `summarize_conversation()`. Grounded JSON still uses original
messages, not summaries; no async function rewrite, schema change, or dependency is added.
The summary saved last is the automatic pass's completion marker, even for **zero
memories**, avoiding repeated charges after success; manual empty extraction still saves no marker.
An extraction error stops before summarisation or summary saving; any error stops the
whole pass, logs only the exception type (redacted), and leaves retry to a future startup.
If summarisation fails after extraction, a future startup reuses the existing nonempty memory cache, but a zero-memory extraction may incur another extraction charge.
Closing permits an in-flight extraction to finish and save, but starts no summary or
next chat; the independent engine is disposed in `finally`, and process exit may wait for the synchronous request.
Startup context is loaded **before** this worker: new memories become available on the
**following restart**, not in the current Live/backend session; there is no dynamic refresh.
User-approved automation normally makes separate **billable extraction and summary
model calls** at startup; tests mock them and make no real model calls.
Activity logs `Memory extraction complete` with count and UUID, never memory texts,
plus the summary save. Manual check: finish a new chat, restart, watch Activity and
inspect `memories` in DBeaver, then restart again and check recall.

## Current progress

- [x] Textual chat interface with streaming, grouped user and Cora transcripts.
- [x] Microphone capture and audio streaming to OpenAI Live.
- [x] Microphone activity indicator with an idle reset.
- [x] OpenAI-managed Responses delegation to `gpt-6-luna`.
- [x] Hosted web search for the delegated model, verified through search events.
- [x] Keyboard input sent to the Responses backend in the existing Live session.
- [x] Typed input mirrored into Live's context to improve voice/text continuity.
- [x] Collapsible Activity panel with timestamps, backend/search lifecycle events,
			and error details, separate from the conversation.
- [x] First custom backend function, `close_app`, using the same guarded shutdown
	action as the close button and Ctrl+X. Mocked routing/audio-cleanup tests
	passed; voice and typed tool selection still need a live test. Shutdown
	is immediate and does not wait for a spoken goodbye.

Voice, keyboard, delegation, and search have been exercised interactively. Typed
context delivery is asynchronous; using one connection does not by itself
guarantee that both models have consumed the latest input at the same instant.

## Conversation display

[ui/conversation.py](ui/conversation.py) owns chat history, transcript grouping and idle
timers, written-answer filtering, and redraws of the existing chat log.
[app.py](app.py) creates the component and routes voice, typed input, and backend
answers to it. The widget layout and selection/copy behavior remain in the UI.

## Activity display

[ui/activity.py](ui/activity.py) owns the latest 500 diagnostic entries, timestamps,
panel summaries/error highlighting, and activity-log redraws. [app.py](app.py)
routes diagnostics to it and schedules a redraw after the panel expands and
layout is ready. Activity does not modify conversation history or transcripts.

## Session configuration

[session_config.py](session_config.py) contains the model names, voice, session
audio format, and Live/backend instructions. Its `build_session_config()` function
builds a fresh payload using the registered tool schemas. [live.py](live.py)
handles the connection, audio streaming, and message transport separately.

## Custom tools

- [tools/registry.py](tools/registry.py) owns registration, argument validation,
	duplicate-call tracking, and dispatch. It does not depend on Textual.
- [tools/close_app.py](tools/close_app.py) keeps the close tool's API schema,
	validator, and handler together. UI shutdown remains in [app.py](app.py).
- [tools/__init__.py](tools/__init__.py) registers the tools and supplies their
	callbacks. Add future tools here after defining them in their own modules.
- [live.py](live.py) uses schemas exported by that same registry alongside
	hosted web search. Handlers own their result messages and any continuation;
	the close handler terminates the session instead of requesting another answer.

## Roadmap / todo

Checkboxes track implemented work, not just planned features. The phases group
the vision; they are not rigid dependencies. Some capability work, such as hosted
search, is already available.

### Phase 1 — Presence

Make Cora comfortable to talk to and useful on screen.

- [x] Streaming text that grows within a grouped message.
- [x] Unified voice/keyboard session with typed-context mirroring.
- [ ] Continue testing mixed voice/text input, overlapping requests, and topic
			changes; improve synchronization where needed.
- [x] Basic buffered playback of Live's generated audio through the default output
	device (mocked tests passed; audible playback still needs a headphone test).
- [ ] Handle playback buffering, interruptions, shutdown, and microphone feedback.
- [x] Completed backend answers displayed as Markdown with syntax-highlighted
	code and tables, separately labelled “Cora · on screen”.
- [x] Prompt-guided separation of written answers and spoken explanations.
	Code requests are delegated for actual source code; Cora is instructed
	not to read code or tables aloud. This is not a hard audio filter.
- [ ] Verify screen/voice separation in live conversations and strengthen it
	if Cora still reads code aloud.
- [ ] Explore image presentation later.

### Phase 2 — Continuity

Keep relevant knowledge and a stable identity across sessions.

- [ ] Persistent user memory: facts, preferences, family, and relevant personal
			context.
- [ ] Persistent project memory: goals, decisions, progress, and unfinished work.
- [ ] Store conversation history and useful summaries.
- [ ] Selectively retrieve relevant memories instead of loading everything into
			each session.
- [ ] Maintain short-term conversation context alongside long-term retrieval.
- [ ] Manage context size, summarization, and recovery after reconnects.
- [ ] Define and persist Cora's identity and personality across sessions.
- [ ] Keep Cora's own persistent state separate from memories about Nick.
- [ ] Provide ways to inspect, correct, and delete stored memories.

### Phase 3 — Agency

Develop persistent interests and bounded initiative.

- [ ] Structured interests that can grow, fade, branch, and differ from Nick's.
- [ ] Persist Cora's own preferences and current state.
- [ ] Periodic background reflection over recent conversations and projects.
- [ ] Update memories and interests, connect ideas, and keep reflection or
			journal-like notes and unfinished thoughts.
- [ ] Define explicit schedules, budgets, and controls for background wakeups.
- [ ] Proactive follow-ups when something relevant or interesting arises.
- [ ] Signal interface for conversations and optional proactive messages.
- [ ] Shared identity and memory across terminal, voice, keyboard, and Signal.
- [ ] User-controlled permissions, quiet hours, and an off switch for initiative.

### Phase 4 — Capability

Expand the delegated worker's tools without making Live the orchestration layer.

- [x] Hosted web search through Responses delegation.
- [ ] Custom-tool registry, argument validation, execution, and result handling.
- [ ] Local tools and file access with explicit permission boundaries.
- [ ] GIS tools and workflows.
- [ ] External API and service integrations.
- [ ] Signal tooling for authorized messaging workflows.
- [ ] Combine hosted tools and application-owned tools in the delegated worker.
- [ ] Broader multi-step and autonomous workflows with progress tracking,
			cancellation, and approval before consequential actions.

## Architectural direction

- **OpenAI Live:** conversational voice interaction and spoken delivery.
- **Responses backend (Luna):** delegated reasoning, hosted search, and future
	tool workflows.
- **Application:** UI, microphone/playback, input routing, diagnostics, persistent
	state, retrieval, permissions, and execution of application-owned tools.
- **Shared interfaces:** one Cora identity and memory system, rather than separate
	terminal, voice, keyboard, and Signal bots.

### Separate user memory from Cora's state

The following is a conceptual future layout, not directories implemented today:

```text
memory/
	nick/
	conversations/
	projects/

cora/
	identity/
	interests/
	reflections/
	preferences/
	current_state/
```

“Nick likes X” and “Cora is interested in X” are different records. Keeping them
separate allows Cora's interests to evolve rather than simply mirroring the user
profile. This persistent state is application-managed; the model does not retain
it automatically between sessions.
