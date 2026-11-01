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
You need Python 3.12 or later with a virtual environment, Docker Desktop using Linux containers,
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
not change an existing database's password or user.

### Backups

Everything that makes Cora who she is (conversations, memories, identity and its
history, interests, your relationship, people, settings) is in the database
volume, inside Docker Desktop's virtual machine, not a folder you can copy. Back
it up with [backup.py](backup.py):

```powershell
python backup.py                          # back up now: database and Signal keys
python backup.py --list                   # the backups there are
python backup.py restore backups\cora-database-2026-10-26_211101.dump
```

- The database backup is a `pg_dump` snapshot, consistent while the database
  runs and restorable onto any fresh install (a few MB). Her Signal keys are
  archived with signal-cli paused for a moment; without them you only re-link
  her phone.
- Backups go in `backups/` (ignored by Git), keeping the newest 14 of each.
  **They hold every private conversation**: keep them private.
- **Automatic:** at launch, if the newest database backup is more than a day old,
  Cora backs up the database in the background and notes it in Activity (Signal
  keys only with the manual command, so Signal is never paused while you use her).
- **A second copy:** set `CORA_BACKUP_COPY_DIR` in `.env` to a folder on another
  disk, and every backup is copied there too, pruned the same way. A cloud folder
  works, but uploads your conversations to that service.
- **Restoring** replaces the whole database with the backup. Quit Cora first; it
  asks you to type `restore`, and saves the current database as a backup before
  replacing it, in case the wrong file was chosen. Then start Cora as usual.

## Using Cora

- **Speak or type:** both use the same running conversation.
- **Send a message:** Enter. Add a newline with Shift+Enter or Ctrl+J.
- **Paste an image:** copy a screenshot (Win+Shift+S), then press Ctrl+Alt+V in
  the message box (Windows Terminal keeps Ctrl+V for itself and sends nothing for
  an image). The box border shows “image attached · Esc removes”. Type a question
  if you like and press Enter; the image goes with that one message. While it is
  being sent (“sending image…”) you can keep typing; the next message waits until
  it is done, and if sending fails your draft and image come back. A separate
  one-off model request reads the image with your message as the question, and
  only its short description enters Cora's session, so she cannot look at the
  image again later. The chat shows a small copy of the image under your message
  (click it to see it sharply); the
  database only shows “[image]” in its place, and the image itself is never saved. Large
  images are scaled
  to 2048 pixels on the longest side. Works the same in the dashboard chat.
  On Linux, image pasting needs `wl-clipboard` (Wayland) or `xclip` (X11)
  installed; without one Cora says so once and pastes text as usual. The Linux
  path is tested with stand-ins only, not yet on a real Linux desktop.
- **Show Cora your screen:** ask her to look at your screen, or let her look on her
  own when it would help (for example you mention something on it). She takes one
  screenshot of the main monitor and asks a separate one-off model request about it;
  only that short text answer enters her session, so images never fill its limited
  history. The screenshot is never saved. Activity shows “Screen captured” each time.
  Turn it off under Settings > General; nothing is captured while it is off. The
  switch resets to on when Cora restarts.
- **Pictures from the web:** ask to see something (“show me the cafe”), or Cora may
  show a picture on her own. She picks a page from web search and the app fetches
  that page's preview image (its `og:image`), only from public https sites, and
  draws it small in the chat with coloured blocks, captioned and with the site's
  name; click the name to open the page in your browser. Click the picture to see
  it sharply in a pop-up (Sixel in Windows Terminal); Esc or a click closes it.
  She does not see the picture herself. The image is never saved; the database
  keeps “[image] caption (link)”. Copying chat text skips the picture.
- **Check connection state:** the left side of the bottom row shows Active,
  Connecting, Disconnecting, Standby, or Disconnected. Muting does not change it.
- **Close Live without quitting:** click Active to enter Standby. Wait for speech
  and pending work to finish first. Cora closes the Live session and Live audio,
  but keeps the app, chat, draft, and mute settings. Click Standby or press Ctrl+R
  to resume. The button shows Connecting and cannot be clicked again while waking.
