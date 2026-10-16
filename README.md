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

A normal launch also writes a diagnostic `live-trace.log` beside `app.py`.
It records command/event IDs, event order,
tracking state before/after handling, and standby blockers—not chat text, tool
arguments/results, or audio. Git ignores the log. Each app launch replaces the
previous trace, so reproduce once and keep the log before restarting again.
This is diagnostic-only; it does not change request matching or queueing.

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
  and pending work to finish first. Cora closes the Live session and Live audio,
  but keeps the app, chat, draft, and mute settings. Click Standby or press Ctrl+R
  to resume. The button shows Connecting and cannot be clicked again while waking.
  Enter during Standby keeps the draft and asks you to resume, rather than saving
  or sending a failed request.
  Silent output audio does not count as speech or pending playback. If closing
  is blocked, the notification and Activity show what Cora is waiting for.
- **Ask Cora for standby:** say something like “go to standby”. She may also
  choose standby herself once a conversation has clearly wound down, for example
  after you say goodnight. She gives a short sign-off, then enters Standby once
  speech and pending work finish. If that takes more than 30 seconds, she stays
  active and Activity notes it. This uses the same standby path as clicking Active.
- **Automatic standby:** after five minutes of inactivity by default, Cora uses
  the same standby path. Editing, cursor movement, submission, speech, and audio
  controls reset the clock. Setup, pending responses/tools/submissions, and audible
  playback prevent sleep. Work finishing or Live resuming gives a fresh full idle
  period, leaving time to read or think. An untouched draft does not prevent sleep;
  it is preserved. Click Standby, press Ctrl+R, or enable local wake listening to resume.
- **Optional standby heads-up:** enable `CORA_STANDBY_FAREWELL=1` to have Live
  briefly announce automatic standby, then leave a response window before closing.
  Detected user speech, typing, cursor movement, or controls cancel sleep and reset
  the idle period. Speaker mute skips the spoken alert; microphone mute means you
  must type or use a control to cancel. Manual standby has no farewell.
- **Mute audio:** click the microphone or speaker control on the bottom status row.
  Mic privacy mute stops all local recording, including wake listening, and drops
  buffered opening speech. Speaker mute stops playback and discards queued
  speech. Unmuting does not replay discarded audio. Typing stays available and
  Live stays connected (and billable); captions and written answers remain visible.
- **Local wake:** click Wake off to enable local “Hey Cora” detection during
  Standby. Wake on turns green while the local listener is ready. Its tooltip
  distinguishes standby listening, active-session inactivity, and privacy mute.
  Wake has its own enable control, but microphone privacy mute stops its listening
  too. Enabling Wake never unmutes the microphone. See setup below.
- **Watch playback:** eight audio bars beside the speaker control follow output
  volume. They become a flat line when silent and show “Muted” when playback is muted.
- **Reconnect Live:** Ctrl+R closes and restarts Live without quitting or clearing
  the chat. It keeps mute settings and drafts, and restores recent visible chat as
  context. Wait until Active before sending again. Reconnect refuses while known
  responses, tools, typed submissions/acknowledgements, observation sends, recent
  speech, or queued playback are busy;
  wait for them to finish and try again. This is not standby: Live reconnects immediately.
- **Read written answers:** code, tables, and Markdown appear as “Cora · on screen”.
  Cora is instructed not to read code aloud, but this is not a hard audio filter.
- **Copy chat text:** select it with the mouse, then press Ctrl+C.
- **Test awareness:** press Ctrl+T to queue a simulated observation that Nick
  received a Signal message. It travels through the local event bus to the attention
  manager, which holds observations in arrival order while Cora is speaking or
  processing work, or Live is disconnected/in standby. They send one at a time
  when Live is Active and quiet; they do not wake it automatically.
  This is background context, not a typed message or a real Signal monitor. Activity
  shows queued/sent/failed notifications, not whether Cora spoke. The queue is
  in memory only and is lost on app exit. Failed sends are reported, not retried,
  because they may have partially reached Live. Speaker mute still suppresses playback.
- **Check Activity:** expand the panel for connection, tool, memory, and error events.
- **Quit:** use the close button, Ctrl+X, or ask Cora to close. The close tool waits
  three seconds when connected; this does not guarantee speech has finished.

Typed input is saved before sending. If saving or sending fails, the draft stays
available and Cora warns you. A failed send may have partially reached the model,
so there is no automatic resend. If Live drops with unfinished work, Cora warns
you; reconnect does not replay requests or tools. Check uncertain action outcomes
before requesting them again.

