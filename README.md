# Cora

**Conversation Optimized Relational Assistant**

Cora is a terminal-based voice and text assistant with persistent memory. You can
talk or type, receive spoken replies and written answers, and ask Cora to recall
things from earlier conversations.

The long-term goal is a consistent identity across interfaces, with evolving
interests and carefully controlled initiative. Development happens in small,
understandable steps.

## Getting started

Cora runs directly on Windows; only its PostgreSQL/pgvector database runs in Docker.
You need Python with a virtual environment, Docker Desktop using Linux containers,
an OpenAI API key, and a microphone/speaker for voice interaction.

Run these commands from the repository root with your Python environment activated.

### 1. Install and configure

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.database.example .env.database
```

Only copy the example on a fresh setup; keep an existing database configuration.
Create a local `.env` file containing `OPENAI_API_KEY=your-key`.
In `.env.database`, choose a private database password **before storing personal
conversations**. Both settings files are ignored by Git; never commit credentials.

The database defaults to `127.0.0.1:5432`, with database and user named `cora`.
Change `CORA_DB_PORT` if that port is already in use. Keep the database local.

### 2. Start the database and apply migrations

```powershell
docker compose --env-file .env.database up -d --wait database
python migrate.py
```

Migrations are explicit: starting Cora does not apply them. Rerunning the migration
command safely skips already-applied files.

### 3. Run Cora

```powershell
python app.py
```

Keep Docker running while chatting, and run **only one Cora instance at a time**.
The startup check treats other active conversations as interrupted.

To stop the database afterwards:

```powershell
docker compose --env-file .env.database down
```

**Your data lives in a persistent Docker volume, but this is not a backup.**
Do not use `down -v` / `down --volumes`, delete the volume, or reset Docker's data:
these can erase saved conversations. Changing settings after initial setup does
not change an existing database's password or user. Back up important data.

## Using Cora

- **Speak or type:** both use the same running conversation.
- **Send a message:** Enter. Add a newline with Shift+Enter or Ctrl+J.
- **Check connection state:** the left side of the bottom row shows Active,
  Connecting, Disconnecting, Standby, or Disconnected. Muting does not change it.
- **Close Live without quitting:** click Active to enter Standby. Wait for speech
  and pending work to finish first. Cora closes the Live session and local audio,
  but keeps the app, chat, draft, and mute settings. Press Ctrl+R to resume;
  clicking Standby to wake is a later increment. Enter during Standby keeps the
  draft and asks you to resume, rather than saving or sending a failed request.
  Silent output audio does not count as speech or pending playback. If closing
  is blocked, the notification and Activity show what Cora is waiting for.
- **Mute audio:** click the microphone or speaker control on the bottom status row.
  Mic mute stops local recording; speaker mute stops playback and discards queued
  speech. Unmuting does not replay discarded audio. Typing stays available and
  Live stays connected (and billable); captions and written answers remain visible.
- **Watch playback:** eight audio bars beside the speaker control follow output
  volume. They become a flat line when silent and show “Muted” when playback is muted.
- **Reconnect Live:** Ctrl+R closes and restarts Live without quitting or clearing
  the chat. It keeps mute settings and drafts, and restores recent visible chat as
  context. Wait until Active before sending again. Reconnect refuses while known
  responses, tools, typing submissions, recent speech, or queued playback are busy;
  wait for them to finish and try again. This is not standby: Live reconnects immediately.
- **Read written answers:** code, tables, and Markdown appear as “Cora · on screen”.
  Cora is instructed not to read code aloud, but this is not a hard audio filter.
- **Copy chat text:** select it with the mouse, then press Ctrl+C.
- **Check Activity:** expand the panel for connection, tool, memory, and error events.
- **Quit:** use the close button, Ctrl+X, or ask Cora to close. The close tool waits
  three seconds when connected; this does not guarantee speech has finished.

Typed input is saved before sending. If saving or sending fails, the draft stays
available and Cora warns you. A failed send may have partially reached the model,
so there is no automatic resend. If Live drops with unfinished work, Cora warns
you; reconnect does not replay requests or tools. Check uncertain action outcomes
before requesting them again.

## How Cora works

Cora uses **two models working together in one Live session**:

- **Live (`gpt-live-1`) is the voice you talk to.** It listens, handles the
  conversation, and speaks back. It can answer directly when no delegation is needed.
- **The Responses backend (`gpt-6-luna`) is the delegated worker.** When a spoken
  request needs current information, memory lookup, tools, or a detailed written
  answer, Live passes it to this model using OpenAI's built-in delegation.

For example, if you ask about something happening today, Live delegates the
question, the backend searches the web, and Live uses the result to answer aloud.
For a question about an earlier conversation, the backend can call `search_memory`.
An explicit request to quit can trigger `close_app` instead.

**The backend chooses tools; the application executes custom tools.** Web search
is hosted by OpenAI, while Cora validates and runs its own tool handlers locally.
Code, tables, and requested written deliverables appear on screen; Live is
instructed to explain them briefly rather than read them aloud.

Typed messages go directly to the same Responses backend. A silent copy also
updates Live's context so you can switch between typing and speaking, though
context updates are asynchronous. Both models receive the same startup memories.

**Current limitation:** both voice and typed chat require an active Live session.
Live bills by session duration, per second, including silence and backend waits;
backend model and tool usage is charged separately. Muting a microphone or speaker
does not stop that duration charge. Independent text-only operation is not planned
for this next increment; quiet use will keep Live connected. Standby closes Live,
ending that session's duration billing. Background memory processing can still
finish and incur its separate charges while the local app remains open.

## How memory works

1. **During chat:** Cora saves typed messages, grouped visible voice transcripts,
   and displayed written answers in one conversation. Internal reasoning, tool
   events, and Activity entries are not saved as chat messages.
2. **When you quit:** pending chat saves finish and the conversation is marked
   closed. No new memory-processing requests start during shutdown.
3. **On the next startup:** a background pass extracts facts, preferences,
   decisions, and tasks from older finished conversations, then saves separate
   summaries. Extraction uses original messages, not summaries.
4. **Maintenance:** pending memories receive embeddings for semantic search.
   Conservative duplicate checks mark equivalent entries without deleting their
   text, evidence, or history. Marked duplicates are excluded from recall.
5. **Recall:** Live and the backend receive a startup snapshot of at most 10
   memories / 3,000 characters. The backend can also use `search_memory` to find
   relevant stored memories on demand (eight results by default, maximum 20).

Memories link back to original user messages and verbatim evidence. Newly embedded
memories can be searched during the current run; the startup snapshot changes only
after a restart. Already-summarised chats are not automatically re-extracted.

### Limitations and costs

- Saved conversations and memories are local, but normal chat and memory processing
  send relevant text or audio to OpenAI. This is not an offline assistant.
- Extraction, summarisation, embeddings, duplicate classification, and semantic
  searches can make separate **billable API requests**. Loading the startup
  snapshot itself makes no extra request.
- Voice grouping is approximate, and a crash can lose unsaved text. There is no
  recovery across app restarts yet. Live reconnect within the running app restores
  only recent visible text (up to 40 messages / 6,000 UTF-8 bytes), not full audio,
  hidden backend state, or unfinished tasks. The full displayed chat stays in the UI.
  A two-second transcript/audio quiet gap is only a reconnect safety hint, not a
  guaranteed completed speech turn; real voice/network timing still needs testing.
- Recall and duplicate decisions can be wrong. Similarity is not confidence;
  conflicting statements are kept rather than automatically superseded. Ages,
  plans, and tasks are dated historical information, not necessarily current facts.
- Processing is one bounded pass per startup, not a continuous loop. A failure
  stops the pass; unfinished work can be attempted on a later startup. An in-flight
  request may finish and save after closing, and process exit may wait for it.
- Search uses an exact database scan, with no relevance threshold or search index
  yet. Results are bounded, but scan cost is not.

### Manual memory tools

Run these from the repository root; replace placeholders with saved UUIDs.
The first two take a **conversation ID**; embedding takes a **memory ID**.
These commands can make billable requests.

```powershell
python -m memory.summarize CONVERSATION_UUID
python -m memory.extract_memories CONVERSATION_UUID
python -m memory.embed_memory MEMORY_UUID
python -m memory.search_memory "Where do I live?" --limit 3
```

Each command supports `--help`. Inspect saved records and their source links with
a database client such as DBeaver. User-facing memory editing/deletion controls
are not implemented yet.

## Project layout

| Location | Responsibility |
| --- | --- |
| [app.py](app.py) | Application orchestration, background work, and shutdown. |
| [live.py](live.py) | OpenAI Live connection, audio transport, and typed-context mirroring. |
| [session_config.py](session_config.py) | Models, voice, instructions, and session configuration. |
| [ui/](ui/) | Conversation display, composer, selection/copy, and Activity. |
| [devices/](devices/) | Microphone capture and speaker playback. |
| [storage/](storage/) | Database connection, ORM models, and conversation persistence. |
| [memory/](memory/) | Memory storage, extraction, summaries, embeddings, search, and duplicates. |
| [tools/](tools/) | Backend tool registration and handlers (`close_app`, `search_memory`). |
| [migrate.py](migrate.py), [migrations/](migrations/) | Explicit, numbered SQL migrations. |
| [tests/](tests/), [examples/](examples/) | Automated checks and standalone examples. |

Memory services are synchronous; UI-facing database calls and background
processing run off-thread.

## Development and testing

Run the normal suite without a database or real model requests:

```powershell
python -m unittest discover -s tests -v
```

To include database tests, start the database and enable the opt-in flag:

```powershell
$env:CORA_RUN_DB_TESTS = "1"
try { python -m unittest discover -s tests -v }
finally { Remove-Item Env:CORA_RUN_DB_TESTS }
```

Write tests use disposable databases, not Cora's saved chats, and require permission
to create databases. The standalone connection test is read-only. Model calls and
audio transport are mocked; real hardware, mixed voice/text behaviour, and memory
quality still need manual checks.

**Migration rule:** never edit, rename, or remove an applied migration. Add a new
higher-numbered SQL file instead. The runner checks history and applies pending
changes transactionally; it does not provide automatic downgrades.

## Roadmap

### Available now

- Unified terminal voice/text chat, streaming transcripts, and written answers.
- Separate clickable microphone and speaker mute controls in the bottom status row.
- A fixed-width speaking animation driven by output audio playback levels.
- Manual Live reconnect with recent chat context, without closing the local app.
- Click Active for close-only standby; resume explicitly with Ctrl+R.
- Hosted web search and guarded custom tools.
- Persistent conversations, automatic memory processing, and semantic recall.
- Non-destructive duplicate markers and manual memory inspection.

### Next priorities

- Build quiet-use controls and standby/wake-up in the small steps below.
- Improve mixed voice/text synchronisation, interruption handling, and audio checks.
- Improve memory relevance and handle corrections while preserving history.
- Add user-facing memory controls and structured project memory.
- Plan larger-context budgets, indexing, and conversation recovery.

### Quiet use and standby — development order

Checked steps are implemented; the rest are planned. Implement and test one step
at a time before moving on; standby must close Live, not merely mute audio.

1. [x] **Speaker mute:** add a clickable speaker control to the existing bottom
  status row. Stop current/queued playback and discard incoming audio while muted,
  without disconnecting Live or replaying old speech on unmute.
  Manual conversation check: mute/unmute worked, including toggling mid-speech.
2. [x] **Microphone mute:** make the existing microphone indicator clickable.
  Stop recording and sending microphone audio while muted; keep typing available.
  Generated silence keeps Live's timeline running without capturing room audio.
3. [x] **Quiet-use check:** verify typed questions, Live's visible reply transcripts,
  written answers, tools, and persistence still work with both audio controls muted.
  Keep replies readable without adding duplicate backend answers.
  Manual quiet-use check completed successfully with both controls muted.
4. [x] **Clear state display:** reuse the left-hand connection indicator for active,
  connecting, disconnecting, and disconnected states, with labels and distinct colours.
  Keep microphone and speaker controls on the right, with labels as well as colour.
  The Active state button now closes Live, with a distinct Standby state after
  confirmed closure; transition and disconnected states remain non-clickable.
5. [x] **Speaking animation:** add fixed-width Unicode audio bars beside the speaker
  control, driven by playback levels. Show a flat line when silent and a muted label
  when disabled; update Textual on its UI thread, not from the audio callback.
  Eight bars update at 20 FPS from lightweight playback-level snapshots, without
  relaying out the chat. Mocked tests pass; live animation/audio alignment needs a check.
6. [x] **Reconnect with context:** close and restart Live without quitting the UI.
  Restore relevant conversation context, keep the logical conversation and mute
  settings, and guard against duplicate tool actions or lost pending work.
  Ctrl+R restarts Live with bounded visible-text history, preserving drafts, mute
  settings, the database conversation, and tool-call deduplication. Known pending
  work blocks reconnect; interrupted requests are reported, never automatically
  replayed. Mocked tests pass; real voice/text continuity still needs a check.
7. [ ] **Manual standby/wake:** add explicit sleep/resume controls using that reconnect
  path. Keep the local app open with Live closed; typing must explicitly wake Live
  or clearly request resume, not silently fail. Standby is not a connection error.
  Enable the left-hand state button: click Active to sleep and Standby to resume.
  Close-only half implemented: click Active to close Live, keep the app/chat open,
  and show Standby only after confirmed session closure and cleanup. Ctrl+R can
  resume using the existing reconnect path; click-to-wake remains pending.
  Mocked close/resume tests pass; real-device standby still needs a check.
8. [ ] **Inactivity standby:** add a configurable idle timeout that considers typing,
  speech, playback, and pending work. Allow reading/thinking pauses; microphone
  silence alone must not trigger sleep during an active task.
9. [ ] **Optional farewell:** offer a brief “I'll leave you to it” before automatic
  standby. Finish playback before closing, respect speaker mute, and cancel sleep
  if the user resumes interacting.
10. [ ] **Local wake word:** detect “Hey Cora” locally during standby, without sending
   room audio to OpenAI. Buffer opening speech through reconnection and handle false
   triggers. Privacy mute disables listening entirely; wake-word listening is separate.
11. [ ] **Camera presence:** explore optional local detection of sustained absence
   and return to sleep/wake Cora. Start with presence, not identity recognition;
   use delays/cooldowns to avoid repeated wake-ups and preserve pending work.

The intended bottom row is **state on the left, audio controls on the right**:

```text
● Active          🎙 Listening   🔊 ▁▂▅█▆▃▁
● Active          🎙 Muted       🔇 Muted
○ Standby         🎙 Wake word   🔊 ───────
```

Standby means the billable Live session is closed, not that the app has quit.
Local wake sensors may remain active only when enabled: provide clear indicators,
separate microphone/camera privacy controls, and manual wake as a fallback.
Camera presence detection is separate from sending images to a model for vision.

### Long-term direction

- Persist Cora's identity, interests, reflections, and state **separately from
  memories about the user**.
- Share identity and memory across terminal, voice, keyboard, and Signal.
- Add camera support, permission-bounded local/GIS tools, and external integrations.
- Introduce proactive follow-ups and background reflection with explicit budgets,
  permissions, quiet hours, and an off switch.

Click-to-wake, automatic standby, local wake sensors, Signal integration, camera
support, and autonomous initiative are not built yet. Speaker mute has been checked in a live
conversation, and quiet use with both audio controls muted has been confirmed.
The playback visualiser still needs a real-device check.
The examples are standalone experiments, not integrated Cora features.