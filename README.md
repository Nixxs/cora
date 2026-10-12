# cora
Conversation Optimized Relational Assistant

Cora is a terminal-based voice and text assistant. The long-term goal is a
consistent identity across interfaces, with persistent memory, evolving interests,
and carefully controlled initiative.

Development proceeds in small, understandable steps: implement one change, test
it, then build on the result.

## Local database — memory step 1

[compose.yaml](compose.yaml) runs PostgreSQL 17 with pgvector 0.8.7 installed.
Only the database runs in Docker; Cora still runs directly on Windows. Cora does
not connect to the database yet. Migrations enable the `vector` extension and
create conversation/message tables; application persistence is a later step.

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
starting Cora does **not** run them or connect to the database.

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

[database.py](database.py) builds a SQLAlchemy engine using the existing Psycopg
driver and the same database settings as the migration runner. Creating an engine
does not connect; running the standalone check opens a connection and reports the
database and user, then closes the connection and disposes the engine:

```powershell
python -m pip install -r requirements.txt
python database.py
```

Expected default result: `SQLAlchemy connected: database=cora, user=cora`.
The check only runs a `SELECT`: it does not create tables, change migration
history, or save messages. SQLAlchemy models are defined separately below.
Cora's startup, voice, keyboard, and shutdown code remain unchanged. This engine
is synchronous and intended for this standalone check; responsive application
integration is a separate step.

[tests/test_database.py](tests/test_database.py) checks configuration, connection
cleanup, and failures without a database. Its read-only live connection test is
enabled by `CORA_RUN_DB_TESTS=1`, using the same opt-in as the migration tests.
Keep using the existing SQL migrations; SQLAlchemy does not replace or run them.

## Conversation and message schema — memory step 3

[models.py](models.py) defines `ConversationRecord` and `MessageRecord` using
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
`conversations`, `messages`, and `schema_migrations`. The new application tables
are empty: Cora still does not save live messages. Processing checkpoints,
summaries, extracted memories, and application integration remain later steps.

[tests/test_models.py](tests/test_models.py) tests ORM/database agreement,
voice/keyboard ordering, defaults, constraints, and migration reruns. With
`CORA_RUN_DB_TESTS=1`, writes happen only in a disposable test database that is
removed afterwards; Cora's own tables are not used for test records.

## Conversation storage service — memory step 4

[conversation_store.py](conversation_store.py) provides `ConversationStore`:

- `create_conversation(conversation_id)` creates and commits a conversation;
	retrying its UUID returns the existing record without resetting its metadata.
- `get_conversation(conversation_id)` returns its saved scalar fields or `None`.
- `save_message(conversation_id, message_id=..., role=..., text=..., channel=...)`
	commits one message and assigns the next sequence number in that conversation.
	Supply `status="completed"` only when completion is confirmed; the default is
	`incomplete`. Delivery status defaults to `not_applicable`.
- `get_messages(conversation_id)` returns saved messages in sequence order.

The caller generates conversation/message UUIDs **before** calling the service
and reuses them when retrying an uncertain save. An identical message-ID retry
returns the existing row, preserving its sequence and timestamp. Different text
or metadata under that ID is rejected, not silently overwritten. Status updates
are not implemented yet and must not be disguised as save retries.

Each write commits before returning; failures roll back. A conversation row lock
serialises saves for that discussion, protecting ordering and duplicate retries.
The service creates a fresh SQLAlchemy session per operation and returns detached
records with loaded scalar fields; use `get_messages()` rather than lazy-loading
the conversation's relationship. The caller owns/disposes the engine.

No new dependencies, migrations, automatic retries, or Cora startup changes are
needed. This service is synchronous and independently testable; responsive app
integration and failure/delivery handling are separate next steps.

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

[conversation.py](conversation.py) owns chat history, transcript grouping and idle
timers, written-answer filtering, and redraws of the existing chat log.
[app.py](app.py) creates the component and routes voice, typed input, and backend
answers to it. The widget layout and selection/copy behavior remain in the UI.

## Activity display

[activity.py](activity.py) owns the latest 500 diagnostic entries, timestamps,
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