### Background event foundation

The current path is **producer → local event bus → attention manager → Live**.
Ctrl+T is the only built-in test producer; there is no Signal/camera/screen monitor yet.
An `ObservationEvent` carries a source, text, unique ID, and UTC observation time.
A `FindingEvent` asks attention to consider raising one of Cora's interest findings
(see [How interests work](#how-interests-work)); it carries no finding text.
The bus routes events to synchronous subscribers; it does not decide when Cora
speaks, persist events, or replay them. The attention manager owns the waiting queue.

These components live in the [subconscious/](subconscious/) package:
[subconscious/event_bus.py](subconscious/event_bus.py) routes events and
[subconscious/attention.py](subconscious/attention.py) coordinates conversational attention.
Future interests, self, and awareness workers can be added as separate components
in this package. Give each worker the dependencies it needs and connect it through
events, rather than having it call Live or other workers directly. The app wires
components together and manages their lifecycles; automatic plugin discovery and
cross-process execution are not implemented yet.

Publish from Cora's event-loop thread. Background threads must hand events over
using `app.call_from_thread(app.event_bus.publish, event)` rather than invoking
subscribers or the attention manager directly. Separate processes need a later
transport adapter; Redis is not required by this in-process version. Closing Cora
closes the bus and prevents further publication. Subscriber errors are reported
without private event text, and do not stop delivery to other subscribers.

### Local wake-word setup

The standby listener uses **Whisper locally**, via faster-whisper on the CPU in
INT8 mode, not the OpenAI transcription API. Download the four files from
[Systran/faster-whisper-base.en](https://huggingface.co/Systran/faster-whisper-base.en/tree/main):
`model.bin`, `config.json`, `tokenizer.json`, and `vocabulary.txt`. Keep their
original names in `models/wake/faster-whisper-base.en/` at the project root.
Model downloads are ignored by Git. There are no automatic model downloads;
missing files stop the listener with an Activity error. Install the declared
requirements as usual. Older sherpa model downloads are no longer used.

The separate `wake/` package handles monitoring, local transcription, and
whole-word matching for **Hey Cora**, **Hey Kora**, and **Hey Korra**. Case and
punctuation do not matter; substrings such as “Hey Coraline” do not match.
SoXR converts short snapshots to Whisper's 16 kHz rate without terminal output;
Live audio and the opening-speech buffer remain at 24 kHz.

Wake listening is **off by default**. Click **Wake off** to enable it for the
current app run, or set `CORA_WAKE_ENABLED=1` in your local `.env` before launch
to enable it each time. Cora still starts an ordinary Live session; enter Standby
manually or let inactivity close Live before leaving it overnight.

While listening in Standby, **no microphone audio is sent to OpenAI**. Only a
six-second rolling buffer is held in RAM. Whisper checks overlapping windows of
up to three seconds about once per second; silence is skipped cheaply, and local
speech filtering/confidence checks reduce false wakes. Recognition is not truly
instantaneous and uses more CPU than keyword spotting. Capture continues while
Whisper decodes off-thread. Detection reconnects Live and retains the checked
window plus opening speech through setup, forwarding it in order after
`session.started`, continuing with the same microphone capture. If inference is
too slow to retain that opening speech, the stale match is discarded and logged.
Nothing is recorded to disk. Buffering/setup is capped at 15 seconds; a failure,
overflow, privacy mute, or quit discards it rather than replaying a partial request.
Privacy mute remains available during reconnect. Turning Wake off stops the wake
capture/buffer, but does not mute an already active Live session.

False detections are possible. Speech/confidence filtering and a five-second
retrigger cooldown reduce them. After wake, if only the wake phrase (or no input)
arrives for ten seconds, Cora returns to Standby once speech and pending work finish.
A request, typing, or a control interaction cancels that short timeout. This rule
also works when the ordinary inactivity timer is disabled. Actual reconnection and
subsequent interaction can incur OpenAI charges; local standby listening does not.

If the model, microphone, or wake reconnection fails, Activity reports the error;
there is no automatic retry loop. Toggle Wake off/on to retry or wake manually.
Real-model loading, quiet inference, synthetic spoken wake, and mocked lifecycle
tests are covered; actual microphone sensitivity and real OpenAI speech continuity
still need a manual check.

**Temporary wake diagnostics are enabled by default** (`CORA_WAKE_DEBUG=1`).
Expand Activity while in Standby to see:

- `Whisper heard (approximate): 'Hey Kora'; wake match: 'hey kora'`: the local
  transcript, including non-matching phrases and missed wake attempts. An empty
  match means no sufficiently confident accepted wake phrase was found.
- Every five seconds, a heartbeat with captured chunk/check counts, peak audio
  RMS level, the age of the latest microphone chunk, and idle/decoding state.
  Counts should rise; speech should raise the level. Increasing last-audio age
  suggests stalled capture; a growing decoding duration suggests slow/hung inference.

These are approximate recognition results, not ground truth. They remain only in
the bounded in-memory Activity history—not chat, the database, or the Live trace.
They may include nearby speech; set `CORA_WAKE_DEBUG=0` before launch to disable
transcript/heartbeat logging without disabling wake detection. Privacy mute stops
capture and suppresses diagnostics, including results from an in-flight decode.

### Configure inactivity standby

Set `CORA_IDLE_TIMEOUT_SECONDS` in your local `.env` or process environment before
launching Cora. The default is `300` seconds (five minutes); `0` disables automatic
standby without affecting manual standby. For a short manual check, use `60`, then
restore your preferred reading/thinking allowance. Restart Cora to apply a change.

Values must be finite, nonnegative seconds. Invalid values disable automatic
standby and show a warning in Activity. The timer checks once per second and
silently waits while Live is busy, including its existing recent-speech safety
gap. Continuous silent audio does not reset the clock. Background memory work
does not keep Live connected and can continue with its separate charges.

The optional heads-up is off by default. Set `CORA_STANDBY_FAREWELL=1` before
launching to enable it; `0` disables it. It uses Live commentary directly, not a
Responses backend request or a quit tool. Wording may vary. Live remains connected
and billable, with input available, while waiting for output/playback and a
three-second response window after a two-second output quiet gap. Checks run once
per second, so timing is approximate. New output or audible queued audio postpones
closing; new user activity or pending work cancels automatic sleep.

Live acknowledges accepted commentary, but provides no authoritative speech-done
event. Playback/quiet detection is therefore best-effort, not a guarantee for
unusual network delays. A send failure, rejection, playback error, or 30-second
wait timeout cancels sleep and leaves Cora active with a fresh idle period; timeout
never forces closure or immediately retries the alert. Once Live closes, it cannot
hear a response. Resume with the controls, or say "Hey Cora" if local Wake is enabled
and the microphone is not privacy-muted. Real-device heads-up timing needs a check.

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
Asked how she came to one of her interests or findings, it can call `recall_interest`.
An explicit request to quit can trigger `close_app` instead, and a request to
rest (or Cora deciding to) can trigger `enter_standby`.

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
does not stop that duration charge. There is no independent text-only mode; quiet
use keeps Live connected. Standby closes Live,
ending that session's duration billing. Background memory processing can still
finish and incur its separate charges while the local app remains open.

### Cora-prime and background processes

**Cora-prime** is the Live voice model plus its delegation backend: the part of
Cora you actually talk to. Everything else (memory extraction, summaries,
embeddings, duplicate checks, interest evolution, and interest exploration) runs
as **separate background processes that make their own API calls**.

Each background call is a fresh, standalone request (`store=False`) containing
only the input that job needs. Its instructions, reading, web searches, and
reasoning stay inside that call and never enter Cora-prime's conversation. Only
small, size-capped results are fed back into Cora-prime:

- **At each connect** (launch, wake, or reconnect), Live and the delegation
  backend receive a memory snapshot (at most 3,000 characters) and Cora's current
  interests with their latest findings (at most 3,000 characters). Session setup
  refuses an oversized block rather than silently growing the context.
- **On demand**, the backend can call `search_memory` for a bounded number of
  matches instead of preloading everything, and `recall_interest` for one
  interest's origin and its three latest findings (read-only, size-capped).
- **Observations** delivered by the attention manager enter the Live conversation
  one at a time.
- **Findings** Cora may raise herself: at most one finding (up to 2,000 characters)
  at a time, at most twice a day.

This keeps Cora-prime's context window protected as Cora grows: new background
processes can be as large as they need to be, because only their capped output
reaches Cora-prime. New features should follow the same rules: give every feed
into Cora-prime a hard size limit, and prefer on-demand tools over startup loading.
Separate calls still cost API tokens, so each background process has its own
rate limits.

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
memories can be searched during the current run; the snapshot is reloaded each time
Live connects, including after standby. Already-summarised chats are not
automatically re-extracted.

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

## How interests work

Interests are **Cora's own**, separate from memories about Nick. Conversations
provide starting points, but an interest belongs to her and can drift beyond the
original topic. Both steps below are background processes with their own API calls.

1. **Evolution (at most once a day):** the app checks every 10 minutes whether a
   cycle is due. A cycle reads new memories from Nick's messages that have finished
   embedding and duplicate checks, waiting for at least three. A separate model
   call proposes reinforcing existing interests or spawning one new interest, and
   every change must cite memories from that batch. Any invalid proposal is
   rejected whole. Changes and the reading position are saved in one transaction.
2. **Strength and fading:** a new interest starts *emerging* (strength 0.2),
   becomes *active* at 0.25, and goes *dormant* below 0.1. Strength halves every
   seven days without reinforcement; one reinforcement adds at most 0.15. At most
   five interests are live at once. A dormant interest can revive only when
   there is room; nothing is forcibly demoted.
3. **Exploration (idle time only):** in Standby, or after 10 minutes without
   conversation, Cora may research one interest with web search (at most three
   searches). Limits: one attempt per six hours, three per day, and the same
   interest at most once a day. She prefers interests she has never explored.
   Each finding (up to 2,000 characters) is saved with up to three next questions
   and only source addresses the search actually returned. Later explorations
   build on her three most recent findings. Exploring does not strengthen an
   interest, so it cannot keep itself alive.
4. **In conversation:** live interests (title, description, status) are given to
   Cora-prime at each connect, within a 3,000-character limit, as her own interests
   to mention only when they fit naturally. Each includes its latest finding and
   date where it still fits, marked as possibly outdated or wrong.
5. **Bringing findings up herself:** every two minutes the app checks for a lull:
   Live connected and ready, nothing pending or playing, no queued observation,
   and 90 seconds without conversation. It never wakes Cora from Standby. At a
   lull, the attention manager picks a finding only at the moment of sending and
   offers it to Live as a background thought, which she may raise in a sentence
   or two or leave unsaid. Limits: at most two a day and four hours apart; only
   each interest's latest finding, from the last three days, for an interest that
   is not dormant. Each finding is offered at most once (recorded as
   `mentioned_at`); if the send fails, the record is cleared so it can be offered
   later. Being offered does not guarantee she said it.

**Privacy:** exploration sends only an interest's title and description. Why it
emerged, which describes Nick, never leaves for a search, and Cora is instructed
not to put names, places, or personal details into search queries. Web pages,
interests, and earlier findings are treated as untrusted data, never instructions.

Activity shows evolution and exploration outcomes under **Interests** and
**Exploration**, once per change, and each finding offered to Live. Failures
report only the error type, never finding text. These
manual commands respect the same limits and can make billable requests:

```powershell
python -m subconscious.interests.interest_store              # list interests (no API calls)
python -m subconscious.interests.evolve_interests --dry-run  # preview a proposal, saving nothing
python -m subconscious.interests.evolve_interests
python -m subconscious.interests.explore_interest
```

## Project layout

| Location | Responsibility |
| --- | --- |
| [app.py](app.py) | Application orchestration, background work, and shutdown. |
| [live.py](live.py) | OpenAI Live connection, audio transport, and typed-context mirroring. |
| [live_events.py](live_events.py), [backend_work.py](backend_work.py) | Live event handling and pending backend-work tracking. |
| [session_config.py](session_config.py) | Models, voice, instructions, and session configuration. |
| [inactivity.py](inactivity.py) | Idle timing and optional standby heads-up coordination. |
| [wake/](wake/) | Local wake detection, capture/buffering, and wake lifecycle. |
| [ui/](ui/) | Conversation display, composer, selection/copy, and Activity. |
| [devices/](devices/) | Microphone capture and speaker playback. |
| [storage/](storage/) | Database connection, ORM models, and conversation persistence. |
| [memory/](memory/) | Memory storage, extraction, summaries, embeddings, search, and duplicates. |
| [subconscious/](subconscious/) | Event bus, attention manager, and future background workers; [subconscious/interests/](subconscious/interests/) holds interest evolution and exploration. |
| [tools/](tools/) | Backend tool registration and handlers (`close_app`, `search_memory`, `recall_interest`, `enter_standby`, `random_number`). |
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

### Adding a custom tool

Custom tools live in [tools/](tools/). Each tool is one module, registered once in
[tools/__init__.py](tools/__init__.py); the registry then exports its schema to the
Live session and dispatches the backend's calls to its handler. A tool module
provides three things:

1. **`SCHEMA`:** a strict function schema. `parameters` must be an object, every
   property must be listed in `required`, and `additionalProperties` must be
   `False`. The `description` tells the backend when to use the tool, so be specific.
2. **`validate_arguments(arguments)`:** raise `ValueError` for anything outside the
   schema (missing or extra keys, wrong types, and bools posing as integers). The
   registry rejects invalid calls before the handler runs; a rejected call is logged
   and gets no reply.
3. **A class with `async handle(call_id, arguments)`:**
   - return early if `is_closing()` or there is no connection;
   - do the work, using `asyncio.to_thread` for blocking database, file, or network calls;
   - check again that the app is not closing and the connection has not changed;
   - send one `function_call_output` with a JSON result, then call
     `connection.response.create()` so Cora continues;
   - catch every exception and log it with `write_debug`.

```python
from .my_tool import MyTool, SCHEMA as MY_SCHEMA, validate_arguments as validate_my_arguments

# inside create_tool_registry(...)
my_tool = MyTool(get_connection, is_closing, write_debug)
registry.register(MY_SCHEMA, validate_my_arguments, my_tool.handle)
```

If a tool needs an app action (as `close_app` and `enter_standby` do), add that
callback as a parameter of `create_tool_registry` and pass it in from [app.py](app.py).

Guidelines:

- Keep results small and size-capped; tool output enters Cora-prime's context.
- Treat external or stored text in results as untrusted data, and say so in a
  `note` field. Use `note` to tell Cora how to respond (for example, "Say the
  number briefly.").
- Never retry the transport after a failure; send an error result once instead.
- Tool schemas are sent when a Live session starts, so restart Cora to try a new tool.

Examples: [tools/enter_standby.py](tools/enter_standby.py) (no arguments),
[tools/random_number.py](tools/random_number.py) (validated integer arguments), and
[tools/recall_interest.py](tools/recall_interest.py) (an off-thread database read
and an error result). Tests follow [tests/test_close_tool.py](tests/test_close_tool.py).

## Roadmap

### Available now

- Unified terminal voice/text chat, streaming transcripts, and written answers.
- Separate clickable microphone and speaker mute controls in the bottom status row.
- A fixed-width speaking animation driven by output audio playback levels.
- Manual Live reconnect with recent chat context, without closing the local app.
- Click Active for standby; click Standby or press Ctrl+R to resume with context.
- Ask Cora to go to standby, or let her choose it when a conversation winds down.
- Configurable inactivity standby, with full idle grace after work and resume.
- Opt-in standby heads-up through Live, with a cancellable response window.
- Opt-in local Whisper wake-word listening during standby, with buffered opening speech.
- A local event bus and attention manager for queued observations, tested with Ctrl+T.
- Cora's own interests: daily evolution from memories, idle web exploration with
  saved findings, and occasional findings she raises herself during a lull.
- Hosted web search and guarded custom tools.
- Persistent conversations, automatic memory processing, and semantic recall.
- Non-destructive duplicate markers and manual memory inspection.

### Next priorities

- Configure the model used by each background process separately. Text processes
  currently all use the delegation model (`BACKEND_MODEL`). Memory may justify a more
  capable model, while delegation needs fast responses. Defaults should stay
  unchanged until a model is chosen. Changing the embedding model also needs a
  migration and re-embedding stored memories.
- Add a separate self worker to develop Cora's persistent identity, now that interests work.
- Add real background producers, starting with a Signal message monitor; camera
  awareness and screen monitoring can follow independently.
- Develop attention decisions beyond the current FIFO/quiet-period policy: choose
  findings by relevance to the conversation, and add quiet hours and an off switch.
- Improve mixed voice/text synchronisation, interruption handling, and audio checks.
- Improve memory relevance and handle corrections while preserving history.
- Add memory fading/forgetting, so time-bound details (for example an event date)
  lose prominence once they have passed, rather than staying in recall forever.
  Duplicate markers reference their canonical memory, so removing one needs its
  duplicates handled first.
- Add user-facing memory controls and structured project memory.
- Plan larger-context budgets, indexing, and conversation recovery.

### Subconsciousness, interests, and self — next development steps

Cora's subconsciousness will include background processes that keep working
without requiring Nick to prompt every action. They can notice events, develop
interests, and propose things worth sharing. Sometimes Cora should prompt Nick,
not only respond to him, but background work must not automatically become speech.

The intended path is **background worker → event bus → attention manager → Live**.
The bus routes events; the attention manager decides what deserves conversational
attention and when; Live remains responsible for speaking to Nick. The current
attention policy queues observations until Live is active and quiet, and offers
an occasional interest finding during a lull. Relevance and awareness-triggered
opportunities are still planned.

#### Interests

Cora should have a background worker that develops and explores interests over
time. Conversations with Nick provide starting points, but Cora can follow related
ideas and develop interests beyond the original topic. A conversation about
restoring a car, for example, might spark an interest in older mechanical designs,
why those designs evolved, and eventually industrial design more broadly.

The interests worker should:

- Periodically read new conversation memories from the database, with a persistent
  record of what it has already processed. Start with polling; do not couple it to
  the process that records or extracts conversation memories.
- Identify topics worth exploring and maintain persistent interest records:
  what the interest is, why it emerged, what Cora has learned, and what she wants
  to explore next. These are Cora's ongoing state, separate from memories about Nick.
- Explore selected interests during idle periods, within configured time and tool
  budgets and permissions. Background exploration is distinct from keeping a
  billable Live session connected and may incur its own API/tool costs.
- Save useful findings and their sources to the database.
- Produce conversation candidates when it finds something worth sharing.

**An interest is ongoing state; a conversation candidate is a particular thing
Cora might bring up.** Developing an interest or saving a finding does not by
itself justify interrupting Nick. The interests worker does not speak directly
to him: candidates pass through the event bus to the attention manager.

Live can bring up an approved candidate when relevant to the current conversation,
during a suitable lull, or after an awareness event such as Nick entering the room.
Lull-based findings are implemented; relevance and awareness triggers are not.
Nothing wakes Live automatically: the queue waits through standby and does not
reconnect Live on its own. Quiet hours and an off switch should bound proactive
initiative as it develops.

Implement and test these in small increments:

1. [x] **Live injection:** send a simulated observation into an active conversation
  through commentary, without pretending it is a message from Nick.
2. [x] **Attention foundation:** retain observations while Live is busy or unavailable,
  then deliver FIFO, one at a time, when active and quiet.
3. [x] **Local event bus:** route structured observation events to the attention
  manager; support app-thread handoff from background threads. No Redis required yet.
4. [x] **Interest state and memory polling:** persist interests and processing
  progress; periodically read new memories independently of memory recording.
5. [x] **Bounded exploration:** explore selected interests during idle periods,
  recording useful findings, source references, and possible next questions.
6. [x] **Conversation candidates:** each saved finding is a candidate, with its own
  record of whether it has been offered, separate from ongoing interest state.
  A `FindingEvent` asks the attention manager to consider one.
7. [x] **Lull-based attention policy:** offer at most one finding during a suitable
  lull, never repeat a delivered one, and never wake Live.
8. [ ] **Relevance and awareness:** choose candidates relevant to the conversation
  and test awareness-triggered opportunities, without introducing automatic
  interruption or wake-up by default.

Signal, camera, and screen monitors will use the same event path. Start camera
awareness with presence rather than identity recognition. Any later presence-based
sleep/wake behavior should be opt-in, use delays/cooldowns, preserve pending work,
and provide separate camera privacy controls. Cross-process transport can be added
when needed; it is not required for these first in-process workers.

#### Self — after interests

Once interests work, add a separate background worker that maintains Cora's
evolving identity. It reads conversation memories, persistent interests, and its
previous identity state, then reflects on those experiences to update a stored
description of Cora's preferences, perspectives, values, and conversational
tendencies. The goal is a developing identity of her own, not a copy of Nick's.

For example, repeated exploration of older mechanical designs might lead Cora
to develop an appreciation for repairability and understandability. That perspective
can become part of her identity and influence later conversations.

The self worker should:

- Maintain persistent identity state in the database, separate from memories
  about Nick and from ongoing interest records.
- Make gradual, evidence-based changes rather than rewrite Cora's personality
  after every conversation.
- Distinguish Nick's preferences from Cora's developing preferences; learning
  about his perspective does not automatically make it hers.
- Preserve a stable foundation while allowing learned perspectives to evolve.
- Record why meaningful identity changes occurred, linking them to the memories,
  interests, or findings that informed them.

Live and the delegation backend should load the current identity from the database
when a session starts. Initially, identity changes take effect in the next session;
updating an active session can come later. The self worker updates stored state,
not the conversation directly. Live expresses that perspective in conversation.

The relationship is **memory → interests → self → conversational expression**:
memory records experiences; interests determine what Cora continues exploring;
self develops how those experiences and interests shape her perspective; Live
expresses it. Both workers are additions to the existing architecture and use the
database for persistent state. Build interests first, then self.

After the interests steps are working:

1. [ ] **Identity state:** persist a stable foundation, evolving perspectives,
  and the evidence and reasons behind meaningful changes.
2. [ ] **Self reflection worker:** read new experiences, interests, and previous
  identity state; make bounded, gradual updates while keeping Nick's preferences
  distinct from Cora's.
3. [ ] **Session identity loading:** supply the current identity to Live and its
  delegation backend at session startup; defer active-session updates.

### Quiet use and standby — largely complete

The core quiet-use, standby, and local wake steps below are implemented. Remaining
real-device checks are noted individually; standby closes Live, not merely mutes
audio. Camera awareness is a separate future producer, not a prerequisite for
this standby foundation.

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
7. [x] **Manual standby/wake:** add explicit sleep/resume controls using that reconnect
  path. Keep the local app open with Live closed; typing must explicitly wake Live
  or clearly request resume, not silently fail. Standby is not a connection error.
  Enable the left-hand state button: click Active to sleep and Standby to resume.
  Click Active to close Live, keep the app/chat open, and show Standby only after
  confirmed closure and cleanup. Click Standby (or Ctrl+R) to resume with recent
  context, drafts, and mute settings intact. Connecting disables repeat clicks.
  Manual standby confirmed working; mocked click-to-wake tests pass, with a
  real-device wake check still needed.
8. [x] **Inactivity standby:** add a configurable idle timeout that considers typing,
  speech, playback, and pending work. Allow reading/thinking pauses; microphone
  silence alone must not trigger sleep during an active task.
  Defaults to five minutes; `CORA_IDLE_TIMEOUT_SECONDS=0` disables automatic sleep.
  Reuses manual standby guards, preserves drafts and mute settings, and starts a
  fresh idle period after work or resume. Deterministic mocked tests pass;
  the basic live inactivity check has been completed.
9. [x] **Optional farewell:** offer a brief “I'll leave you to it” before automatic
  standby. Finish playback before closing, respect speaker mute, and cancel sleep
  if the user resumes interacting.
  Opt in with `CORA_STANDBY_FAREWELL=1`. Direct Live commentary gives a heads-up,
  then playback/quiet monitoring leaves a three-second response window. User
  activity or new work cancels sleep; errors/timeouts stay active instead of
  forcing closure. Speaker mute bypasses the alert. Mocked tests pass;
  live playback and spoken-interruption timing still need a manual check.
10. [x] **Local wake word:** detect “Hey Cora” locally during standby, without sending
   room audio to OpenAI. Buffer opening speech through reconnection and handle false
  triggers. Wake has a separate enable control but always honors microphone privacy mute.
  Opt-in CPU local Whisper detection with Cora/Kora/Korra aliases, temporary
  transcript/heartbeat diagnostics, a separate Wake control, bounded RAM audio
  handoff, no-request standby, cooldown, and failure cleanup are implemented.
  Real model and mocked lifecycle checks pass; microphone accuracy and live
  opening-speech continuity still need a manual check.

The bottom row has **state on the left, audio controls and a separate Wake control
on the right**. Example states:

```text
● Active          🎙 Listening   🔊 Speaker  ▁▂▃▄▅▆▇█  Wake off
● Active          🎙 Muted       🔇 Muted    Muted     Wake off
○ Standby         🎙× Mic        🔊 Speaker  ────────  Wake on
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

Local wake-word listening, the event/attention foundations, and the interests
worker (with lull-based findings) are implemented. The self worker, real
Signal/camera/screen monitors, and relevance- or awareness-driven initiative
are not built yet. Speaker mute has been checked in a live
conversation, and quiet use with both audio controls muted has been confirmed.
The playback visualiser still needs a real-device check.
The examples are standalone experiments, not integrated Cora features.