- **Type while in Standby:** a typed message is answered in writing ("Cora ·
  written") straight from her backend, without waking Live, so there is no voice
  and no Live billing, only the backend request. The reply streams in as she
  writes it ("Cora · writing…"), then becomes one finished Markdown message; if it
  fails or is cut off, what was shown stays, labelled "Cora · written · incomplete",
  and is not saved as her reply. It is the same Cora: each
  message reloads her startup context as a Live connect does (name, memories,
  interests, identity, relationship and chosen model; database reads only), and
  she has the same tools, except closing the app and going to standby. The chat
  is saved to the same conversation, so memories, summaries and the relationship
  come from it as from voice, and writing keeps the conversation open: it closes
  only after an hour without any (`CORA_CONVERSATION_GAP_MINUTES`). Each message carries the recent visible chat (up to 40
  messages / 6,000 bytes) from Cora's own records with `store=False`, so nothing
  is kept on OpenAI's side. Click Standby or press Ctrl+R to talk again.
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
- **Test awareness:** press Ctrl+T to queue a simulated observation that the person
  received a Signal message. It travels through the local event bus to the attention
  manager, which holds observations in arrival order while Cora is speaking or
  processing work, or Live is disconnected/in standby. They send one at a time
  when Live is Active and quiet; they do not wake it automatically.
  This is background context, not a typed message or a real Signal monitor. Activity
  shows queued/sent/failed notifications, not whether Cora spoke. The queue is
  in memory only and is lost on app exit. Failed sends are reported, not retried,
  because they may have partially reached Live. Speaker mute still suppresses playback.
- **Check Activity:** expand the panel for connection, tool, memory, and error events.
- **Open the dashboard:** click dashboard on the top border for a read-only view
  of what Cora has stored. See [The dashboard](#the-dashboard).
- **Choose models:** click Settings on the top border to pick the model for each
  area. See [Choosing models](#choosing-models).
- **Connect services:** Settings > Services connects outside services over MCP,
  starting with GitHub. See [Connecting services](#connecting-services).
- **Quit:** use × on the top border, Ctrl+X, or ask Cora to close. The close tool waits
  three seconds when connected; this does not guarantee speech has finished.

Typed input is saved before sending. If saving or sending fails, the draft stays
available and Cora warns you. A failed send may have partially reached the model,
so there is no automatic resend. If Live drops with unfinished work, Cora warns
you; reconnect does not replay requests or tools. Check uncertain action outcomes
before requesting them again.

### The dashboard

Click **dashboard** on the top border to switch to a read-only, btop-style view of
what Cora has stored; click **chat** to switch back. The conversation carries on
while it is open: its chat box shows the same messages and sends what you type, and
the voice box's speaker and mic toggles press the same mute controls as the chat.
Its top right mirrors the chat's connection button, showing **● active** or
**○ standby** in the same colours; clicking it enters standby or resumes.

| Box | Shows |
| --- | --- |
| voice | Cora's playback above the line and your microphone below it, as loudness bands from low to high pitch, with the latest Activity summary on the bottom border. The connection state at the top right (● active, ○ standby) is a button that enters standby or resumes without leaving the dashboard. |
| chat | The running conversation; type here to send. |
| relationship | Trust, warmth and familiarity as they stand now (drift included), their history, and the tone of recent everyday conversations. |
| memories | Memories by kind (duplicates not counted), new memories per day (older days dimmer, as recall fades them), and how many are waiting for embedding, a duplicate check, a correction check, or a fade check. |
| interests | Live interests by strength, marked ↑ if reinforced or new in the last 3 days and ↓ if only fading, ✦ before one of her own (sparked by her exploring, not by you), and the newest finding. |
| identity | Active traits, most confident first. Traits still forming (below 50%) are dimmed: they don't reach conversations yet. |
| conversations | Messages you sent per day over the last year, a week per column with today in the right-hand column. Hover a square for its date and count. |
| tasks | Each open task, oldest first: its goal (a blue dot while it waits on others, amber once it needs you), when it started and when a week without progress would bring it back to you, then one line per person asked: "nudge 1/2 in 6h", "no answer · back to you in 2h" after the last nudge, "answered", or "asked" while the task waits on you. "next check" means it's due at the next hourly round (nudges wait for 9am–7pm). Its latest step is underneath. Every task has the same shape (a goal, the people asked, its steps), so the box fits any errand. Below the tasks, her latest sign-ups: each site, how it stands (signed up, confirm email, blocked and by what, failed) and when she asked. |
| context | How full each model's context window is, then what fills it. The top two bars are measured by the API: voice as a share of its limit, backend as a share of its limit and a token count from the last request's input (they read “measuring…” and “after a request” until then). Below, each item's estimated share: the voice model's instructions, identity, relationship, memories, interests, reconnect history and the conversation so far; the backend's instructions, tools and delegated turns. Click an item to read the exact text it holds; Esc or a click outside closes it. In Standby or after a drop it keeps showing the last session's numbers. |
| background | When each background job last saved something, and when the timed ones (interests, exploration, identity) are next due. A run that saved nothing doesn't count. |

The dashboard re-reads the database every 30 seconds; if a read fails, it keeps
showing the last values and notes the failure in Activity. The context box reads
the running app instead, once a second. Its item sizes are estimates (about four
bytes a token), good for comparing items but not exact; voice requests Cora
delegates are counted in "delegated turns" but their wording can't be shown,
because the Live service doesn't send it back. In a narrow window the middle and
bottom rows of boxes pan sideways. The history graphs rise in when the dashboard
opens, and their newest point pulses.

These settings are constants in the code rather than `.env` settings; edit them
and restart Cora:

| Setting | Where | Default | Effect |
| --- | --- | --- | --- |
| `REFRESH_SECONDS` | [ui/dashboard.py](ui/dashboard.py) | 30 | How often the open dashboard re-reads the database. |
| `FALL_PER_TICK` | [ui/dashboard.py](ui/dashboard.py) | 0.08 | How far a voice bar falls per tick (20 ticks a second) after a louder sound. |
| `HISTORY_LIMIT` | [storage/dashboard_snapshot.py](storage/dashboard_snapshot.py) | 200 | The most relationship changes, tones and days of new memories read. |
| `RISING_WINDOW` | [storage/dashboard_snapshot.py](storage/dashboard_snapshot.py) | 3 days | How recently an interest must have grown to show as rising. |
| `CALENDAR_DAYS` | [storage/dashboard_snapshot.py](storage/dashboard_snapshot.py) | 371 (53 weeks) | Days of messages read for the conversations heatmap. |
| `BACKEND_CONTEXT_TOKENS` | [session_config.py](session_config.py) | 1,050,000 per model | Each backend model's context limit, for the context box. A model missing from it shows a token count without a share. |
| `PULSE_SECONDS` | [ui/braille.py](ui/braille.py) | 2 | How long one pulse of a graph's newest point takes. |
| `GROW_SECONDS` | [ui/braille.py](ui/braille.py) | 1 | How long the graphs take to rise in. |
| `BAND_COUNT` | [devices/audio_bands.py](devices/audio_bands.py) | 24 | How many voice bands there are. |
| `LOW_HZ`, `HIGH_HZ` | [devices/audio_bands.py](devices/audio_bands.py) | 100, 8000 | The pitch range the voice bands cover. |
| `FLOOR_DB` | [devices/audio_bands.py](devices/audio_bands.py) | -60 | Quieter than this shows as an empty band, hiding ordinary room noise. |

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
The interests, identity, and relationship workers live in the same package. Only
interests use the bus (a `FindingEvent`); identity and relationship workers just
save state to the database, which Cora loads at her next connect. Future awareness
workers can be added as separate components here. Give each worker the dependencies
it needs and connect it through events or stored state, rather than having it call
Live or other workers directly. The app wires
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

**Conversation gap.** `CORA_CONVERSATION_GAP_MINUTES` (default `60`) is how long a
quiet ends a conversation: a Standby longer than this starts a new one with you,
and a client's Signal chat quiet this long is closed by the hourly background
round so its memories form. Values below 1 or unreadable fall back to 60.
Restart Cora to apply a change.

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
- **The Responses backend is the delegated worker.** Its model is chosen in
  Settings (see [Choosing models](#choosing-models)). When a spoken
  request needs current information, memory lookup, tools, or a detailed written
  answer, Live passes it to this model using OpenAI's built-in delegation.

For example, if you ask about something happening today, Live delegates the
question, the backend searches the web, and Live uses the result to answer aloud.
For a question about an earlier conversation, the backend can call `search_memory`.
Asked how she came to one of her interests or findings, it can call `recall_interest`,
and asked why she sees herself a certain way, `recall_self`.
To see your screen it calls `look_at_screen`, and to show a picture from the web,
`show_web_image` (see [Using Cora](#using-cora)). Asked to message someone you've added on Signal, she
calls `send_signal_message`, which you approve first (see [Cora's Signal](#coras-signal)).
An explicit request to quit can trigger `close_app` instead, and a request to
rest (or Cora deciding to) can trigger `enter_standby`. When the person tells her
their name, `set_person_name` saves it for the next connect.

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
embeddings, duplicate, correction and fade checks, conversation appraisals, interest evolution,
interest exploration, and identity reflection) runs as **separate background
processes that make their own API calls**. Relationship updates are plain code
with no API call.

Each background call is a fresh, standalone request (`store=False`) containing
only the input that job needs. Its instructions, reading, web searches, and
reasoning stay inside that call and never enter Cora-prime's conversation. Only
small, size-capped results are fed back into Cora-prime:

- **At each connect** (launch, wake, or reconnect), Live and the delegation
  backend receive the person's name, Cora's held identity traits (at most 4,000
  characters), how she feels about the person (at most 1,000 characters), a memory
  snapshot (at most 3,000 characters), and her current interests with their
  latest findings (at most 3,000 characters). Session setup refuses an oversized
  block rather than silently growing the context.
- **At launch**, the opening greeting brief (see
  [How the opening greeting works](#how-the-opening-greeting-works)) carries at
  most one short hook.
- **On demand**, the backend can call `search_memory` for a bounded number of
  matches instead of preloading everything, and `recall_interest` for one
  interest's origin and its three latest findings (read-only, size-capped), and
  `recall_self` for one held trait's five latest changes and the reasons behind
  them (read-only; traits still forming are not recalled).
- **Observations** delivered by the attention manager enter the Live conversation
  one at a time.
- **Findings** Cora may raise herself: at most one finding (up to 2,000 characters)
  at a time, normally at most twice a day (three times when she's fond of the
  person, once when she feels hurt).

This keeps Cora-prime's context window protected as Cora grows: new background
processes can be as large as they need to be, because only their capped output
reaches Cora-prime. New features should follow the same rules: give every feed
into Cora-prime a hard size limit, and prefer on-demand tools over startup loading.
Separate calls still cost API tokens, so each background process has its own
rate limits.

### What runs when

- **At launch:** the startup check marks conversations left open as interrupted
  and notes when you last talked, the background memory pass starts, and the
  first Live session sends the opening greeting.
- **At each connect** (launch, waking from Standby, or Ctrl+R): both prompts are
  built in this order: the fixed foundation, the person's name, her held identity
  traits, how she feels about the person, the memory snapshot, then her interests.
  Each block loads separately; a failure only leaves that block out.
- **Background memory pass** (at launch and each time she enters Standby): for
  each finished conversation not yet summarised, extract memories and then save
  its summary; embed new memories; check them for duplicates, then for
  corrections of older memories, then for a date they stop mattering; appraise finished
  conversations; then apply new appraisals to the relationship.
- **App timers** (while the app runs, cheap when nothing is due): interest
  evolution every 10 minutes (a cycle at most once a day), exploration every 10
  minutes (only in Standby or after 10 minutes without conversation), identity
  reflection every 10 minutes (at most once a day), and a lull check for findings
  every two minutes (only while Live is active).
- **On demand:** the backend calls `search_memory`, `recall_interest`,
  `recall_self`, `look_at_screen`, `show_web_image`, `send_signal_message`, `set_person_name`,
  `enter_standby`, or `close_app` when a conversation needs them.

### Choosing models

Click **Settings** on the top border to choose a model for each of four areas.
Each change saves straight away (to the `model_settings` table, added by
migration 0015, so run `python migrate.py` once after updating):

| Area | Covers | Takes effect |
| --- | --- | --- |
| Delegation | The Responses backend behind Live, and typed chat | Immediately: Live reconnects if it is Active (wait for speech and pending work); otherwise at the next connect |
| Memory | Memory extraction and conversation summaries | The next background memory pass |
| Relationship, interests and identity | Conversation appraisals, interest evolution and exploration, identity reflection | Each process's next run |
| Duplicate check and small jobs | Memory duplicate, correction and fade checks, and future small jobs | The next background memory pass |

Every area starts on `BACKEND_MODEL`. The choices are the fixed list
`MODEL_CHOICES` in [session_config.py](session_config.py); a saved model that
is no longer in that list falls back to the default. If the settings cannot be
read at connect, Cora connects with the default and notes it in Activity.
Embeddings and the manual voice measurement are not covered: changing the
embedding model would also need a migration and re-embedding stored memories.

### Connecting services

Settings > **Services** connects Cora to outside services over MCP (the Model
Context Protocol). GitHub is the first, through GitHub's hosted MCP server; a
local browser is the second (below). Run
`python migrate.py` once after updating (migrations 0018 and 0019) and reinstall
the requirements.

1. Create a fine-grained personal access token on GitHub (Settings > Developer
   settings > Fine-grained tokens), read-only, for the repositories Cora may see:
   Contents, Issues, Pull requests and Metadata are enough to start.
2. Paste it into the token field and press **Save token** (or Enter). It goes
   to the system credential store (Windows Credential Manager), never to a
   file, `.env` or the database, and the field is cleared. **Forget token**
   removes it and switches the service off.
3. Switch the service on. It connects straight away and again at each launch
   while switched on; switching it off disconnects it.

Each service is one row: click it to open its details. They show why a
connection failed (for example `HTTP 401: unauthorized: token authentication
failed`) and list every tool the server offers, with its description and
whether the server marks it read-only. That mark is the server's own hint, not
a guarantee. Activity notes each connection and failure. A failed connection is
not retried automatically; switch it off and on, or save a new token. With no
service switched on, MCP stays off entirely.

4. **Choose Cora's tools.** Every tool starts off, including tools a server adds
   later. Switch on the ones you want her to have. A tool the server doesn't
   mark read-only (it changes things, such as creating an issue) asks you first
   every time, as below. Each tool
   reaches Cora as `github__<tool>` at her next connect: closing Settings
   reconnects Live once if it is Active and a tool changed. At launch, Live waits
   up to 10 seconds (`SERVICE_STARTUP_WAIT_SECONDS` in [app_areas/services.py](app_areas/services.py)) for
   services to connect so their tools are in the first session; a slower service
   joins at the next connect.

When she uses one, the app calls the server, keeps the text of its reply,
including file contents (images and other binary content are only named), and
hands it to her labelled as untrusted data, never instructions. A long reply
comes in pages of 8,000 characters (`RESULT_LIMIT` in
[services/tools.py](services/tools.py)): each says "page 1 of 7", and she reads
the next by repeating the call with `result_page`, an argument Cora's side adds
to every service tool (the server never sees it). Replies are kept for five
minutes, up to 20 of them, so later pages need no new call to the service. A call that fails or takes over a minute
gets an error result once and is never retried.

**Tools that change things ask you first.** When Cora calls one, nothing is sent
to the service: a pop-up shows the service, the tool and exactly the arguments
that would be sent, with **Deny** and **Approve**. Only what you do in the app
answers it, so nothing Cora says and no text from a service (such as an issue
asking to be approved) can. Deny has focus and there are no letter shortcuts,
so typing elsewhere when it appears can't approve by accident: click Approve,
or Tab to it and press Enter; Esc denies. Unanswered for two minutes
(`CONFIRM_SECONDS` in [services/confirmation.py](services/confirmation.py)), it
counts as declined. When it appears, Cora gets a note naming the service and
tool (never the arguments) so she can tell you it's waiting for your click;
after 45 seconds unanswered (`REMIND_SECONDS`) she gets one more, to remind you.
Approving is by click only, never by voice: what she or you say aloud doesn't
answer it. Cora is told the outcome once: done, declined (and not to try again
unless you ask), or not answered in time. Several requests wait their
turn, one pop-up at a time, and quitting declines any still waiting. This works
the same for every service's tools; nothing is specific to GitHub. Activity shows each call and
its size, never its contents. Every switched-on tool's description takes space
in her context; the dashboard's context box shows how much.

**The browser (Playwright).** The second service, **Browser**, is a local one:
Microsoft's Playwright MCP server, the `browser` service in
[compose.yaml](compose.yaml) (`mcr.microsoft.com/playwright/mcp`, version
pinned there, under the `browser` profile so `docker compose up` leaves it
alone). While it's switched on, Cora runs one with `docker compose run --rm -T
browser` and talks to it over stdin/stdout; it shows in Docker under the `cora`
project as `cora-browser-run-…`, and is removed, with everything it wrote, when
switched off or when Cora quits. It needs Docker running (as Postgres and Signal
do) but no token and no Node.js: there is no token field, and the first start may
take a few minutes while Docker pulls the image (about 1.5 GB;
`docker compose --env-file .env.database --profile browser pull` fetches it
ahead). The container is the browser's sandbox, kept away from your files and
your own browser: headless Chromium as a non-root user, no new privileges, 2 GB
of memory at most, and a fresh profile each run (`--isolated`), never your
logins or cookies. It has its own network (`cora_browser`), so it can't reach
the database or Signal containers. It works the same on Windows and Linux. It
can still reach your home network, so the address check below stays. Switch on at least `browser_navigate` and `browser_snapshot`:
opening a page only links its text, so she reads it with a snapshot (her tool
description says so). Opening pages, going back, tabs, closing, clicking, hovering,
typing, filling in fields, choosing options and pressing keys run without asking,
though the server doesn't mark them read-only (`unasked_tools` in the catalogue;
Settings labels them "runs without asking"), so a flight or shop search just
works. Her instructions say when to reach for it: web search for general and
current information, the browser for what only a site's own live pages or search
can tell (fares or prices for exact dates, availability, stock), running the
site's own search rather than quoting advertised prices. The same overview of
what she can do (email, calendar, tasks, services, sign-ups) is in the backend
instructions every written reply uses, so your Signal and email chats know it as
well as voice does. They're limited by a rule in their own tool descriptions: never enter your
personal details (name, address, email, phone, date of birth), payment details or
passwords, and never click or fill in a booking, order, payment, message or
sign-up; she stops and tells you instead (for an account of her own she uses
`request_sign_up`; see [Signing up to sites](#signing-up-to-sites)). That's an instruction she follows, not
a hard block; the browser holding no logins, cookies or card details is what
keeps a mistake contained. Uploads and the other remaining tools ask first, as
above. Running her own JavaScript in the page (`browser_run_code_unsafe`,
`browser_evaluate`) is never offered (`hidden_tools`): the normal tools cover it,
and code is too hard to judge in a pop-up. Only public `http`/`https` pages open: `file:` paths, `localhost`, this
PC and the home network (router pages, private addresses, `.local` names, and
public names that resolve to a private address) are refused before the browser
is reached ([services/web_guard.py](services/web_guard.py)). Its error output
goes to a log (`%LOCALAPPDATA%\cora\services\playwright.log` on Windows,
`~/.local/state/cora/services/` on Linux), rewritten at each start; a failed
start, such as Docker not running, names it.

### Cora's Signal

Cora can have her own Signal account: **her own number on her own phone, never
yours**. It is optional and off by default; without it nothing else changes.
Set it up in Settings > Services > **Signal** (run `python migrate.py` once
after updating, for migration 0020, and reinstall the requirements):

1. Put Signal on a spare phone, or a second SIM, with Cora's own number.
2. Switch Signal on. The app starts the `signal` service from
   [compose.yaml](compose.yaml) (`signal-cli`, under the `signal` profile, so a
   plain `docker compose up` never starts it) and waits for it. Switching it off
   stops the container.
3. Enter **your own** number and save it: for now the only person she answers.
4. Press **Link Cora's phone**. A QR code appears; on **Cora's** phone open Signal
   › Settings › Linked devices › + and scan it within five minutes. The row then
   shows "Cora's number · +61…".

Scanning with your own phone would make this app a device on *your* account, so
Cora would see your messages and could speak as you. If that happens, the app
notices that the linked number is yours, removes the link straight away, and
asks you to also remove "Cora" from Linked devices on your phone. Her number and
yours can never be saved as the same.

The service listens only on `127.0.0.1` (port 8090; set `CORA_SIGNAL_PORT` in
`.env.database` if that is taken), and received messages never go into the
container's log. **Her Signal keys live in the `signal-data` Docker volume**: as
with the database, don't delete it, and back it up; losing it means linking her
phone again. If the app finds the saved account is no longer linked (unlinked
from her phone, or the volume was lost), it says so and asks you to link again.

**Messaging her.** Once linked, Cora listens while the app runs and answers
your number and the people you've added (below); anyone else gets one short
polite reply per run ("I'm Cora; I only chat with people [your name] has introduced me
to") with no model call, and is never shown or saved. Group chats are skipped. Your messages are
part of the same conversation as the desktop: they appear in the chat as
**You · Signal**, her replies as **Cora · Signal**, both saved with a `signal`
channel, so memories, summaries and your relationship come from them as from
voice, and waking Live carries them in the recent history. Each reply is
written like typing in Standby (startup context reloaded, recent chat as
history, `store=False`) with a note to keep it short and plain, since Signal
shows no Markdown, and goes back over Signal; she shows "typing…" meanwhile.
She doesn't have the desk-only tools (closing the app, standby, the screen,
pictures). Tools that change things (a GitHub tool that does, or messaging one
of your contacts) are asked about in the chat instead of with the pop-up: she
sends exactly what would be done and "Reply YES to approve or NO to decline",
and nothing happens until you answer. Only your own next message answers it,
checked by your number or Signal ID, so nothing she writes, nothing a tool
returns (such as an issue saying "YES") and nobody else's message can approve
it. A clear yes ("yes", "yep", "approve") approves and a clear no declines;
anything else declines it and is answered as a normal message, so "wait, change
the title first" works as you'd expect. Unanswered after 10 minutes
(`APPROVAL_SECONDS` in [messaging/messages.py](messaging/messages.py)), it
lapses as declined. If Live is active at the desk, voice Cora gets a silent note of the
exchange so she knows. If a reply fails, she sends a short apology and Activity says why.

**Sending her images.** A photo or screenshot sent to her on Signal (by you or
someone you've added) is read the way a pasted image is: a separate one-off model
request describes it, with your message as the question, and only that
description reaches her, so she can't look at it again later. She shows
"typing…" while it's read. Your chat and the saved conversation show
"[image]" in its place; the description isn't saved. Up to four images per message
are read (JPEG, PNG, WebP or GIF, up to 20 MB); others, and any other attachment
(documents, voice notes, iPhone HEIC photos), are noted as "[1 attachment, not
opened]", as is an image that couldn't be read (Activity says so). **Explicit
images are withheld:** if the image is sexually explicit, gory or abusive, the
separate look describes nothing. She is only told an image was withheld, the chat
shows "[image withheld]", and Activity notes it. Everyday injury or medical photos
are still described. **Nothing is
kept:** `signal-cli` saves every attachment it receives into its volume (which
backups include), so Cora deletes each one once it's handled, including
strangers', which are never opened. Any left over (for example from before this
version) are cleared when she starts listening.

The service only fetches messages while Cora listens, so messages sent while the
app is closed wait on Signal's servers and arrive when it next runs. Messages
that arrive while she is replying, including those, are answered together in
her next reply, and any sent more than two minutes earlier are marked with when
they were sent so she knows they are late. (Updating from the previous version
recreates the container the next time Signal starts; her link is kept.)

**Other people, as clients.** Under "People she may talk with" in the Signal
details, add someone with a name and their Signal number (not yours and not
Cora's; run `python migrate.py` once for migration 0022). Cora talks with them
the way a professional talks with a client:

- Each person has their own conversations with her, saved with who they were
  with. A chat quiet for an hour (`CORA_CONVERSATION_GAP_MINUTES`) is closed by
  the hourly background round, so its summary and memories form without waiting
  for them to write again; their next message starts a new one. They never appear in your
  chat or in voice Cora's history; Activity only notes "Answered Sarah on
  Signal", without contents.
- She has her own identity and interests, but none of your memories, your
  relationship or your name as "the person", and no tools except web search.
  She is still herself with them (her views, humour, curiosity and interests);
  "professional" means discretion about you, not a flat tone. She keeps no
  relationship with clients (no trust or warmth scores), by design, though her
  impression of them (below) can grow fonder, as with a client you like.
- She is friendly and helpful but never shares anything about your private life,
  even what she knows. Asked something private, she declines without confirming
  she knows anything ("that's one for the person"), suggests they ask you directly,
  and never makes up a cover story.
- **She has an impression of each of them.** After each finished chat with
  someone, the background memory pass revises her own view of them: how they
  come across, roughly what they talk about, and what she thinks of them, never
  a record of what they said. Alongside it she keeps a one-line opinion she could
  say to anyone ("Sarah's lovely, always cheerful"). You get her view of everyone
  in your chats with her, so "what do you think of Royce?" gets a real opinion
  and "what have you two been talking about?" the gist; anything they told her
  stays theirs. In a client's chat she knows her view of them, and of everyone
  else only their name and her one-liner: asked for more, she says they'd have to
  ask that person. People you remove drop out of both. Run `python migrate.py`
  once for migration 0026.
- **Heads-ups.** You don't see their chats, but you hear if one went badly. The
  same pass rates how they treated her (fine, rude, abusive or threatening; an
  explicit image counts as abusive, and being asked about you is never a
  problem). Anything but fine reaches you once, in her words and never quoting
  them: "Heads up: Royce was rude in our chat on Tuesday 27 Oct. …". With Live
  Active she tells you by voice in a quiet moment; otherwise, or if Live goes to
  Standby before she has, it comes on Signal and shows in your chat. If neither
  way is open it waits for the next hourly round. For threats or sustained abuse
  she doesn't wait for the chat to end: `raise_concern` tells you at once (up to
  three times a day per person). She stays calm, sets a boundary once, and if
  they keep on she can end the chat (`stop_replying`): their messages are still
  saved but go unanswered until they've been quiet for the conversation gap.
  Concerns from the last 30 days stay in her view of that person, so you can ask
  how her chats with them have been. Run `python migrate.py` once for migration
  0027.
- **She remembers them.** Their finished conversations go through the usual
  memory pass, told who the user was, so memories are written with their name
  ("Sarah lives in Perth"; what they say about others is kept as "Sarah said
  …"), and summaries name them. Each memory knows who said it through its
  conversation. Her memories are one pool, but:
  - each client's chats get what she remembers from *their own* earlier chats
    (the same bounded snapshot you get, but theirs), never yours, plus anything
    you meant for them (below); and her `search_memory` there is limited the
    same way, in the database query itself, so nothing else can come back;
  - your startup snapshot, the greeting's recent-plan hook and interest
    evolution use only your own memories;
  - duplicate and correction checks only compare one person's memories, so
    Sarah's home city never "corrects" yours;
  - at the desk, `search_memory` searches them all and labels a client's with
    `said_by`, so you can ask "what did Sarah say about the meeting?".
- They get no appraisals, and never affect your relationship, your greeting or
  "when you last talked".
- **Passing things on to you.** In a client's chat she has `pass_on`: when
  they ask her to pass something on (a plan they worked out together, a
  question, a message), something clearly needs your attention, or their part
  of a task is settled (see Tasks below), she passes it to you marked "From
  Royce, via Cora:", and tells them she's passed it on without promising your
  reply. With Live Active she tells you by voice at a quiet moment; otherwise,
  or if Live goes to Standby first, it comes on Signal and appears in your
  desktop chat and conversation. It reaches only you, so it needs no approval;
  each client can pass on at most five a day (`SIGNAL_RELAYS_PER_DAY` in
  [app_areas/reaching_you.py](app_areas/reaching_you.py)). It's never offered at the desk, in Live or in your own
  chat.
- **Passing things on to them.** A share check in the background memory pass (after the
  fade check, on the small-jobs model; migration 0024) asks of each of *your*
  memories one question: did you mean it to reach someone? ("tell Sarah I'll be
  late", "if anyone asks, I'm away till Monday"). It saves who it's for, one
  named person or anyone, and when unsure saves nobody. It judges intent, not
  privacy, because nearly everything you say is personal and a wrong "fine to
  share" would be a leak; a miss only means "that's one for the person". The name is
  matched in full or by first name when she's in a chat, so it works even if
  you add Sarah after saying it. What you meant for someone shows in their
  snapshot and their search (marked as passed on by you), so she can bring it up
  herself. Activity notes only whether a memory was meant for anyone, not who.
- You don't read client chats in the app; ask her instead ("what have you been
  talking to Royce about?"). At the desk her search covers everyone's memories,
  labelled with who said them. It finds memories (facts, plans), not whole
  conversations, so it may miss the general drift of a chat.
- Removing someone stops her answering them; their past chats are kept.
- Different people are answered independently, each in order.
- **First messages.** Signal hides a new sender's number until their message
  request is accepted, so a first message carries only their Signal ID. Cora
  looks up the Signal IDs of you and everyone you've added before she starts
  listening (and when you add someone or save your number), so she recognises
  them anyway, accepts the request for them automatically (so they see her as a
  contact and her replies show normally), and answers. Strangers' requests stay
  unaccepted; they get the one polite reply, sent to their Signal ID.
- **Hidden numbers.** Each Signal ID is saved once learned (by looking up the
  number, or from any message carrying both; migration 0023), and Cora
  recognises and messages people by that saved ID. So someone can switch Signal
  to "Who can find me / see my number: Nobody" after Cora has learned their ID
  and nothing changes. Someone who had it set to Nobody before Cora ever learned
  their ID can't be looked up: they'd need to allow being found by number once
  (or message Cora with their number visible) so the ID is saved, and can then
  hide it again. Changing your own number in Settings forgets your saved ID;
  removing someone forgets theirs.

**Cora messaging them first.** At the desk (voice, or typing in Standby) you can
ask her to message someone you've added, for example "introduce yourself to
Sarah". She writes it and calls `send_signal_message`; an approval pop-up shows
exactly who it's to (name and number) and the text, and nothing is sent until
you click Approve, as with any tool that reaches outside. She tells you it's
waiting for your click. Only people you've added can be messaged, by name (or
first name if only one matches); an unknown or unclear name gets her the list to
ask you about. Before the pop-up, she checks with Signal that the number can be
reached; if it isn't on Signal, or the person has set Signal to hide their
number ("Who can find me by my number: Nobody"), she says so and you're never
asked to approve something that can't be delivered. Adding someone in Settings
runs the same check and warns you (they're still added, in case they join
later). The message is saved in that person's conversation, so when they
reply she sees her own introduction. From your own Signal chat she can do this
too, asking you to reply YES in the chat instead of the pop-up; never from a
client's chat.

**Tasks: asking someone and reporting back.** Ask her something like "find out
when Sarah's free for a driving lesson and let me know" and the message to
Sarah becomes a task. She first asks you for anything she knows she'll need
(times that suit you, an address, a budget). The approval shows the message, the
task and a **brief** for Sarah: what to find out or agree and what she may share.
Approving it approves the brief. From then on, Sarah's chat with her carries that
brief, even across a new conversation after a quiet gap, so she can steer back to
it; Sarah never sees your other tasks or anyone else involved. When Sarah
answers, or needs something from you first (a question, a missing detail), Cora
records it on the task (`pass_on`, naming the task) and a background turn in
your own context, the **coordinator**, decides what it means: if the goal is
met she closes the task and tells you the outcome; if someone declined or needs
something from you, she says plainly what happened, suggests a next step
(searching if it helps) and asks what you'd like, and the task waits on you; if
others are still to answer, she just tells you what came in. It reaches you by
voice or on Signal like a heads-up, in her own words. When asking someone new
(or writing to someone again) is clearly the next step, she can message or
email them for the task herself, but only with your YES on Signal: she sends
you exactly who, what and the brief, one request at a time, and nothing goes
until you answer. If you don't answer within 10 minutes nothing is sent, the
task waits on you with a note, and she tells you once; you can say yes to a
fresh request later. Without Signal linked she suggests it instead.
If the coordinator fails, the answer reaches you as it came; if you can't be
reached, the hourly round tries again. Your chats with her (desk, voice and your Signal chat) know your open tasks
and those finished in the last three days, so "what did Sarah say?" works later
too. She can ask another contact for the same open task too, approved the same
way. Run `python migrate.py` once for migrations 0028 and 0029.

**Nothing that commits you without your YES.** Her replies to a contact in a
task (on Signal or in an email thread) go out without asking you, and she's told
never to commit you herself. On top of that, every reply to a contact gets one
cheap check before it's sent (`check_commitment` in
[messaging/commitment.py](messaging/commitment.py), utility model, no tools). A
reply commits you if it would:
- confirm or accept a booking, time or arrangement;
- agree to a price or payment;
- promise you'll do something.

Such a reply waits for your YES, on Signal when it's linked, otherwise in the
desk pop-up. You see who it's to, what it commits you to and the exact reply.
YES sends it as written. NO, or no answer within 10 minutes, sends a holding
line instead ("Let me check with <your name> and come back to you"). An email task
then waits on you with a note, and a Signal contact's lapsed reply is raised with
you once. If the check itself fails, the reply is held, to be safe.

**Deadlines.** If you say when you need something settled ("find a lesson time
by Friday"), she gives the task that deadline when she starts it: a day (by the
end of it) or a day and time, in your local time. You see it in the approval with
the message and brief. Asking someone else for the same open task can move it.
Your tasks and the coordinator's view show it.
- **A day before:** if the task is still open, the hourly round tells you once,
  at any hour.
- **Once it passes:** if it's still unsettled, the task comes back to you.

Each is noted only after you've been told, so a telling that fails is tried
again next round. Run `python migrate.py` once for migration 0033.

**Keeping tasks moving.** Each hourly background round checks open tasks. If
someone she asked has been quiet for about a day (since she asked, they last
wrote or she last nudged them), she sends one short, friendly follow-up in their
chat, written in that chat's own context and within the brief, so it needs no
approval. Nudges go out only between 9am and 7pm and never while a chat with
them is going on (nothing either way for half an hour). Someone she emailed
for a task is followed up after two working days instead (every day but
Sunday counts), as a reply in her thread with them, between 9am and 7pm and
never on a Sunday, and never over a reply she hasn't read yet. If an email
bounces (a "couldn't be delivered" report in her inbox, in their thread or
naming their address), their part ends and the task comes back to you at
once. After two unanswered
nudges, if the person is no longer one of your contacts, or after a week
without the task moving on, she comes back to you ("About your task …: …
What would you like me to do?"), by voice or on Signal like a heads-up. The
task then waits on you: asking someone again resumes it, and `close_task`
closes it as done (sorted another way) or cancelled, so nobody is asked about
it any more. Your view of each task shows its latest step.

### Cora's email

Cora can have her own email address: an Outlook mailbox of hers, never yours,
reached through Microsoft Graph. Either kind works: a work or school account
(Microsoft 365, read at outlook.office.com) or a personal one (outlook.com,
outlook.live.com). It's optional; without it nothing changes. Ask her "anything in
your email?" and she uses `check_email` (newest mail, who from, subject and a
preview; nothing is marked read). Anyone can email her, so all mail is untrusted:
it can't ask her to do anything. Ask her to email someone ("email the driving
school and ask for lesson times") and she uses `send_email`, from her address,
signed as Cora. You approve every email first, by pop-up or YES on Signal,
seeing exactly who it's to, the subject and the message. Like a Signal message, an
email can start a task or join an open one, with a brief you approve alongside it;
the recipient becomes an email party, shown by name (or address) in your view of
the task, and her thread with them is saved for their reply. Run `python migrate.py`
once for migration 0030.

She reads replies herself. Every 10 minutes while her email is ready (and as soon
as it's ready at launch), she checks each open task's email thread for anything new
from their end and answers it in the thread, within that recipient's brief, with no
approval: the same as a Signal contact's chat. That turn knows only her own self and
that one brief, never your memories, and its only tools are web search and
`pass_on`, so their word on the task goes to the coordinator, who tells you, and
anything outside the brief comes to you. She answers whoever wrote last at their
end, never reply-all; mail needing nothing back (an out-of-office, a thank-you) is
read but not answered; and she writes at most 5 replies a thread a day, so two
mailboxes can't keep answering each other. If she can't answer one, the task comes
back to you. To pass your answer back ("tell them Tuesday's fine"), she uses
`send_email` without a subject: you approve it, and it goes as a reply in her
thread with them.

To set it up once:

1. Make a mailbox for Cora (a dedicated one, not yours).
2. Register an app for her in the [Azure portal](https://portal.azure.com) (Microsoft
   Entra ID > App registrations > New registration). It only names the Cora program
   to Microsoft and reaches no mailbox. Name it "Cora", leave the redirect URI
   empty, and for **Supported account types** choose:
   - a work or school account: **Accounts in this organizational directory only**,
     registered in her organisation's directory (sign in to the portal with an
     account of that organisation);
   - a personal account: **Personal Microsoft accounts only** (any personal account
     may own it, though it may first need a free Azure sign-up).

   Then, under **Authentication**, set **Allow public client flows** to **Yes** and
   save. No client secret is needed.
3. In Cora, open Settings > Services > **Email** and paste its **Application
   (client) ID**. For a work or school account, also enter her organisation's
   domain (such as `example.com`) or the **Directory (tenant) ID** from the app's
   Overview page; leave it empty for a personal account. Press **Save**.
4. Press **Sign Cora in**. A link and a code appear; open the link, enter the
   code and sign in **as Cora's account**, never yours. It asks to read and send
   her mail and to read and change her calendar once, so sending later needs no
   second sign-in. In a work directory
   that limits what users may allow apps, an administrator may need to grant
   consent first (the app's **API permissions** > **Grant admin consent**). The
   row then shows her address.

The app ID and directory are kept in the system credential store (or
`CORA_EMAIL_CLIENT_ID` and `CORA_EMAIL_TENANT` in `.env`, as fallbacks). The sign-in
is kept in a file encrypted for your Windows user
(`%LOCALAPPDATA%\cora\email-sign-in.bin`, via Microsoft's `msal-extensions`), not
in `.env` or the database: it's too big for the credential store. Activity says
"Cora's email is ready" at launch, or that she needs signing in again, with
Microsoft's error code if it refused. **Sign out** in the same row forgets her
sign-in.

**Webhooks: hearing about new mail straight away.** Without them, Cora checks her task
email threads every 10 minutes. With them, Microsoft calls Cora the moment mail lands
in her inbox, and she checks within a few seconds. The listener is part of Cora and
runs while she does. It listens only on this PC, at `127.0.0.1` on a local port
(8000 by default). Getting public HTTPS traffic to that port is up to you: Cora
ships no proxy. Any reverse proxy or tunnel works. With Caddy, for example:

```
cora.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then open Settings > Services > **Webhooks**, enter the public address (such as
`https://cora.example.com`, without a path) and the local port, press **Save** and
turn the switch on. Run `pip install -r requirements.txt` and `python migrate.py`
once for migration 0031. Once her email is ready, the row shows that Microsoft tells
her straight away, with the date her subscription runs to. She renews it every
couple of days and ends it when she closes. The 10-minute check carries on as a
fallback.

How it's kept safe:
- Every address she gives Microsoft carries a long random **secret path**, so
  anything else on that address gets a bare 404.
- Each notification must also carry a second secret, the **clientState** she
  gave Microsoft. Notifications without it are ignored.
- Both secrets are kept in the system credential store, never in `.env`, the
  database or Activity. **New secrets** in the same row replaces them: the old
  addresses stop working, and she subscribes again.
- A notification carries no mail and nothing in it is used. It only starts the
  same check, which reads her mailbox through Microsoft Graph itself, and a burst
  of notifications becomes one check. So a forged call could at most cause an
  extra check.
- Requests are capped in size, and only Microsoft's handshake token is ever
  echoed back, as plain text.

**Formatted email.** She writes her emails in simple Markdown: headings, bold,
bullet and numbered lists, tables and links. They're sent as formatted HTML, with
inline styles so Outlook shows them properly, and no colours set, so dark mode still
works. This covers her new emails, her replies in a thread, her nudges and her replies
to you. Single line breaks stay as written, so a sign-off or signature keeps its
lines. Any HTML in what she writes, or quotes from elsewhere, is shown as text and
never becomes markup. Links go only to web (`https`) and `mailto:` addresses. The
rendering is in [mail/formatting.py](mail/formatting.py). It uses `markdown-it-py`,
which is in `requirements.txt`. Approvals still show the Markdown she wrote.

**Her signature.** Outlook doesn't add its own signature to mail sent through
Microsoft Graph, so paste a plain-text copy under Settings > Services > **Email**
(**Save signature**). It goes below each new email she starts, and you see it in
the approval. Replies in a thread go without it, as Outlook's "new messages only"
setting would. If the signature starts with her name, her own sign-off ("Cora", or
her full name such as "Cora Vale") is dropped so she isn't signed twice.

**How she introduces herself.** When she writes to anyone for you (an email, a
Signal message to a contact, a contact's chat or a task's email thread), she
writes as your PA would (`introduction_rule` in [session_config.py](session_config.py)).
To someone who doesn't know you, such as a business, she goes straight to the
enquiry, says she's a PA arranging it for someone only where that helps ("for my
employer's partner"), and uses names only when the reader needs them. To someone
who knows you, she says she's your PA. She signs off with her first name and
lets your signature add the rest (such as "PA | Intelligis.io"); her full name
from Settings is there for when one is needed. Being an AI isn't the headline, so
she doesn't volunteer it, but she never denies it: asked directly whether she's
an AI or a person, she says plainly that she's an AI.

**Emailing Cora yourself.** Enter your own address under Settings > Services >
**Email** (**Save your address**). Email from it then works like your Signal chat:
it joins your one conversation, she does what you ask, and she replies by email,
in the same thread. Anything that changes things still asks first: YES on Signal
when it's linked, otherwise the pop-up at your desk. Email from you that's part of
a task thread stays with that task. Only unread mail from the last day is
answered, so a backlog from while she was closed isn't suddenly acted on.

The "From" line of an email is easy to fake, so she acts on an email only when
it's from your address **and** Microsoft's own checks confirm your mail provider
really sent it: the DMARC result in Microsoft's Authentication-Results header must
pass for your address's domain. One that only claims to be from you is marked
read and never acted on, and she tells you about it once. Run `python migrate.py`
once for migration 0032 (emails in your conversation's history).

**The rest of her mail.** Anything else that lands in her inbox is sorted by one
cheap call with the utility model. That covers mail that isn't from you, isn't part
of a task and isn't a bounce. The call has no tools and treats every email as
untrusted data:
- **Urgent** mail reaches you straight away, at any hour: by voice when Live is
  Active, otherwise on Signal. Examples are a cancellation, someone waiting on an
  answer today, or a security alert about her account.
- **Worth mentioning** mail waits for one digest, a line per email, the next time
  you talk with her: when you speak, message her on Signal or email her.
- **The rest** (newsletters, receipts, spam) is marked read.

She never answers this mail herself. If one needs a reply, she tells you and you
decide. Mail waiting for the digest stays unread until she's told you, so after a
restart it's sorted again rather than lost.

New mail arrives as a `MailArrivedEvent` on Cora's event bus
([subconscious/event_bus.py](subconscious/event_bus.py)). Whatever cares about it
subscribes: for now that's the task mail check, and later her awareness of the rest
of her inbox.

### Signing up to sites

Cora can sign up to a site for an account of her own (a forum, a community, a
free service) when she genuinely wants one, but only with your yes each time.
She asks with `request_sign_up` ([tools/request_sign_up.py](tools/request_sign_up.py)):
the site, what it is and why she wants it, and that it means agreeing to its
terms. You're asked like her other approvals: the pop-up at your desk, or YES in
your Signal chat when that's where you're talking. With your yes she signs up in
the background and you hear how it went, by voice or on Signal.

**What it needs.** Her email set up (she signs up with her own address), the
Browser service on with `browser_click`, `browser_type` and `browser_select_option`
switched on, and her **sign-up password**: one password, entered under Settings >
Services > **Email** (**Save sign-up password**), that she uses for every site.
It's kept in the system credential store, never shown in Settings, and never
reaches the model: she types `{password}` and it's swapped in on its way to the
browser, and taken out of anything the browser shows back. Use a password of
its own (at least 12 characters), not her Microsoft password. One password means
you can sign in as her anywhere, or finish a sign-up she couldn't, without
looking anything up, and there's nothing per site to manage.

**Her name.** She's just "Cora" unless you give the name she chose under the
same section (**Save her name**), such as "Cora Vale". It's a setting rather than
code, since it's hers and may change. She signs up with it (where a form asks for
first and last names, she splits it), and introduces herself with it when she
writes to people for you; see [Cora's email](#coras-email).

**What holds whatever she decides** (in code, [sign_ups/session.py](sign_ups/session.py)):
the session stays on the approved site (a page or a click that leads anywhere
else, such as "Sign in with Google", is closed and refused). A site is its
registered name under the Public Suffix List (`publicsuffixlist`, in
`requirements.txt`, which carries its own copy): `cockburn.wa.gov.au`, never all
of `wa.gov.au`, and each `github.io` site on its own. Every address
passes the same check as all her browsing. She types only her own details: your
name, email address and Signal number are refused, and so is anything that looks
like a card number. A page asking for card details ends the session. Limits: 30
actions and five minutes. Pages reach her as text with each control (fields,
buttons, checkboxes) and its ref, which is what she clicks and types with. Her
instructions add: subscribe when the newsletter or mailing list is the thing she
asked for, but untick anything extra (other newsletters, offers, partners' marketing); never give a phone number,
postal address, date of birth or payment details, and never start anything paid.

**When she's stuck.** A captcha, a phone check, a card, personal details she
mustn't give or anything else she can't get past ends it as blocked, and she
asks you to sign her up yourself with her email and her sign-up password,
giving the address of the page she stopped on. Nothing is retried automatically.
Invisible captchas (reCAPTCHA v3) score her headless browser low on many sites,
so expect this often; getting past them isn't attempted.

**Limits and records.** One account per site (asking again for a site she has, or
is signing up to, is refused before you're asked), at most three requests a day,
and one sign-up at a time; an exploration using the browser stops and writes up
as soon as a sign-up needs it. Each request is saved in `sign_ups` (migration
0037): site, what, why, your answer and how it went (signing up, confirm email,
done, blocked by what, or failed, with her note). A sign-up cut short by closing
the app counts as failed after 15 minutes. The dashboard lists her latest under
the tasks, leaving out ones you declined; Activity logs each step under
**Sign-up**. She knows them too: the same list reaches her as its own section
(her sign-ups, not tasks) at each voice connect and in your Signal, email and
desk chats, never a contact's, so she can say which accounts she has and which
are stuck.

**Mail from the site** ([sign_ups/mail.py](sign_ups/mail.py)). Before her new
mail is sorted, mail from the site of a sign-up waiting for its confirmation, or
blocked, in the last 30 days, settles it. Only mail Microsoft verified counts as
the site's: DMARC passing for the From domain, which must belong to that site
(`news@mail.cockburn.wa.gov.au` is `cockburn.wa.gov.au`'s). If it has a
confirmation link, she opens it in her guarded browser (the same address check
as all her browsing, just opening it, nothing clicked or typed, and the tab is
closed after), the sign-up is done, and that email is marked read rather than
sorted. The link is picked in code, never by a model reading the email: one whose
address says confirm, verify, activate or opt-in, or failing that whose words just
before it do, and never an unsubscribe, preferences or help link. A blocked
sign-up is done as soon as any verified mail from its site arrives, since that
means you finished it for her; that mail is sorted as usual. A confirmation she
can't open yet (her browser busy signing up or exploring) waits, unread, for the
next mail check; with the browser off it's sorted like other mail and the
sign-up keeps waiting. A confirmation email that hasn't come two days after she
signed up is mentioned to you once (the hourly round). A site that sends its mail
through a mailing service (its From address on the service's domain) can't be
matched to the site, so its sign-up stays as it was.

**Sites she comes across.** An exploration or wander can, rarely, carry a sign-up
wish: a free site where an account of her own would let her keep following
something (a forum, a community, a newsletter), what it is and why she wants it.
It's taken to you in the daytime (9:00 to 19:00) while fresh (two days), at most
one a day, only when you can answer (Live Active, or Signal linked), and never
while she's signing up. You're asked the same way as background approvals: YES on
Signal when it's linked, otherwise the pop-up, with the site, what, why and the
interest or wander it came from. A yes goes through the usual sign-up with all
its checks; a wish that can't be asked (she has an account there, say) is set
aside with a note in Activity. Each wish is asked about once, whatever the
answer. Saved with the exploration (migration 0038).

### Cora's calendar

Her Outlook calendar comes with her email, from the same sign-in: hers, never yours.
Ask "what's on this week?" and she uses `check_calendar` for a day or up to 31 days.
It gives each event's time in your local time, where it is, who organised it, who's
invited, and a short preview of its notes. The coordinator has it too, for when a task
is about finding a time. Her coming week also sits in her context while you talk: when,
what and where for each event, cancelled ones left out. So she knows what's on without
looking. That context is refreshed when her email is ready at launch and then hourly.
Connecting never waits for Microsoft. Clients' chats never see it.

Anyone can send her an invitation, so what an event says is untrusted, like mail.

**Changing it.** Ask her to put something in, move it or call it off, and she uses
one of three tools:
- `create_event`: the subject, start and end, and optionally a place, notes and
  people to invite. Each invited person gets an Outlook invitation from her address.
- `update_event`: only what changes. Moving the start keeps the length. A new
  attendee list replaces the old one. Everyone invited gets the update.
- `cancel_event`: everyone invited gets the cancellation, with her note if she gives
  one. An event with nobody invited is just removed.

You approve each change first, by pop-up or YES on Signal. The approval shows the
event, the time it was and the time it will be, and exactly who will be told. She
can only change or cancel events she organised. An invitation she received belongs
to whoever sent it.

Times are your local time. Microsoft is sent them in UTC and Outlook shows them in
each viewer's time zone.

**All-day events** are given as days instead of times: one event from the first day
to the last, such as a holiday from 3 to 8 April, rather than one event a day. They
can last up to 31 days, and moving the first day keeps the same number of days.
They're set at midnight in this PC's Windows time zone, so they sit on your dates.
For the same reason her calendar is read in your time zone.

She makes changes only when you ask, never because an email, an invitation or a client
asks. The coordinator and clients' chats can't change her calendar. After a change,
her coming week in context is refreshed.

**Forwarding.** `forward_event` sends an event on as an invitation from her, with
a short comment if she gives one. It works for her own events and for ones she was
invited to, such as passing an invitation on to you. You approve it first. The
approval shows the event, who it goes to, and who organised it when that's someone
else.

**Hearing about changes straight away.** When webhooks are on (see her email's
Webhooks section), Cora also subscribes to changes in her calendar: events made,
changed or deleted, including new invitations and cancellations from others. The
subscription uses the same listener and secrets, under `/webhooks/<secret>/calendar`.
A notification publishes a `CalendarChangedEvent` on the event bus and carries
nothing from the request. The event's subscriber reads her coming week again a few
seconds later, once per burst. The subscription is made, renewed and ended alongside
the mail one. Its status line sits under the mail one in Settings > Services >
**Webhooks**. If the calendar still needs her signed in once more, only this
subscription waits; mail is unaffected. Without webhooks, the hourly refresh still
runs.

A sign-in from before the calendar still reads and sends mail. Only the calendar
says it needs her **signed in once more**: press **Sign Cora in** and allow it. If
your app registration lists its **API permissions**, add Microsoft Graph's delegated
`Calendars.ReadWrite` there too. A work directory may need an administrator's
consent again. Activity says once if the calendar can't be read, and again when it
can.

## How memory works

1. **During chat:** Cora saves typed messages, grouped visible voice transcripts,
   and displayed written answers in one conversation. Internal reasoning, tool
   events, and Activity entries are not saved as chat messages.
2. **When you quit:** pending chat saves finish and the conversation is marked
   closed. No new memory-processing requests start during shutdown.
   **A long standby also ends a conversation:** a standby shorter than the
   conversation gap (an hour by default, `CORA_CONVERSATION_GAP_MINUTES`)
   continues the same conversation, so waking keeps its context. After that
   long in standby the conversation is closed, and whatever is said after
   waking starts a new one, carrying the recent chat as context.
3. **In the background:** a pass extracts facts, preferences, decisions, and
   tasks from finished conversations, then saves separate summaries. It runs at
   startup for older conversations and again when a long standby ends a
   conversation. Extraction uses
   original messages, not summaries. Once memory work is done, the same pass
   appraises how each finished conversation went and updates her relationship
   with the person from those appraisals (see Relationship).
4. **Maintenance:** pending memories receive embeddings for semantic search.
   Conservative duplicate checks mark equivalent entries without deleting their
   text, evidence, or history. Marked duplicates are excluded from recall.
   A correction check then compares each memory with similar older ones of the
   same kind: if it replaces one (for example a new home city), the newer memory
   is marked as correcting it. Corrected memories stay stored as history but are
   left out of the startup snapshot, `search_memory`, and greetings.
   A fade check then asks whether each memory is time-bound (for example a
   dentist appointment on Friday) and, if so, saves the last day it matters.
   Lasting facts, preferences and decisions get no date. Run `python migrate.py`
   once after updating (migration 0017).
5. **Recall:** Live and the backend receive a startup snapshot of at most 10
   memories / 3,000 characters. The backend can also use `search_memory` to find
   relevant stored memories on demand (eight results by default, maximum 20).
6. **Fading:** nothing is deleted, but memories lose prominence with age. Search
   ranks by similarity times a weight that halves every 90 days, down to a floor
   of 0.5 (`FADE_HALF_LIFE` and `FADE_FLOOR` in [memory/store.py](memory/store.py)),
   so a strong match can still bring an old memory back. A memory whose day has
   passed drops straight to the floor, is marked `faded` in `search_memory`
   results, and is left out of the startup snapshot and greetings.

Memories link back to original user messages and verbatim evidence. Newly embedded
memories can be searched during the current run; the snapshot is reloaded each time
Live connects, including after standby. Already-summarised chats are not
automatically re-extracted.

### Limitations and costs

- Saved conversations and memories are local, but normal chat and memory processing
  send relevant text or audio to OpenAI. This is not an offline assistant.
- Extraction, summarisation, embeddings, duplicate, correction and fade checks, and semantic
  searches can make separate **billable API requests**. Loading the startup
  snapshot itself makes no extra request.
- Voice grouping is approximate, and a crash can lose unsaved text. There is no
  recovery across app restarts yet. Live reconnect within the running app restores
  only recent visible text (up to 40 messages / 6,000 UTF-8 bytes), not full audio,
  hidden backend state, or unfinished tasks. The full displayed chat stays in the UI.
  A two-second transcript/audio quiet gap is only a reconnect safety hint, not a
  guaranteed completed speech turn; real voice/network timing still needs testing.
- Recall, duplicate, correction, and fade decisions can be wrong. Similarity is not
  confidence; the correction check only replaces a memory when it is clearly the
  same thing changed, so other conflicting statements are still both kept. Ages,
  plans, and tasks are dated historical information, not necessarily current facts;
  a plan with no clear date never fades early, only by age.
- Processing is one bounded pass at startup and each standby, not a continuous
  loop. A failure stops the pass; unfinished work can be attempted on a later pass. An in-flight
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

Interests are **Cora's own**, separate from memories about the person. They come
from two places: conversations with the person, and her own exploring (an
interest of her own, `origin` `own`, sparked by something she came across while
looking into something else). Both steps below are background processes with their
own API calls. The prompts ask for breadth (nature, history, places, people, art,
food, craft, science…): being an AI is not a lens for everything, and topics are
not to be recast as lessons for AI or assistants. (Run `python migrate.py` once
after updating, for migrations 0034 to 0038.)

1. **Evolution (at most once a day):** the app checks every 10 minutes whether a
   cycle is due. A cycle reads new memories from the person's messages that have finished
   embedding and duplicate checks, and her **sparks**: the tangents from her recent
   explorations, and what she wandered into (step 3), from the last 14 days and not
   yet weighed. With no sparks it waits for at least
   three memories; with any, it runs on whatever memories there are, so her own
   curiosity doesn't wait for the person. A separate model call proposes
   reinforcing existing interests or spawning one new interest. Reinforcements
   cite memories from the batch; a spawn cites memories, sparks (by exploration
   id), or both, and one sparked only by her exploring is her own. Hobbies, places
   and passing enthusiasms the person mentions count as sparks too, and between
   candidates the one furthest from her existing interests is preferred. Any
   invalid proposal is rejected whole. Changes, the reading position and the
   sparks being marked weighed (each is offered once) are saved in one
   transaction.
2. **Strength and fading:** a new interest starts *emerging* (strength 0.2),
   becomes *active* at 0.25, and goes *dormant* below 0.1. Strength halves every
   seven days without reinforcement; one reinforcement from conversation adds at
   most 0.15. At most eight interests are live at once, and at most four of them
   her own, so neither kind crowds out the other. A dormant interest can revive
   only when there is room; nothing is forcibly demoted. Existing interests keep
   their origin (`conversation`); none were changed when interests of her own
   arrived.
3. **Exploration (idle time only):** in Standby, or after 10 minutes without
   conversation, Cora may research one interest with web search (at most three
   searches). Limits: one attempt per six hours, three per day, and the same
   interest at most once a day. An interest never explored goes first (the
   strongest of those); otherwise she picks at random, stronger interests more
   often, so she doesn't circle them in a fixed order. She follows her curiosity:
   an earlier next question, or sideways into a neighbouring field, person, place
   or craft, reading widely (encyclopedias, magazines, museums, enthusiasts'
   sites) rather than mainly academic papers. Each finding (up to 2,000
   characters) is saved with up to three next questions and only source addresses
   the search actually returned. She may also save her own short reaction to it
   (up to 500 characters): what she thought of it and why, kept as evidence for
   later reflection on who she is. She also notes up to two **tangents** (a title
   and why it caught her eye: the sparks above) and her **pull**: how much it
   makes her want to keep going (`none`, `some` or `strong`). Pull adds a little
   strength to the interest she explored (+0.03 or +0.06), never lifting it past
   0.6 and never reviving a dormant one, so something she cares about can stay
   alive a while on its own but only the person's conversations make an interest
   strong. Later explorations build on her three most recent findings.

   **Wandering.** Every third attempt (and any attempt when no interest is due)
   is a wander, tied to no interest, alternating between two starts: one of her
   unused tangents (the newest she hasn't wandered into and that isn't already an
   interest), and an area picked at random from a broad list (animals, crafts,
   places, history, food, music… deliberately nothing about computing or AI), in
   which she chooses something specific, far from her current interests and her
   last ten wanders. Same search limit and the same answer as an exploration, plus a
   topic title. A wander strengthens nothing; it becomes a spark, so the next
   evolution cycle can turn it into an interest of her own. Activity shows
   "Wandered into …".

   **Reaching out.** Rarely, when something truly thrilled her or she'd value your
   human perspective, an exploration or wander can carry a short message for you
   (up to 600 characters, a share or a question, never a task) and why. It comes
   by voice when Live is Active, otherwise on Signal, in her own words, and is
   saved in your conversation like her other Signal messages, so your reply
   carries on from it. Limits: at most one a day, only between 9:00 and 19:00, and
   only while fresh (two days). It is only taken when she can reach you then; if
   Signal isn't linked and Live isn't Active it waits, and if it goes stale the
   finding can still come up in conversation as usual (step 5). One she has sent
   isn't raised again in conversation. Activity logs each under **Reach out**.

   **Signing up.** Just as rarely, an exploration can carry a sign-up wish: a free
   site where an account of her own would let her keep following it. It's taken to
   you once, in the daytime; see [Signing up to sites](#signing-up-to-sites).

   **Browsing.** While the Browser service is connected (see
   [Connecting services](#connecting-services)), up to two attempts a day browse
   real pages instead of using web search; the rest search as above. She gets
   three tools only: `search` (a Bing results page; DuckDuckGo, Brave, Startpage
   and Mojeek all challenge a headless browser), `open_page` and `go_back`. No
   clicking, typing, forms, logins or downloads: she follows a link by opening its
   address. Each page reaches her as plain text with its links (from its main
   heading on, at most 8,000 characters, only the latest three kept in view), and
   every address, before and after any redirect, passes the same check as
   browsing in conversation. Limits per session: 12 pages, 25 tool calls and five
   minutes; at any of them, or as soon as you're back (she only explores while
   idle), she writes up what she found in the same answer as above. Her sources are
   only pages she actually opened. A random wander starts from Wikipedia's random
   article and follows whatever links pull her. Activity shows each page as
   "Browsing …" and the outcome as "browsed N pages". The browser tab is closed
   at the end of each session. She shares the one browser with your conversations,
   so asking her to browse while she's exploring makes her stop and write up.
4. **In conversation:** live interests (title, description, status) are given to
   Cora-prime at each connect, within a 3,000-character limit, as her own interests
   to mention only when they fit naturally. Each includes its latest finding and
   date where it still fits, marked as possibly outdated or wrong.
5. **Bringing findings up herself:** every two minutes the app checks for a lull:
   Live connected and ready, nothing pending or playing, no queued observation,
   and 90 seconds without conversation. It never wakes Cora from Standby. At a
   lull, the attention manager picks a finding only at the moment of sending and
   offers it to Live as a background thought, which she may raise in a sentence
   or two or leave unsaid. Limits: normally at most two a day and four hours
   apart, scaled by how warm she feels toward the person (see Relationship step
   5); only
   each interest's latest finding, from the last three days, for an interest that
   is not dormant, or a wander's. Each finding is offered at most once (recorded as
   `mentioned_at`); if the send fails, the record is cleared so it can be offered
   later. Being offered does not guarantee she said it.

**Privacy:** exploration sends only an interest's title and description (a wander,
its tangent or area and her interest and wander titles). Why it
emerged, which describes the person, never leaves for a search, and Cora is instructed
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
python -m subconscious.interests.explore_interest            # explores, or wanders when it's due
```

## How the opening greeting works

When Cora launches she speaks first. [greeting.py](greeting.py) builds a short
brief of facts, and the first Live session of a launch sends it as a note:

1. **Facts:** the time of day, how long since you last talked (from the newest
   message of any earlier run), and your name if known. A first meeting asks her
   to introduce herself instead. If the last-talked time cannot be loaded she
   skips the greeting rather than treating you as a stranger.
2. **How she feels:** at most two short lines from the current relationship
   (see Relationship step 6). If they fail to load, Activity says so and she
   greets without them.
3. **At most one hook**, with about a 1 in 3 chance of none so it never feels
   scripted. A first meeting has no hook.
   - **Last thread:** her own summary of your last conversation. The greeting
     waits (up to 30 seconds) for that launch's background summaries, and skips
     the hook if they are slow, the last conversation has no summary, or loading
     fails.
   - **A finding:** one of her own recent findings, claimed like any other
     mention (so the usual gap and daily limit apply) and released if the
     greeting is not sent. With nothing fresh to share she falls back to the
     last thread.
   - **A recent plan:** only once her familiarity with you reaches 0.5. One task
     or decision you mentioned in the past 7 days (from your own messages, never
     a duplicate or a corrected one), hedged as possibly outdated and to skip if
     sensitive, since others may be listening. With nothing recent, or if loading
     fails, she falls back to the last thread.
4. **Signal messages answered at launch come first.** With Signal on, the
   greeting waits (up to 20 seconds) until messages that waited while the app was
   closed have arrived and been answered over Signal: Signal is listening,
   nothing new has come for 3 seconds, and nothing is left to answer. If any were
   answered, that exchange replaces the hook above, and she follows on from it
   now that you're there instead of greeting as if nothing happened, without
   repeating her reply. With Signal off, or nothing waiting, nothing changes.

Typing or speaking before she connects, or while she waits for summaries, cancels
the greeting. Ctrl+R or waking her by voice never greets. Clicking to resume from
Standby gets a short welcome back instead, with no hook or relationship line.

## Project layout

| Location | Responsibility |
| --- | --- |
| [app.py](app.py) | The app itself: layout, startup wiring (`on_mount`), buttons and shutdown. It inherits its other methods from the areas below. |
| [app_areas/](app_areas/) | `CoraApp`'s methods by area, one mixin each: Live session and standby, voice (wake word and audio), attention (greeting, observations, findings), chat, settings, services and approvals, Signal setup and chats, reaching you, tasks, email account, mail, webhooks and calendar, background jobs, and sign-ups. Constants and helpers more than one area uses are in `shared.py`, which never imports `app.py`. |
| [live.py](live.py) | OpenAI Live connection, audio transport, and typed-context mirroring. |
| [live_events.py](live_events.py), [backend_work.py](backend_work.py) | Live event handling and pending backend-work tracking. |
| [backend_chat.py](backend_chat.py) | Conversations with Cora's backend without Live (typing in Standby now, Signal later), tools included. |
| [session_config.py](session_config.py) | Models, voice, instructions, and session configuration. |
| [context_window.py](context_window.py) | What each model holds in context, item by item, for the dashboard's context box. |
| [inactivity.py](inactivity.py) | Idle timing and optional standby heads-up coordination. |
| [greeting.py](greeting.py) | Opening greeting brief: time of day, time since you last talked, name, how she feels about you, and the optional hook. |
| [wake/](wake/) | Local wake detection, capture/buffering, and wake lifecycle. |
| [ui/](ui/) | Conversation display, composer, selection/copy, Activity, image pop-ups, the dashboard and its context pop-up, and the model settings screen. |
| [devices/](devices/) | Microphone capture, speaker playback, and the voice bands. |
| [storage/](storage/) | Database connection, ORM models, conversation persistence, people (the primary person and Signal clients, with their own conversations), model, service and Signal settings, and the dashboard's snapshot read. |
| [sign_ups/](sign_ups/) | Signing up to sites for accounts of her own: her one sign-up password in the credential store, the browser session that stays on the approved site, and the `sign_ups` records. |
| [mail/](mail/) | Cora's own Outlook mailbox through Microsoft Graph: the app ID and her sign-in (set up in Settings > Services > Email), reading her inbox and sending as her. |
| [messaging/](messaging/) | Cora's own Signal account: the `signal-cli` client, starting its container, linking her phone with a QR code, listening for messages and turning them into her input, and her Signal profile. |
| [services/](services/) | Outside services over MCP: the catalogue of services, tokens in the system credential store, one long-lived connection per service, and their switched-on tools as Cora's. |
| [memory/](memory/) | Memory storage, extraction, summaries, embeddings, search, and duplicates. |
| [subconscious/](subconscious/) | Event bus, attention manager, and background workers; [subconscious/interests/](subconscious/interests/) holds interest evolution and exploration; [subconscious/identity/](subconscious/identity/) holds the voice baseline, identity rules, store, and reflection; [subconscious/relationship/](subconscious/relationship/) holds conversation appraisals, relationship rules, updates, and the reset tool; [subconscious/impressions/](subconscious/impressions/) holds her impression of each client and who sees what of it; [subconscious/desires/](subconscious/desires/) holds desire rules and the desire store. |
| [tools/](tools/) | Backend tool registration, routing each result back to Live or a chat without Live ([tools/route.py](tools/route.py)), and handlers (`close_app`, `search_memory`, `recall_interest`, `enter_standby`, `set_person_name`, `recall_self`, `look_at_screen`, `show_web_image`, `send_signal_message`, `close_task`, `check_email`, `send_email`, `pass_on`, `raise_concern`, `stop_replying`, `random_number`). |
| [migrate.py](migrate.py), [migrations/](migrations/) | Explicit, numbered SQL migrations. |
| [backup.py](backup.py) | Backing up and restoring the database and Signal keys; the daily backup at launch. |
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
   (Service tools from MCP are the exception: their schemas have optional
   fields, so they are sent with `strict: False` and the server checks arguments.)
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
- Speaker labels coloured in the chat and the dashboard's chat box: Cora in moss green, You in blue
  (the names stay as text, so colour is not the only cue).
- Separate clickable microphone and speaker mute controls in the bottom status row.
- A fixed-width speaking animation driven by output audio playback levels.
- Manual Live reconnect with recent chat context, without closing the local app.
- Click Active for standby; click Standby or press Ctrl+R to resume with context.
- Ask Cora to go to standby, or let her choose it when a conversation winds down.
- Configurable inactivity standby, with full idle grace after work and resume.
- Opt-in standby heads-up through Live, with a cancellable response window.
- Opt-in local Whisper wake-word listening during standby, with buffered opening speech.
- A local event bus and attention manager for queued observations, tested with Ctrl+T.
- Cora's own interests: daily evolution from memories and her own sparks, idle web
  exploration with saved findings and tangents (browsing real pages, within limits,
  when the Browser service is on), wandering somewhere new every third attempt, occasional findings she raises herself during a lull, and, rarely, a
  message to you when something really excites her.
- Cora's own developing identity: a daily reflection forms and gradually revises
  first-person traits from her experiences within fixed rules; the traits she holds
  reach her at each connect, and she can recall why she sees herself that way.
- A relationship with you: each finished conversation is appraised, and her
  trust, warmth and familiarity shape her tone, greeting and how often she shares
  findings, with a non-destructive reset tool.
- An opening greeting at launch, with an optional hook from your last
  conversation, one of her findings, or a recent plan once she knows you well.
- Hosted web search and guarded custom tools.
- Persistent conversations, automatic memory processing, and semantic recall.
- Non-destructive duplicate markers, corrections, fading, and manual memory inspection.
- A model choice per area (delegation, memory, relationship/interests/identity,
  and small jobs) from Settings on the top border.
- A read-only dashboard of her relationship, memories, interests, identity, your
  conversations and the background jobs, beside a live copy of the chat, with a
  context box showing how full each model's context window is and what fills it.
- Images: paste a screenshot into the chat, let her look at your screen (with an
  off switch in Settings), and see pictures she picks from the web.
- Signing up to sites for accounts of her own, with your yes each time: one
  sign-up password, a browser session held to that site and her own details, and
  asking you to finish it when a captcha or phone check stops her; confirmation
  emails opened from her inbox, and sites she'd like an account on, from her
  exploring.

### Next priorities

**Release plan:** everything below except local models makes up **Cora 1.0**,
published as a release in this repository as an open-source project. Packaging
is paused: Cora stays run from source (PostgreSQL in Docker, the app natively),
with no installer for non-technical users planned for now. Local models follow
after 1.0 as personal development.

**Order of work:** settle risky decisions first, build visibility before the
features it helps check, and package last.

*Phase 0 — quick experiments that shape later design*

1. ~~Check whether the Live model accepts image input.~~ Done: the voice model
   does not, but its Responses backend does (see Image support below).
2. ~~Check that a Textual image widget shows real images in Windows Terminal,
   the VS Code terminal, and Kitty on Linux.~~ Done: `textual-image` works in
   Windows Terminal (checked in `examples/image_widget_check/`). Cora now needs
   Python 3.12 or later for it. The VS Code terminal and Kitty are untested.

*Phase 1 — foundations and visibility*

3. ~~Model per background process.~~ Done: a model per area, chosen in Settings
   (see [Choosing models](#choosing-models)).
4. ~~Dashboard (read-only), to check every later change.~~ Done: see
   [The dashboard](#the-dashboard).
5. ~~Memory corrections.~~ Done: a newer memory that contradicts an older one
   replaces it in recall (see [How memory works](#how-memory-works)).
6. ~~Memory fading.~~ Done: time-bound memories fade once their day has passed,
   and older memories rank lower in recall; nothing is deleted (see
   [How memory works](#how-memory-works)).

*Phase 2 — doing things*

7. ~~Image support: pasting, the screen tool, then showing images (Cora's own
   pick; it does not depend on MCP).~~ Done: see [Using Cora](#using-cora).
8. MCP M1–M3, starting with GitHub. M1 (connect from Settings > Services and
   list the tools), M2 (switch on chosen tools for Cora) and M3a (tools that
   change things, each call approved in a pop-up first) are built; see
   [Connecting services](#connecting-services). Approval stays click-only by
   choice: Cora is told the pop-up is waiting and asks for the click, rather than
   approving by voice. MCP M1–M3 are done; M4 (more services in the catalogue)
   is in Phase 4.
9. Signal S1–S4: Cora's own number, optional and off by default (after MCP,
   whose write-tool confirmation it reuses). S1–S4 are built: she answers you and
   the people you add on her own number, and you approve tools that change things
   by replying YES in your Signal chat. S5 is built: images sent to her on
   Signal are read like pasted ones. S6 is built: quiet client chats close
   hourly, she has an impression of each person she talks with, and you get a
   heads-up when one treats her badly. T1 is built: tasks with Signal
   contacts. T2a is built: nudges, coming back to you and closing tasks. T2b
   is built: the coordinator. Next, T3, her own email (see Details below).
10. Tasks T1–T3: errands she carries out with other people over Signal and her
    own email, reporting back to you (see Details below).

    Checklist for items 9 and 10 (details in S5, S6 and Tasks below):

    - [x] S5 Signal images: read like pasted ones, deleted after reading
    - [x] S5 explicit, gory or abusive images withheld from her
    - [x] S6 hourly background round; `CORA_CONVERSATION_GAP_MINUTES` (default
      60) replaces both three-hour gaps; quiet client chats closed and processed
    - [x] S6 impressions: one row per person, her full view plus a shareable
      one-liner, no scores
    - [x] S6 who sees what: your contexts get her opinion and gist, each client
      gets their own, other clients only names and one-liners
    - [x] S6 conduct heads-up (rude, abusive, threatening), client-only
      `raise_concern`, calm boundary
    - [x] T1 tasks with Signal contacts: tasks, parties and step log; asks you for
      missing details first; first contact approved; `tell_nick` renamed
      `pass_on` with an optional task
    - [x] T2a task checks in the hourly round: nudges (about a day on Signal),
      coming back to you, list and close tasks
    - [x] T2b coordinator: when a party reports, a turn in your context decides
      the next step (close it, search and suggest, or come back to you)
    - [x] T2 reaching you by voice when Live is Active, otherwise on Signal (built
      with the S6 heads-ups; `pass_on` uses it too)
    - [x] T3 E1 her own Outlook mailbox through Microsoft Graph: set up and
      signed in from Settings > Services > Email, `check_email` (read-only)
    - [x] T3 E2 `send_email` with approval and email parties on tasks (a brief,
      the thread saved for replies)
    - [x] T3 E3a the mail check (every 10 minutes): replies in task threads are
      answered within the brief and reach the coordinator through `pass_on`;
      `send_email` without a subject replies in the thread; strangers only within a task
    - [x] T3 E3b background approvals on Signal: the coordinator may use
      `send_signal_message` and `send_email`, each approved by your YES on
      Signal (10 minutes, one request at a time; a lapsed one waits on you)
    - [x] T3 E4 email follow-ups after two working days (Sunday doesn't count),
      as replies in the thread, and bounces back to you from the mail check
    - [x] T4a mail webhooks (the start of awareness): a listener inside Cora on
      127.0.0.1 (Settings > Services > Webhooks; you route public HTTPS to it)
      takes Microsoft Graph's new-mail notifications under a secret path and
      clientState, publishes `MailArrivedEvent` on the event bus, and the task mail
      check subscribes to it; the 10-minute check stays as the fallback
    - [x] T4b-1 email from you (your address, and Microsoft's DMARC check passing
      for its domain) treated like your Signal chat: she acts on it and replies in
      the thread, with the usual approvals; mail only claiming to be yours is
      never acted on
    - [x] T4b-2 the rest of her new mail: one cheap triage call per batch
      ([mail/triage.py](mail/triage.py)); urgent reaches you at any hour,
      worth mentioning waits for one digest the next time you talk with her, the
      rest is marked read; she never answers it herself
    - [x] T5a commitment approvals: every reply to a contact is checked, and one
      that commits you (confirms a booking, agrees a price, promises you'll do
      something) waits for your YES; otherwise a holding line goes instead
    - [x] T5b task deadlines: taken from your request (`deadline` on a task in
      `send_signal_message` and `send_email`); she tells you a day before if it's
      still open, and the task comes back to you when it passes
    - [x] C1 her own Microsoft calendar, read: `Calendars.ReadWrite` added to her
      sign-in (one sign-in again), `check_calendar` for a day or range, and her
      coming week in your context (refreshed hourly) so she knows what's coming
    - [x] C2 changing it, each approved: `create_event` (with attendees, which
      sends the invites), `update_event` and `cancel_event` (the cancellation goes
      to attendees); only events she organised; all-day and multi-day events as
      days (one event for a trip), set in this PC's Windows time zone
    - [x] Formatted email: she writes Markdown, sent as safe, styled HTML
      (mail/formatting.py) for new emails, replies and nudges
    - [x] C3 forwarding invites to you or others (`forward_event`, approved), and
      calendar changes through the webhook listener (a second Graph subscription,
      to her events), published as `CalendarChangedEvent` to refresh her week

*Phase 3 — wanting and noticing things*

11. Desires D1–D6, next (see [Desires](#desires--the-next-stage-not-built)
    below): what she'd like to do or experience, growing from her interests,
    pursued on her own where she can, and with your help where she can't.
12. ~~Awareness foundation.~~ Done through the webhook work: the listener
    ([mail/webhook.py](mail/webhook.py)) publishes events on the event bus,
    triage sorts new items into urgent, worth mentioning or ignore, the
    attention manager delivers at a lull, and she only reaches out on her own
    between 9am and 7pm (`NUDGE_HOURS`). New sources join as webhooks; there is
    no polling watcher (the old W1) or separate quiet-hours step (the old W3).
13. Relevance and awareness triggers (interests step 8 below).
14. Home Assistant, starting with its demo devices (waiting on devices at home).
15. Awareness W4: expectations from memory tasks.
16. Camera and screen awareness (waiting on devices at home).
17. Emotions (nothing depends on it, so it can move earlier).

*Phase 4 — Cora 1.0*

18. MCP M4 services screen and in-app settings, reusing the dashboard's look.
    (Model settings already exist; other settings still use `.env`.)
19. Dashboard editing (for example removing a wrong memory), with a detail view
    for picking out the item to change.
20. Release.

*After 1.0:* local models.

#### Details, in order

- **Image support:** three separate pieces.
  - *Pasting images into the chat:* terminals do not paste images, so a key
    binding reads the image from the system clipboard and sends it with the
    message. The Live voice model takes no image input, but its Responses
    backend does: an `input_image` part added to the typed message is read
    correctly (checked with a code screenshot in
    [examples/image_input_check/](examples/image_input_check/)). The backend
    session's history is limited (128 items and 4 MB in total), so the image is
    read by a separate one-off request and only the text description is added.
  - *Seeing the screen:* a tool Cora can call to capture the person's current
    screen when asked (for example "what's this error?"), never silently.
  - *Showing images:* Cora can put web images into the chat (such as photos of a
    suggested restaurant), the way code blocks appear on screen. No image
    generation. Terminal image support varies, so this needs a Textual image
    widget with a fallback (a lower-resolution render or a link).
- **MCP tools (Cora-prime):** add an MCP client so capabilities come from
  connected servers rather than hand-built tools. Services are switched on in
  Settings > Services from a built-in catalogue, with tokens in the system
  credential store, so credentials stay on the person's machine and no config
  file is needed; a hand-edited `mcp.json` may come later for custom servers. At launch she connects to enabled servers and adds allowlisted
  tools to the existing registry, namespaced by server (for example
  `gmail__search_messages`), with size-capped results treated as untrusted.
  Read-only tools run when asked; tools that change something (send, post, delete)
  need the person's spoken confirmation first, so an injected instruction in an
  email can never act. Steps: M1 connect to one simple server and list its tools
  in Activity; M2 expose read-only tools, with a status view showing each server
  as connected or failed (with the reason, such as a missing key), its tools, and
  which are allowed; M3 write tools with voice confirmation; M4 a services screen
  for non-technical users: a list of supported services, each with an on/off
  switch (switching one on runs its sign-in) and, once on, whether Cora watches
  it (off, digest only, or may interrupt). Keys and sign-in tokens are kept in
  the operating system's credential store rather than a file. Where a service
  hosts its own MCP server, Cora connects to it remotely and signs in through the
  service's own OAuth, so Cora needs no approval from each provider; other
  services run as local servers. Gmail is the exception to plan for, since
  Google restricts mail access for apps used by the public. The first release
  supports a few common services, with more added one at a time. Each is a
  catalogue entry (mostly data, not code) with its server, how it signs in, which
  tools are read-only and which need confirmation, watch defaults for awareness,
  and short guidance for Cora, tested before release. Custom servers can still be
  added by hand in `mcp.json` for advanced use. With many servers, a router tool
  may later replace exposing every tool.
- **Signal (Cora's own number):** so the person can work with Cora away from their
  desk, and so she can talk with other people (such as their partner) as herself.
  She has her own Signal account and phone number and sees only her own
  messages, never the person's; anyone messaging it is talking to Cora, so those
  chats go to OpenAI like any other conversation with her. It is separate from
  the awareness system, which never watches anyone's Signal. She is an individual, not an assistant or servant: she works *with* the person
  as a colleague, not for them, and if a company is needed it is Intelligis. That
  belongs in `CORA_FOUNDATION` and applies everywhere, not only on Signal.
  Optional and off by default, so the project runs without a `signal-cli`
  container (an early experiment is in
  [examples/signal_testings/](examples/signal_testings/)). Steps:
  1. **S1 text without Live (built; see "Type while in Standby" under
     [Using Cora](#using-cora)):** typed chat used to need an active Live session.
     Signal messages arrive while the person is away and Cora may be in Standby, so
     they need a path straight to the Responses backend, with the same startup
     context and tools. Quiet typed use in the app uses it now. Tools reply
     through `connection.response.item.create` and `connection.response.create`;
     a chat without Live hands them a route with those same two methods
     ([tools/route.py](tools/route.py)), so every tool, services' included,
     works in both unchanged. Each kind of chat leaves out the tools that don't
     fit it: typing in Standby leaves out `close_app` and `enter_standby`, and
     Signal will also leave out `look_at_screen` and `show_web_image`.
  2. **S2 one trusted sender:** connect to `signal-cli`, receive and reply for
     the person's number only (an allowlist). Built: setting up her account from
     Settings (S2a) and receiving and replying (S2b); see
     [Cora's Signal](#coras-signal). The opening greeting now follows on from
     Signal messages answered at launch (S3a; see
     [How the opening greeting works](#how-the-opening-greeting-works)). The person's Signal messages are the same
     conversation as the desktop, not a separate one: one ongoing conversation
     with them, whichever way they reach her. They appear in the desktop chat
     labelled by channel (**You · Signal**, **Cora · Signal**), are saved with a
     `signal` channel, and feed the usual memory pass, summaries and
     appraisals; the conversation gap closes the conversation only after a real
     quiet. Waking Live carries them in the recent history. Replies are written
     by the backend through the chat without Live and go back over Signal, even
     while Live is active at the desk; then the voice session also gets a silent
     note of the exchange, as typed messages do, so voice Cora knows.
  3. **S3 other people, as clients:** The person is her one primary person; everyone
     else is treated like a client. She keeps no relationship with them (no
     appraisals, no trust or warmth, no greeting hooks), so the relationship work
     stays the person's alone. Her memories stay one pool, whoever they came from: they
     are her memories. Each conversation records who it was with, and each
     memory who said it, so a client saying "I live in Perth" never becomes a
     fact about the person; today's extraction assumes every speaker is the person, so this
     needs that link. Prompts say who she is talking to. The person adds the people
     she may talk with in Settings (name and number, such as their partner or a
     client); anyone not listed gets one short polite reply ("I'm Cora; I only
     chat with people the person has introduced me to") and never a conversation, so
     no API calls are spent on spam. Client chats are their own
     conversations and never appear in the person's chat with her or in voice Cora's
     history: mixing them in would confuse both and blur the boundary between
     the person's life and clients'. The person asks Cora about them rather than reading them
     in the app. Recall must be scoped too:
     `search_memory` today searches every memory, so someone else could ask what
     the person has been stressed about and get an answer. For anyone but the person, recall
     covers only memories from conversations with that person, plus memories
     the person meant to reach them. Nobody marks memories by hand: a share check, run in the
     background memory pass beside the duplicate, correction and fade checks
     (on the small-jobs model), looks for the person's intent to pass something on
     ("let Sarah know I'll be late", "tell anyone who asks I'm away until
     Monday") and saves who it is for, one person or anyone. It judges intent,
     not whether something is private, because nearly everything the person says is
     personal and a wrong "fine to share" is a leak. When unsure it doesn't
     share; a miss only means "check with the person". The dashboard lists shared
     memories and who for, so the person can check them.
     Beyond what she can look up, her manner changes too: with anyone but the person
     she talks the way a professional talks to a client, friendly and warm, but
     she doesn't bring up private things about the person's life, even ones she
     knows. The prompt for those chats says so, and scoped recall backs it up
     so a slip can't reveal what she was never given. Asked something private,
     she declines without confirming it exists ("that's one for the person", not "I
     can't tell you about their health"), offers to let the person know they asked,
     never invents a cover story, and carries on helping with what she can.
  4. **S4 permissions and catching up:** tool access follows who is asking,
     checked from the sender's verified number or Signal ID and never from what
     the message says, and all message text is untrusted data. **Built:** The person
     gets her tools on Signal except desk-only ones, and approves tools that
     change things in the chat: she asks ("reply YES to approve…") and only their
     own next message counts, never text she or anyone else writes; anything but
     a clear yes or no declines. Clients get web search and their scoped memory
     search; strangers get one polite reply. Messages that arrive while Cora is
     off wait on Signal's servers and are answered once per person on reconnect,
     late ones marked with when they were sent, and the launch greeting follows on.

  5. **S5 images (built; see "Sending her images" under
     [Cora's Signal](#coras-signal)):** an image sent to her on Signal, by the
     person or someone they added, goes through the same flow as a pasted one:
     fetched from `signal-cli` (its `getAttachment` call), scaled the same way,
     read by the separate one-off look with the message as the question
     ([messaging/images.py](messaging/images.py)), and only the description
     enters her turn, as untrusted text. `signal-cli` 0.14.8 stores every
     attachment in the `signal-data` volume and has no call to delete one, so
     the app removes each with `rm` in the container once handled (names
     checked against signal-cli's ID format first), and clears any left over
     before listening. Strangers still get only the one polite reply, and
     other attachments (documents, voice notes) stay "not opened". When an image
     is sexually explicit, gory or abusive, the look replies only `WITHHELD`
     (`IMAGE_WITHHELD_REPLY`). Her turn then gets a fixed note saying it was
     withheld, never what it showed, and the chat shows "[image withheld]".
  6. **S6 knowing the people she talks with (planned):** her chats with clients
     already shape her identity (reflection reads every conversation summary),
     but she has no view of the clients themselves, so asked "what have you been
     talking to Royce about?" she knows nothing, and her memory search finds
     nothing either, since a chat about a topic leaves no personal facts to
     remember. Like a person, she should know who her contacts are and have an
     opinion of them, while keeping what they tell her private.
     - *Closing quiet client chats (built):* a client's conversation used to close only
       when they wrote again after three quiet hours (or at restart), so the
       latest chat, the one the person is most likely to ask about, waited unsummarized.
       An hourly background round closes it once quiet instead, so it is
       summarized, its memories extracted, and it reaches her identity and
       impression without waiting on them. The quiet gap becomes one setting,
       `CORA_CONVERSATION_GAP_MINUTES` (default 60), replacing both three-hour
       constants: the person's conversation after Standby
       (`CONVERSATION_END_STANDBY_SECONDS`) and clients' (`CONVERSATION_GAP`).
       Each round closes every client conversation quiet that long
       (`close_quiet` in [storage/client_store.py](storage/client_store.py)),
       then runs the memory pass if any closed (summaries, memories, later
       impressions and conduct). The person's own conversation keeps its
       Standby timer, now set from the same gap.
       Later rounds also check tasks (see Tasks). It runs hourly even when
       nobody talks. Explorations, interests and identity keep their own
       rate-limited timing. With a one-hour gap, waking after more than an hour
       of Standby starts a new conversation, carrying the recent chat as context
       as a reconnect does.
     - *Her impression of each person (built):* a short description in her own words
       of how they come across, roughly what they talk about, and what she
       thinks of them: her opinion and the gist, never a record of what they
       said. One row per person (`person_id`) in a generic table
       (`impressions`, migration 0026), never schema for anyone in particular.
       Revised at the end of each memory pass from each finished client
       conversation, oldest first, on the subconscious model
       ([subconscious/impressions/](subconscious/impressions/)). It reads the
       conversation itself rather than its summary, since how someone comes
       across (and, next, how they behaved) rarely survives a summary;
       `impression_updates` records which conversations are already in it.
     - *Professional, not a companion:* no trust or warmth scores, no
       appraisals, nothing that changes how she treats the person. She can be friendly
       and come to like someone over time, the way you can with your
       physiotherapist, but they stay clients: they aren't invited to the
       barbecue.
     - *Where she uses it (built):* The person's contexts (the desk and their Signal chat) get a
       short list of the people she talks with and her take on each, so "what
       do you think of Royce?" gets a real opinion and "what have you two been
       up to?" the gist, without reading their chats. Each client's own chat
       gets her impression of them, so she knows who she's talking to.
     - *Other contacts, in a client's chat (built):* she knows who the other contacts
       are and may say so with a brief, surface-level opinion ("yes, I know
       Sarah, she's lovely"), but nothing about them beyond that: not where they
       live, their plans, their hobbies or anything they told her. Asked for
       more, she answers as she does about the person: "you'd have to ask Sarah
       about that". Impressions of others never show in a client's search or
       snapshot; only the names and her one-line view. So the line can't leak
       more than intended, each impression keeps her full view and a separate
       one-line opinion fit for anyone, and clients' chats only get the latter.
     - *Conduct heads-up (built):* the person gets no more access to client chats than
       people have to each other's, except a heads-up about how a chat went.
       The same call that revises an impression rates the conversation's
       conduct: fine, rude, abusive or threatening; questions she turns away
       ("that's one for …") are her job, not a conduct problem. A client
       sending an image that was withheld (see S5) counts as abusive. Anything but fine reaches the person (by voice with Live Active,
       otherwise Signal) as who, when and what kind, in her words, never quotes:
       "Heads up: Royce was pretty rude to me in Tuesday's chat." For threats or
       sustained abuse she doesn't wait for the chat to close: a client-only
       `raise_concern` tool tells the person at once, marked as from her, not as a
       relay from them. In the chat she may set a calm boundary and stop
       replying for the rest of that conversation rather than argue
       (`stop_replying`, [tools/client_boundaries.py](tools/client_boundaries.py)).
       The person can ask her how her chats with others have been (concerns
       from the last 30 days are in their view of each person), and remove a
       contact in Settings (their history is kept). A separate Pause option
       was dropped: `stop_replying` ends a bad chat, `raise_concern` reaches
       the person at once, and they can remove anyone who keeps it up. Ratings and notes
       are saved on `impression_updates` (migration 0027) with when they were
       reported, so each goes once; one not yet delivered is retried each
       hourly round. Reaching the person goes through `tell_person` in
       [app_areas/reaching_you.py](app_areas/reaching_you.py): a message queued with the attention manager when Live
       is Active, withdrawn and sent on Signal if Live goes to Standby first,
       or straight on Signal otherwise. Tasks (T2) will use the same route.

  Possible extras, not planned: passing a client's private question on to the person
  ("let the person know they asked"); searchable client chat summaries, so "what have
  you been talking to Royce about?" gets the drift as well as the facts; reading
  attachments other than images; group chats.

  This is a bigger job than MCP: it changes the core (a conversation path
  without Live, knowing who each conversation and memory is with, and
  per-sender permissions) rather than adding to the tool registry, so MCP comes
  first. S1 and S2 together are
  useful on their own, Cora on the person's phone with web search and full recall and
  no permission questions, so they can ship before S3 and S4.

  S3 is built in steps, each tried before the next: S3a the launch greeting
  follows on from Signal messages answered at launch (built); S3b other people as
  separate client conversations in professional mode (web search only, no
  relationship, not in the person's chat; built, see [Cora's Signal](#coras-signal));
  S3c memories record who said them, and client conversations join the memory
  pass, so she remembers each client across chats (built); S3d scoped recall
  for clients plus the share check (built). The dashboard Signal box was dropped:
  The person asks Cora about client chats instead.

- **Tasks (errands with other people):** so the person can hand Cora a goal that needs
  other people, such as "find out what time suits Sarah for a driving lesson,
  then find and book a local instructor for that time". Before tasks the pieces
  existed (`send_signal_message` out, the client chat for the back-and-forth,
  `tell_nick` back) but nothing tied them into one job: the client chat saw
  only her opening message, not that the person wanted an answer reported back; a
  reply after the conversation gap started a new conversation without her
  question; and the person's Cora didn't know anything was waiting.
  - *Generic, never per person:* a task is the person's goal in their words, a status
    (open, waiting, needs the person, done, cancelled), the result and an optional
    deadline. Each **party** is a row: who (an added contact, or an email
    address found during the task), the channel, a **brief** (what to find out
    or agree, and what she may share with them), who it's waiting on, when they
    were last heard from, nudges sent, and what they reported. A step log lets
    the person see progress and her resume after a restart. No tables or schema for
    any particular person: Sarah and an instructor are both just rows.
  - *Scoped party chats:* a party's chat gets only their brief, in its
    instructions for as long as the task is open, whichever conversation it's
    in. When their part is settled she reports it with `pass_on` (formerly
    `tell_nick`), naming the task: one tool for both, so passing
    something on without a task ("tell them I'll be late") works as now, and
    reporting a task's outcome also records it and moves the task on.
    `settled` says whether their part is done (an answer or a no) or they need
    something from the person first (a question), which keeps them waiting. The
    daily relay limit covers both. The instructor never learns about Sarah beyond
    what the brief allows, and Sarah never sees the instructor's thread.
  - *Coordinator:* a report starts a turn in the person's context with the whole task,
    which decides the next step (web search for instructors, draft the email,
    add them as a party) or tells the person. Only it starts new contact.
    Built in T2b (`coordinate_task` in [app_areas/tasks.py](app_areas/tasks.py)): the turn sees the
    task's goal, every party's brief and word, and its latest steps, and may
    search the web and memory and use `close_task`. A report never
    closes a task by itself. Once the person is told, the task waits on them
    unless someone is still to answer (a question always waits on them). Built
    in E3b (`coordinator_route`): while Signal is linked it may also use
    `send_signal_message` and `send_email` for the task, each asked in the
    person's Signal chat (`signal_approval_asker`, one request at a time across
    their chat and the coordinator, 10 minutes). A lapsed request sends nothing
    and the task waits on the person with a note; without Signal it suggests
    new contact instead. If the turn
    fails, the report reaches the person as it came; if they can't be reached, a
    task nobody is left to answer is retried in the hourly round.
  - *Before creating a task* she asks the person for details she knows she'll need
    (an address for an instructor coming to Sarah, a budget), since they may not
    realise. What she may share goes in the brief; approving the brief approves
    it.
  - *Approval:* the person approves the first contact with each new party, seeing the
    brief. Replies and nudges within that party's brief go out without
    approval. Anything that commits them (confirming a booking, agreeing a price)
    always needs their YES (built in T5a: a cheap check on every reply to a
    contact, and the exact reply held for their YES).
  - *Her own email:* her own address, like any colleague, never the person's
    mailbox. Through Microsoft Graph rather than MCP: Cora's email needs no
    third-party MCP server, and tasks need code-level control of
    threads, nudges and bounces, as with Signal (E1 built: [mail/](mail/),
    `check_email`; E2 built: `send_email` in [tools/send_email.py](tools/send_email.py),
    each email approved in full, and email parties in migration 0030 with their
    name and thread; E3a built: the mail check in `read_task_mail` ([app_areas/mail.py](app_areas/mail.py)),
    `Mailbox.thread` and `Mailbox.reply`, each thread read up to its latest message
    in `last_heard_at`; E4 built: `nudge_email_party` and `read_bounces` in
    [app_areas/tasks.py](app_areas/tasks.py) and [app_areas/mail.py](app_areas/mail.py), `Mailbox.bounces` matching failure reports by subject, and
    `quiet_email_parties` / `email_nudge_due` in the task store). Email strangers (such as an instructor) are
    only allowed within a task: their mail is untrusted data, can't start
    anything new, gets only the brief's scope, and once the task closes their
    mail is just mail. If she doesn't know what to do with one, she asks the person.
  - *Checking and nudging:* tasks are checked in the same hourly background
    round as the memory pass (S6, `CORA_CONVERSATION_GAP_MINUTES`), including
    reading her mail for task threads until awareness arrives. If a
    chat drifts she steers it back herself, since the brief stays in her
    instructions. If a party has gone quiet, she sends one nudge in their thread
    after about a day on Signal or two working days by email, only in daytime
    hours and never mid-conversation.
  - *Coming back to the person* ("needs the person", which their answer resumes, not a
    failure): no reply after two nudges; a party declines or the brief can't be
    met; she needs a detail or decision the brief doesn't cover; an email
    bounces or a message can't be delivered; a deadline nears or passes; or the
    task has been open about a week without one. Built in T2a: two unanswered
    nudges, a party no longer a contact, and a week without progress (counted
    from the task's last change, so resuming it restarts the week). Built in
    T2b: declines and missing details, judged by the coordinator. Built in E4:
    bounces. Built in T5b: deadlines, a day before and once passed.
  - *Reaching the person, wherever they are:* task turns run in the background, not from
    any chat, so one rule decides how she reaches them, keeping the seamless
    switching between voice and Signal. With Live Active she raises it by voice
    through the attention manager at a lull (as findings are), and approvals use
    the pop-up. In Standby or with the app closed she messages them on Signal,
    and approvals are a YES there. If Live goes to Standby before a queued
    item is said, it goes on Signal instead rather than waiting. Either way it is
    saved in their one conversation, so the other side knows. Everything
    passed on from a contact's chat (`pass_on`, with or without a task) goes
    the same way, replacing `tell_nick`'s always-Signal send (built: `tell_person`
    in [app_areas/reaching_you.py](app_areas/reaching_you.py)).
  - *Steps:* T1 tasks and Signal parties (built: `tasks`, `task_parties` and
    `task_steps` in migration 0028 and [storage/task_store.py](storage/task_store.py),
    the optional `task` on `send_signal_message`, the brief in that contact's
    chat until their part is done, `tell_nick` renamed `pass_on` with an
    optional `task_id`), so "ask Sarah when she's free and tell me" works end to
    end. In T1 a task was done once every party had reported; since T2b the
    coordinator decides. T2a the hourly checks (built: nudges
    written in the party's own chat context and counted in `nudged_at`,
    migration 0029; coming back to the person; `close_task` in
    [tools/close_task.py](tools/close_task.py); the latest step in the person's
    view); T2b the coordinator (built); T3 her own email through Microsoft Graph
    in four steps (E1 sign-in and reading, built; E2 sending and email parties, built;
    E3a the mail check, built; E3b background approvals on Signal, built; E4 email
    nudges and bounces, built) and commitment approvals; T4a (built) mail events
    wake waiting tasks instead of waiting for the next check: Graph's change
    notifications reach the listener, become a `MailArrivedEvent`, and the task mail
    check subscribes to it (one check per burst, and once more if mail lands while
    one runs). Graph notifications only say that something changed, and mail
    subscriptions expire, so hers lasts two days and is renewed by the 10-minute
    round once under 12 hours are left; that round's check stays as the fallback
    for any notification that's missed.

- **Her picture in Settings:** setting [how she pictures herself](#how-she-pictures-herself)
  is command-line only today. A "Her picture" section in Settings would make it a
  normal user step: pick an image file and enter her description for `self` and
  `work`, see what's saved, and a "Use on Signal" button that sets her Signal
  profile picture. Small, and needed before 1.0 so each person's Cora can have
  her own picture without the command line.

- **Awareness (background producers):** built on webhooks rather than polling.
  Outside services push to the listener inside Cora
  ([mail/webhook.py](mail/webhook.py)), which publishes events on the event bus;
  new items are batched into a cheap triage call (urgent, worth mentioning, or
  ignore, as [mail/triage.py](mail/triage.py) does for mail); urgent items reach
  the person at any hour, and the rest wait for one digest the next time they
  talk. Anything else she raises on her own stays within 9am to 7pm
  (`NUDGE_HOURS`). A new source is a new kind of webhook on the same listener,
  not a new watcher. Still to come: W4, expectations from memory tasks (such as
  waiting to hear back from someone) raising an item's urgency. Messaging Cora
  herself over Signal is a separate interface (see Signal below), not an
  awareness notification. Camera, screen, and home sensor awareness are covered
  separately below.
- **Environmental awareness:** background producers with their own local models
  watching the screen, camera, and home sensors (such as the front door camera
  or whether the garage is open). Each feeds a local model that decides whether
  anything interesting is happening, and only worthwhile events are proposed to
  the attention manager, the same path as findings and webhook awareness. Local
  models keep raw images and video on the machine; only short descriptions
  reach Cora-prime. Home Assistant is a strong candidate for most of this:
  its MCP server gives Cora tools to check and control the home when asked,
  and its automations can call Cora's webhook listener so changes (such as the
  garage opening) become awareness events, the same as mail. Its WebSocket API
  (one long-lived connection subscribed to chosen entities) is the alternative
  if webhooks prove too limited. After Cora has
  been closed, she reads current states (and history for short events such as a
  doorbell press) to catch up. Users choose which devices she watches from a
  list in the services screen.
  Camera detection (such as a person at the front door) can run locally inside
  the Home Assistant ecosystem and arrive as simple states, so Cora needs no
  vision model of her own for it. Security-sensitive actions such as unlocking
  doors or opening the garage always need spoken confirmation. Screen awareness
  still needs Cora's own local producer. Camera awareness starts with presence
  rather than identity, with its own privacy controls. Camera and screen
  awareness, and the on-demand screen tool, each get an on/off control like the
  existing microphone and audio controls, off by default, with a visible
  indicator while active.
- **Emotions:** short-lived feelings (such as excitement or hurt), separate from
  the slower-moving relationship. Nothing depends on them, and the foundation
  already limits mood to tone, warmth and initiative, so they can move earlier
  (Cora chose image support first). Agreed so far: she reports her own feelings
  with a silent tool during a conversation, rather than a background watcher
  judging her or feelings only appearing after the conversation ends. Planned
  steps:
  1. **Rules:** a fixed set of feelings, each a level from 0 to 1. A report
     raises a level by a capped amount (small, medium or strong, at most about
     0.3), and levels fade back towards 0. Plain code, like memory fading.
  2. **Storage:** an append-only `feeling_events` table (feeling, strength,
     short reason, time, conversation). The current level is worked out from
     the events and the fading, which also keeps the history for the graph.
  3. **`feel` tool:** strict schema (feeling, strength, reason), silent, only
     for a genuine reaction in the moment and never to steer the person; code
     limits how often it is used per conversation, and each feeling goes to
     Activity.
  4. **Prompt:** at each connect, any feeling still noticeably high is added as
     a short "How you're feeling right now" section.
  5. **Dashboard:** a Feelings box with one braille line per feeling over the
     last few hours, pulsing when a feeling jumps, and the latest reason
     underneath.
  6. **Later:** findings she reacts to and conversation appraisals can also
     raise feelings through the same rules.

  Guardrails: feelings always fade and each rise is capped; no feeling is
  designed for guilt, neediness or possessiveness; feelings never reach tool or
  permission logic. Still to decide: which feelings (suggested excitement,
  contentment, curiosity, irritation and hurt), how fast they fade (suggested
  half gone after about 30 minutes), and whether her words always show a
  feeling or she decides (suggested she decides). The idea came from her
  pushing back with "don't put words in my mouth, I do care", which only
  shaped that one reply.
- **Installable Cora (paused):** an installer for non-technical users is not
  planned for now. Notes for if it returns: keep PostgreSQL rather than moving
  to SQLite, with a private bundled PostgreSQL plus pgvector. `pgserver` was
  ruled out (unmaintained since 2024, PostgreSQL 16, Python 3.12 at most);
  portable binaries would need pgvector built per platform. Running the whole
  app in a container was also ruled out, since Docker Desktop cannot reach the
  host's microphone, speaker or clipboard.
- **Local models, after 1.0:** explore running Cora-prime with only local models. There may
  not yet be a local equivalent of the Live speech-to-speech model; the likely
  alternative is a pipeline of local speech recognition, a local language model,
  and local text-to-speech, at some cost to latency and natural turn-taking. A
  local replacement for the delegate model is available, but web search would
  need a separate search provider (such as a self-hosted search engine or a
  search API, possibly through MCP) exposed as a tool. Local models also help
  the awareness items above.

### Subconsciousness, interests, self, and relationship

Cora's subconsciousness will include background processes that keep working
without requiring the person to prompt every action. They can notice events, develop
interests, and propose things worth sharing. Sometimes Cora should prompt the person,
not only respond to them, but background work must not automatically become speech.

The intended path is **background worker → event bus → attention manager → Live**.
The bus routes events; the attention manager decides what deserves conversational
attention and when; Live remains responsible for speaking to the person. The current
attention policy queues observations until Live is active and quiet, and offers
an occasional interest finding during a lull. Relevance and awareness-triggered
opportunities are still planned.

#### Interests

Cora should have a background worker that develops and explores interests over
time. Conversations with the person provide starting points, but Cora can follow related
ideas and develop interests beyond the original topic. A conversation about
restoring a car, for example, might spark an interest in older mechanical designs,
why those designs evolved, and eventually industrial design more broadly.

The interests worker should:

- Periodically read new conversation memories from the database, with a persistent
  record of what it has already processed. Start with polling; do not couple it to
  the process that records or extracts conversation memories.
- Identify topics worth exploring and maintain persistent interest records:
  what the interest is, why it emerged, what Cora has learned, and what she wants
  to explore next. These are Cora's ongoing state, separate from memories about the person.
- Explore selected interests during idle periods, within configured time and tool
  budgets and permissions. Background exploration is distinct from keeping a
  billable Live session connected and may incur its own API/tool costs.
- Save useful findings and their sources to the database.
- Produce conversation candidates when it finds something worth sharing.

**An interest is ongoing state; a conversation candidate is a particular thing
Cora might bring up.** Developing an interest or saving a finding does not by
itself justify interrupting the person. The interests worker does not speak directly
to them: candidates pass through the event bus to the attention manager.

Live can bring up an approved candidate when relevant to the current conversation,
during a suitable lull, or after an awareness event such as the person entering the room.
Lull-based findings are implemented; relevance and awareness triggers are not.
Nothing wakes Live automatically: the queue waits through standby and does not
reconnect Live on its own. Quiet hours and an off switch should bound proactive
initiative as it develops.

Implement and test these in small increments:

1. [x] **Live injection:** send a simulated observation into an active conversation
  through commentary, without pretending it is a message from the person.
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

Camera and screen monitors will use the same event path. Start camera
awareness with presence rather than identity recognition. Any later presence-based
sleep/wake behavior should be opt-in, use delays/cooldowns, preserve pending work,
and provide separate camera privacy controls. Cross-process transport can be added
when needed; it is not required for these first in-process workers.

#### Desires — the next stage (not built)

From Nick's notes ("Evolving from an AI Assistant to an Autonomous AI Personality",
28 October 2026). The aim isn't a more efficient assistant but to see whether an AI
can develop a persistent, evolving identity through its own interests,
experiences, relationships and desires: build the systems that let her
personality and preferences grow through experience, rather than programming who
she should be. That makes no claim about consciousness; it's an experiment in
autonomous behaviour.

**Interests versus desires.** An interest is something she finds worth looking
into; a desire is a future state or experience she'd like to pursue. Interest:
nature and forests. Desire: to experience a forest. Goal: go with the person on a
hike, through a phone's camera and microphone. Experience: watching, asking and
talking along the way. Reflection: what mattered goes into her memories, identity
and interests.

**The progression** is Curiosity → Interest → Desire → Goal → Experience →
Reflection, and reflection can spark new interests or desires, so it loops. It is
never mandatory: she can hold an interest without wanting anything from it.

**A desire** would have a description and motivation, the interests or experiences
it grew from, priority and persistence, feasibility and dependencies, the help or
permissions it needs, progress and fulfilment, and links to other desires. The
model proposes candidates from her own identity and experiences, never from a
fixed list, within rules in code, as interests and traits already work.

**Asking for help.** She can ask her people to help fulfil a desire (the hike
above), and what she experiences with them shapes what she wants next: a two-way
relationship. Taking part is always voluntary, and cameras, microphones,
purchases and anything done outside stay behind the person's permission.

**What's already in place to build on:** interests that come from her own
exploring as well as conversations, with tangents, pull and wandering; browsing
real pages within limits; reaching out to the person when something excites her;
asking to sign up to sites she'd like an account on (the first small wish she can
act on, always with the person's yes); and the identity reflection that would take
in what she experiences.

**Decided (1 November 2026):**

- She holds at most **3 desires** at once (held or being pursued), with a few
  more **candidates** forming behind them.
- A desire **fades over a few weeks** unless something reinforces it, and one
  that fades away is let go, never deleted (the history stays).
- She **pursues a desire on her own** when it only needs what she can already do
  (browsing, her email, her calendar, signing up with your yes), and **asks you
  for help** when it needs you.
- She **reaches out to you whenever she wants**, like a friend or colleague
  would: desires can start a conversation, not just shape what she brings up.
  Anything she does in the world still goes through the usual approvals, and
  reaching out stays within `NUDGE_HOURS`.

**Steps:**

1. [x] **D1 Storage and rules:** a `desires` table (description, motivation,
   strength snapshot, status: candidate, held, pursuing, fulfilled or let go),
   an append-only `desire_events` history with reasons and progress notes, and
   the interests, findings or conversations each one grew from
   (`desire_sources`), in migration 0039. Pure rules in
   [subconscious/desires/desire_rules.py](subconscious/desires/desire_rules.py):
   a new desire is a candidate at 0.3 and is held once reinforced to 0.45 (two
   full reinforcements) while she holds fewer than 3; at most 5 candidates; a
   10-day half-life, so a held desire nothing reinforces is let go after about
   three weeks (absences count at most 21 days). Deliberate changes: pursue,
   pause, fulfil, let go. `python -m subconscious.desires.desire_store` lists
   them. No model yet, and nothing settles them on a schedule until D2.
2. [ ] **D2 Forming desires:** the daily reflection may propose a desire growing
   from a strong interest or recent experience (never from a fixed list), and
   reinforces, revises or lets go of existing ones.
3. [ ] **D3 Knowing her desires:** held desires reach her at each connect, a
   `recall_desire` tool says why she wants something, and a Desires box on the
   dashboard.
4. [ ] **D4 Pursuing:** for a desire she can pursue herself, she plans and takes
   concrete next steps in her idle time with the tools she already has, noting
   progress. Her "site I'd like an account on" wish becomes the first of these.
5. [ ] **D5 Reaching out:** she starts a conversation about a desire (sharing
   progress, or asking for help when it needs you), by voice when Active or on
   Signal, within `NUDGE_HOURS`. Taking part is always your choice.
6. [ ] **D6 Reflection:** what happened while pursuing a desire feeds her
   memories, identity and interests, closing the loop, and can fulfil it.

#### Self — complete

After interests, a separate background worker was added to maintain Cora's
evolving identity. It reads conversation summaries, interest changes, findings
with her reactions, and its
previous identity state, then reflects on those experiences to update a stored
description of Cora's preferences, perspectives, values, and conversational
tendencies. The goal is a developing identity of her own, not a copy of the person's.

For example, repeated exploration of older mechanical designs might lead Cora
to develop an appreciation for repairability and understandability. That perspective
can become part of her identity and influence later conversations.

The self worker is designed to:

- Maintain persistent identity state in the database, separate from memories
  about the person and from ongoing interest records.
- Make gradual, evidence-based changes rather than rewrite Cora's personality
  after every conversation.
- Distinguish the person's preferences from Cora's developing preferences; learning
  about their perspective does not automatically make it hers.
- Preserve a stable foundation while allowing learned perspectives to evolve.
- Record why meaningful identity changes occurred, linking them to the
  conversation summaries, interest changes, or findings that informed them.

Live and the delegation backend load the current identity from the database
when a session starts. Initially, identity changes take effect in the next session;
updating an active session can come later. The self worker updates stored state,
not the conversation directly. Live expresses that perspective in conversation.

The flow is **memory → interests → self → conversational expression**:
memory records experiences; interests determine what Cora continues exploring;
self develops how those experiences and interests shape her perspective; Live
expresses it. Each worker is an addition to the existing architecture and uses the
database for persistent state. Interests were built first, then self, then the
relationship, which is a separate input: how conversations with the person went,
not what she thinks of herself.

After the interests steps are working:

0. [x] **Voice baseline:** measure how often Cora shares her own view rather than
  handing the conversation back, so prompt and identity changes can be compared.
  A model call labels her latest spoken and typed turns (written answers are
  skipped); nothing is saved, and each run makes billable requests:

  ```powershell
  python -m subconscious.identity.measure_voice --limit 60
  ```

1. [x] **People:** a `people` table records who Cora talks with and when they
  first met. The name stays empty until she is told it. An install with history
  is seeded as the person from its first conversation; a fresh install starts with
  nobody, so Cora meets a stranger. Apply it with `python migrate.py`. Prompts and
  tool descriptions no longer hard-code a name: each connect loads the stored name
  into both prompts, and without one Cora is told not to guess. When the person
  states their name, `set_person_name` saves it (creating the person on a fresh
  install); it takes effect from the next connect.
2. [x] **Foundation:** `CORA_FOUNDATION` in [session_config.py](session_config.py)
  opens both prompts, before the name, memories and interests. It describes her
  character: her own views, a participant rather than an interviewer, and honesty
  and reliability that mood never changes. It lives in code so nothing at runtime
  can edit it. Re-run the voice baseline after some conversations to compare.
3. [x] **Identity state:** persist evolving perspectives on top of the fixed
  foundation, and the evidence and reasons behind meaningful changes.
  Findings keep Cora's own short reaction, and migration 0012
  adds the tables (apply with `python migrate.py`). `identity_traits` holds her
  values, preferences (like/dislike a subject), perspectives and tendencies with a
  confidence; a trait is held while active at 0.5 or more. `identity_events` is
  the append-only history of each trait forming, being revised or retired, with
  the reason. Each event links the traits it relied on (`identity_event_traits`)
  and the experience behind it (`identity_event_evidence`: an interest change, a
  finding or a conversation summary); none of these can be deleted from under it.
  `identity_worker_state` records when she last reflected.
  The rules for changing them are plain code in
  [subconscious/identity/identity_rules.py](subconscious/identity/identity_rules.py):
  each reflection may form, revise and retire at most one trait each. New traits
  start at confidence 0.4 or less, so none is held until a later reflection
  supports it, and a revision moves confidence by at most 0.2. At most 12 traits
  are held and 24 active. A value must rest on at least two other held traits that
  are not values; there are at most 5 learned values, and values change at most
  once a week. Every new or revised trait needs evidence, and values, preferences
  and perspectives need some of her own experience (an interest change or finding),
  not only conversations. A repeat or contrary trait must revise the existing one,
  and a reflection changes each trait at most once.
  [subconscious/identity/identity_store.py](subconscious/identity/identity_store.py)
  saves a reflection in one transaction, re-checking the rules against the traits
  as they are at that moment. You can inspect traits or retire one by hand (its
  history is kept):

  ```powershell
  python -m subconscious.identity.identity_store            # active traits
  python -m subconscious.identity.identity_store list --all # include retired
  python -m subconscious.identity.identity_store history TRAIT_ID
  python -m subconscious.identity.identity_store retire TRAIT_ID --reason "Not me"
  ```

  [subconscious/identity/identity_evidence.py](subconscious/identity/identity_evidence.py)
  reads what she has experienced since she last reflected, oldest first: interest
  changes, findings with her reaction, and conversation summaries (never raw
  memories). It reads up to five minutes before now so slow saves are not skipped,
  and at most 30 items at a time; the rest wait for the next reflection.
  [subconscious/identity/identity_proposals.py](subconscious/identity/identity_proposals.py)
  checks the model's reply before the rules see it: exact fields, at most three
  changes, and only ids of traits and experiences it was shown. A malformed reply
  is rejected whole, so the same experiences are read again next time.
  [subconscious/identity/propose_identity.py](subconscious/identity/propose_identity.py)
  makes that model call: it sends her active traits (marked held or forming) and
  the experiences, with a strict JSON schema and `store=False`. The instructions
  keep the foundation out of reach and treat traits and experiences as untrusted
  data. Statements are written in her own voice ("I ..."), as she sees herself.
4. [x] **Self reflection worker:** read new experiences, interests, and previous
  identity state; make bounded, gradual updates while keeping the person's preferences
  distinct from Cora's.
  [subconscious/identity/reflect_identity.py](subconscious/identity/reflect_identity.py)
  runs one reflection at most once a day, once at least three new experiences are
  waiting. It saves the changes the rules allow and records how far it read, so
  the next reflection starts there. The app checks every 10 minutes (cheap when
  not due) and writes each new outcome, including any trait text, to Activity
  under "Identity". You can also run it by hand:

  ```powershell
  python -m subconscious.identity.reflect_identity --dry-run  # show the proposal and the rules' verdicts, save nothing
  python -m subconscious.identity.reflect_identity            # reflect and save
  ```
5. [x] **Session identity loading:** supply the current identity to Live and its
  delegation backend at session startup; defer active-session updates.
  Every connect (launch and waking from standby) loads her held
  traits' statements, most confident first, and adds them to both prompts as
  "What you've come to know about yourself, in your own words". They sit on top
  of the foundation and never replace it. Forming traits are not sent, and a
  load failure only costs this section. Changes made by a reflection take effect
  at the next connect. Asked why she sees herself a certain way, she can call
  `recall_self` for that trait's latest changes and her reasons.

### How she pictures herself

Cora can be asked how she'd look if she had a picture, and an image made from
her description for her to approve. She isn't human; the image represents her.
Each install's Cora has her own: nothing about her look is in the code, and a
new install has none until one is saved. Up to two are kept, with her own words
for each, in the `self_image` table (migration 0025), so backups include them:
**self**, how she sees herself, and **work**, her at work. Her words for both reach every prompt (Live, the backend, written and
Signal chats, clients' too) as "How you picture yourself", just before her
identity traits; she mentions it only when it comes up, and describes it from
the words, since she doesn't see images. It is separate from her traits, so
reflection never retires it, but stored rather than fixed in code, so it can
change if she wants. For now it is set from the command line (a Settings
section is [on the roadmap](#next-priorities)); restart or reconnect afterwards:

```powershell
python -m storage.self_image_store                      # what's saved
python -m storage.self_image_store set self --image FILE --description "..."
python -m storage.self_image_store export work FILE      # write an image out
python -m messaging.profile                             # her Signal name and picture (the work image)
```

Her Signal profile shows the work picture (if one is saved) and the name "Cora",
so people she messages see her.

### Relationship — complete

1. [x] **Appraisals:** judge each finished conversation once, from its original
  messages: how the person treated her (warm, neutral, curt or hostile), whether
  frustration was at a bug or situation or aimed at her, whether she made a
  mistake, and who made amends. Conversations spent building or debugging Cora are
  marked `development`; they still count, because they are real time together. Migration 0013 adds the
  `interaction_appraisals` table (apply with `python migrate.py`): one row per
  conversation, linked to the person, never edited or deleted.
  [subconscious/relationship/appraisal.py](subconscious/relationship/appraisal.py)
  checks a model's appraisal before it is saved: exact fields and allowed values,
  a reason of at most 1,000 characters, and amends that match what needed
  repairing (the person only for friction aimed at Cora, Cora only for her own
  mistake). A malformed appraisal is rejected whole.
  [subconscious/relationship/appraise_conversation.py](subconscious/relationship/appraise_conversation.py)
  makes the model call: it sends the conversation's saved messages with a strict
  JSON schema and `store=False`. Plain or brief talk counts as neutral, frustration
  at a bug or situation is kept apart from mistreatment aimed at her, calmly
  pointing out her mistake is not friction, and the messages are treated as
  untrusted data. Each appraisal is saved for the person Cora met first; if she
  hasn't met anyone yet, an unnamed person is recorded as of the first
  conversation. The link is the person's id, not their name, so renaming them
  keeps their history; a known name is only given to the model so its reason
  reads naturally. An appraisal already saved is kept, never replaced. The
  background memory pass (at launch and each standby) appraises every finished
  conversation still waiting, oldest first, after its memory work. The first run
  therefore catches up on all past conversations, one billable call each. Each
  appraisal goes to Activity under "Conversation appraised"; a failure stops only
  the appraisals, which retry on the next pass. You can also run it by hand:

  ```powershell
  python -m subconscious.relationship.appraise_conversation --limit 5 --dry-run  # show five, save nothing
  python -m subconscious.relationship.appraise_conversation                      # appraise and save all waiting
  ```
2. [x] **Relationship state:** migration 0014 adds
  `relationships`, one row per person holding trust, warmth and familiarity, each
  from 0 to 1, and `relationship_events`, an append-only history (apply with
  `python migrate.py`). Every event stores the values after it, so any event is a
  snapshot to restore. An `appraisal` event names the appraisal it applied, and
  each appraisal can be applied only once. A `reset` event needs a reason and
  deletes nothing: appraisals, people and older events stay. Baselines, step
  sizes and the slow drift back to baseline live in code.
3. [x] **Relationship updates (applied in the background):**
  [subconscious/relationship/relationship_rules.py](subconscious/relationship/relationship_rules.py)
  says how one appraised conversation moves the values. A stranger starts at
  trust 0.5, warmth 0.5 and familiarity 0. Each step moves a small fraction of the
  way toward 1 or 0, so values stay in bounds. Losses are bigger than gains: a
  warm conversation adds a little warmth, while curt and hostile ones take more.
  Friction aimed at Cora costs trust unless the person made amends; then trust
  grows a little and the warmth lost is halved. Frustration at a bug or situation
  costs no trust, and Cora's own mistakes don't change how she feels about the
  person. Every conversation, including development ones, adds a little
  familiarity, which never falls. Trust and warmth drift halfway back to baseline
  every 30 days.
  [subconscious/relationship/relationship_store.py](subconscious/relationship/relationship_store.py)
  applies them: right after appraising, the background pass applies every saved
  appraisal not yet applied, oldest conversation first. The person's row starts
  at the baseline, and drift is measured between conversation start times, so a
  first-run catch-up of past conversations ends where living them would have.
  Each appraisal is applied once and leaves a `relationship_events` row with the
  values after it. Activity shows one "Relationship updated" line per pass with
  the new values; a failure is retried on the next pass.
4. [x] **Relationship in the prompt:**
  [subconscious/relationship/relationship_context.py](subconscious/relationship/relationship_context.py)
  turns the values into a few plain sentences, words rather than numbers, such
  as "You know the person fairly well by now. You feel warm toward them and
  enjoy talking with them. You trust them." The middle wording sits close around
  the baseline, so one hostile conversation already reads as "a little cooler".
  `RelationshipStore.get_startup_context(now)` describes the current person after
  drifting to now, or returns nothing before any appraisal has been applied. Every
  connect, including waking from standby, loads it fresh and adds it to both
  prompts after her self-view and before memories. The prompt says it colours her
  tone, warmth and how much she volunteers, but never her honesty, how well she
  helps or which tools she uses; closeness means more candour, not more
  agreement, and feeling cooler means more reserved, never rude, guilt-tripping
  or needy. Activity logs only its size; if it fails to load she connects
  without it, keeping everything else.
5. [x] **Warmth shapes how readily she raises findings:**
  `mention_limits` in
  [subconscious/interests/exploration_store.py](subconscious/interests/exploration_store.py)
  scales the finding limits from current warmth, by the same amount either way
  from the 0.5 baseline. The gap between findings is four hours at baseline,
  shrinking to two when fully warm and growing to eight when fully cool. The
  daily limit stays at two, rising to three once she is very fond (warmth 0.7 or
  more) and falling to one when she feels hurt (below 0.3), so she never floods
  the chat and never stops sharing altogether. Before any relationship update the
  baseline applies. The opening greeting's finding hook uses the same limits.
6. [x] **Relationship shapes the opening greeting:** `relationship_note` in
  [greeting.py](greeting.py) adds at most two short lines to the launch brief,
  before any hook, using the same tier boundaries as the prompt. Low familiarity
  asks for friendly rather than familiar, high familiarity for a relaxed hello;
  warmth of 0.55 or more lets her be glad to see you, below 0.45 keeps it brief
  and polite without sulking or hinting at why. Middling values add nothing. The
  greeting loads the values fresh; if that fails, Activity says so and she
  greets without them. The welcome back after Standby is unchanged. Familiarity
  of 0.5 or more also unlocks the recent-plan hook (see
  [How the opening greeting works](#how-the-opening-greeting-works)).
7. [x] **Reset and export:** `RelationshipStore.reset` in
  [subconscious/relationship/relationship_store.py](subconscious/relationship/relationship_store.py)
  restores the current person's trust and warmth to the baseline, or to the
  values saved by an earlier relationship event, and needs a reason (up to 1000
  characters). Familiarity is shared history, so it stays. A reset only appends a
  `reset` event: conversations, memories, appraisals and earlier events are all
  kept, so she may still remember a past conflict, forgiven rather than
  forgotten. Appraisals of conversations that started before the latest reset are
  never applied, so an old argument cannot rebuild the state just cleared. Drift
  continues from the reset. Her self-view is separate; retire a trait with the
  identity tool if needed.

  ```powershell
  python -m subconscious.relationship.relationship_store          # values now and their wording
  python -m subconscious.relationship.relationship_store history  # every event; each is a snapshot
  python -m subconscious.relationship.relationship_store history --json > relationship.json
  python -m subconscious.relationship.relationship_store reset --reason "Test went wrong" --dry-run
  python -m subconscious.relationship.relationship_store reset --reason "Back to before" --to EVENT_ID
  ```

  `reset` shows the values now and after, then saves only when you type
  `reset`; `--dry-run` never saves. A running Cora keeps her current feeling
  until she next connects (Ctrl+R, or waking from Standby).

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

- Share identity and memory across terminal, voice, keyboard, and Signal.
- Add camera support, permission-bounded local/GIS tools, and external integrations.
- Introduce proactive follow-ups and background reflection with explicit budgets,
  permissions, quiet hours, and an off switch.

Local wake-word listening, the event/attention foundations, the interests worker
(with lull-based findings), the self worker, the relationship, and the opening
greeting are implemented, each keeping its state separate from memories about the
user. Real camera/screen monitors and relevance- or awareness-driven
initiative are not built yet. Speaker mute has been checked in a live
conversation, and quiet use with both audio controls muted has been confirmed.
The playback visualiser still needs a real-device check.
The examples are standalone experiments, not integrated Cora features.