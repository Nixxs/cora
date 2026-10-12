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
not connect to the database yet. Message tables, migrations, and enabling the
`vector` extension in the database are separate next steps.

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
