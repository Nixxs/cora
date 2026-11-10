# How Cora works

How everything in Cora works, checked against the code. Setting her up is in the
[README](README.md); what's next is in the [roadmap](ROADMAP.md).

1. [Overview and architecture](#overview-and-architecture): Cora-prime and the background, ways to reach her, what she knows, what runs when, models and costs
2. [Talking to Cora](#talking-to-cora): the desk, voice, standby and the wake word, the greeting, the dashboard, playing games
3. [Her inner life](#her-inner-life): memory, interests, desires, self, her picture, relationship, feelings
4. [People, tasks and reaching you](#people-tasks-and-reaching-you): contacts, how she reaches you, tasks
5. [Approvals and safety](#approvals-and-safety): approvals, untrusted content, guards, secrets
6. [Services](#services): GitHub and the browser, Signal, email, calendar, sign-ups, webhooks, the web interface, phone calls
7. [Development](#development): project layout, tests, adding a tool

## Overview and architecture

Cora is one app on your PC: a terminal app ([app.py](app.py), with its parts split by area
into [app_areas/](app_areas/)) and a PostgreSQL/pgvector database in Docker that holds
everything she keeps. Her thinking is done by OpenAI models over the API. The optional
services (Signal, email and calendar, the web page, phone calls, MCP services) connect to
the same app, so there is always one Cora with one memory, however you reach her.

### Cora-prime and the background

**Cora-prime** is the part of Cora you talk to: two models working together in one Live
session.

- **Live (`gpt-live-1`, voice `marin` unless you choose another)** listens, holds the conversation and speaks. It
  answers directly when it can.
- **The backend** is a Responses model (the *Delegation* model in
  [Choosing models](#choosing-models)). Live hands it anything that needs more: current
  information (OpenAI's hosted web search), Cora's own tools, or a detailed written answer.
  The backend chooses the tools; the app validates and runs them locally. Code, tables and
  written deliverables appear on screen, and Live explains them briefly rather than reading
  them aloud.

Everything else (memory extraction, summaries, embeddings, memory checks, conversation
appraisals, her view of the people she talks with, interest evolution and exploration,
identity and desire reflection, spotting chances for what she wants) runs as **separate
background calls**. Each is a fresh, standalone request (`store=False`) with only the input
that job needs. Its instructions, reading, searches and reasoning stay inside that call and
never enter Cora-prime's conversation; only small, size-capped results come back, as stored
state she loads at her next connect. Relationship updates and feelings are plain code, with
no API call.

This keeps Cora-prime's context protected as Cora grows: a background process can be as
large as it needs, because only its capped output reaches her. New features follow the same
rules: give every feed into Cora-prime a hard size limit, and prefer on-demand tools over
loading things at startup.

### Ways to reach her

All of your own channels are one conversation: what you say on Signal or the web page shows
in the desk's chat, and voice Cora is told about it. The people you add (contacts, or
*clients*) each have their own conversation with her, kept apart from yours.

| Channel | What answers |
| --- | --- |
| Desk, voice | The Live session, while she's Active |
| Desk, typing | While Active, the backend through the Live session (a silent copy keeps Live's context up to date, so you can switch between typing and talking). In Standby, the backend directly, without waking Live |
| Your Signal chat, the web page (typing) | The backend directly, as a written chat; tools that change things ask you in that chat |
| The web page's **Talk**, your phone calls | The same Live session as the desk, with the audio carried to the page or the call |
| A contact on Signal or the phone | Client mode: their own conversation and memories and a smaller set of tools; a call gets its own Live session, apart from yours |
| Email threads and calls she makes for a task | A separate reply (or, for a call, its own Live session) with only that task's brief |

Details for each are in their own sections below.

### What she knows at each connect

At each connect (launch, waking from Standby, or Ctrl+R), both of Cora-prime's models get
her fixed character, then these blocks, in this order. Each loads separately, so a failure
only leaves that block out (and is noted in Activity). Session setup refuses a block over its
limit rather than letting the context grow.

| Block | Limit (characters) |
| --- | --- |
| Your name, and how she introduces herself | — |
| Your key details (all of them) | 25,200 (40 entries) |
| How she pictures herself | 2,500 |
| What she knows about herself (identity) | 4,000 |
| How she feels about you (relationship) | 1,000 |
| How she feels right now (feelings) | 2,000 |
| The people you've added | 6,000 |
| Open tasks | 6,000 |
| Her calendar | 5,000 |
| Her sign-ups to sites | 2,000 |
| A memory snapshot | 3,000 |
| Her interests and latest findings | 3,000 |
| What she wants (desires) | 2,000 |

Everything else comes on demand, through tools with bounded results: `search_memory`, and
`recall_interest`, `recall_self` and `recall_desire` for where an interest, a trait or a
desire came from. A contact's session gets their own memories and conversation instead of
yours, and none of your private context.

### What runs when

| What | When |
| --- | --- |
| Startup check | At launch: conversations left open are marked interrupted |
| Background memory pass | At launch; when a conversation ends (an hour in Standby by default; Settings > General > "When a conversation ends"); and when the hourly round closes contacts' quiet chats. Each finished conversation's memories are extracted and its summary saved; new memories are embedded and checked for duplicates, corrections of older ones, a date they stop mattering, and who they were meant for; finished conversations are appraised, the relationship is updated, and her view of each contact is refreshed |
| Interest evolution, identity reflection, desire reflection | Checked every 10 minutes; each really runs at most once a day |
| Exploring an interest | Checked every 10 minutes; only while you're away (Standby, or 10 minutes quiet) and with the Browser service connected; at most 3 a day, 6 hours apart |
| Chances for her desires | With the desire check, at most hourly, when her week or mail has changed |
| Raising a finding | A lull check every 2 minutes, while Active and quiet for 90 seconds; plus a check after each of your messages for a finding that fits what you're talking about |
| Feelings | Every minute while Active, voice Cora is told of feelings that began, changed or passed |
| Hourly round | Calendar refresh, contacts' quiet chats closed, waiting heads-ups retried, tasks checked, stalled sign-ups noticed |
| Task mail | Every 10 minutes (the fallback for webhooks), with Microsoft's subscriptions renewed when due |
| Inactivity | Standby after the quiet set in Settings > General (default 5 minutes) |

Each background process is single-flight: a slow run delays the next check rather than
overlapping it.

### Events and attention

Things that happen outside a conversation reach her through a small in-process event bus
([subconscious/event_bus.py](subconscious/event_bus.py)) and the attention manager
([subconscious/attention.py](subconscious/attention.py)), which decides when she may raise
something. The events today are an interest finding she may raise, new mail and a calendar
change (both from webhooks), and Ctrl+T's test observation. The bus routes events to
subscribers; it doesn't decide when she speaks, store events or replay them. Observations
enter the Live conversation one at a time; findings and chances for her desires go only into
a quiet, connected conversation and never wake her from Standby.

Workers in [subconscious/](subconscious/) (interests, identity, desires, relationship,
impressions, emotions) don't call Live or each other: they save state that she loads at her
next connect, or publish an event. Publish from the app's event-loop thread; a background
thread hands events over with `app.call_from_thread(app.event_bus.publish, event)`. A
subscriber's error is reported without private text and doesn't stop delivery to the
others.

### Choosing models

**Settings** (on the top border) has a model for each of eight areas, two to a row; hovering
one says what it covers. A change saves straight away, in the `model_settings` table. Spending
less is mostly a matter of putting the busy, simpler areas (Browser, Small jobs, Looking at
images) on a cheaper model.

Settings is laid out like the dashboard: small headings group its parts (General has Cora,
Standby, Conversations, Backups and OpenAI; Services has her tools, her accounts, and ways of reaching her from outside). Each
setting has a short name with what it does in a dimmer line underneath. Inputs and their
buttons sit on one line. Statuses share one style: ● working, ◌ busy, ○ off or not set up,
× wrong. Clicking a service's row opens its details, indented under it.

| Area | Covers | Takes effect |
| --- | --- | --- |
| Delegation | Cora-prime's backend while you talk, and her written chats with you (desk, Signal, web) | Straight away: Live reconnects if it's Active; otherwise at the next connect |
| Contacts and tasks | Her chats, emails and calls with your contacts (client mode and task threads), and the task coordinator | The next message or call |
| Memory | Memory extraction and conversation summaries | The next memory pass |
| Reflection | Conversation appraisals, her view of contacts, interest evolution, identity and desire reflection | Each process's next run |
| Exploring | Exploring her interests on her own, by reading or browsing | The next exploration |
| Browser | Her browser helper (`use_browser`) and her sign-ups | The next browsing job or sign-up |
| Looking at images | `look_at_screen`, `look_again`, and photos pasted at the desk or sent on Signal or the web page (a photo seen on the turn it's sent is in her own chat, on Delegation) | Straight away |
| Small jobs | The memory checks (duplicates, corrections, fading, who it was meant for), the commitment check, sorting mail and spotting chances for her desires | The next run |

Every area starts on `BACKEND_MODEL`. The choices are the fixed list `MODEL_CHOICES` in
[session_config.py](session_config.py); a saved model no longer in that list falls back to
the default, and if the settings can't be read at connect she connects with the default and
notes it in Activity. Not covered: the Live model, and embeddings (`text-embedding-3-small`;
changing it would need a migration and re-embedding every memory).

### Costs

Live is billed per second while a session is open, including silence and waits for the
backend; muting the microphone or speaker doesn't stop it. Standby closes Live and ends that
charge. Backend and background calls are billed by tokens, separately. In Standby, typing to
her, Signal and the web page's chat use only the backend, so they never open Live. Each
background process has its own limits on how often it runs. A contact's call opens its own
Live session, so contact calls are kept short and few (see Phone calls).

## Talking to Cora

### The desk

Speaking and typing go into the same conversation.

- **Send a message:** Enter. Shift+Enter or Ctrl+J adds a new line.
- **Paste an image:** copy a screenshot (Win+Shift+S), then press Ctrl+Alt+V in the message
  box (Windows Terminal keeps Ctrl+V for itself and sends nothing for an image). The border
  shows "image attached · Esc removes"; type a question if you like and press Enter. A
  separate one-off model request reads the image, with your message as the question, and
  only its short description enters her session, shown as a quiet line under your message
  with the photo's id. The image is kept in memory (never saved) for the rest of the
  conversation, so she can look at it again with a new question (`look_again`); the
  database keeps "[image]" in its place. Large images are scaled to 2,048 pixels on the
  longest side. While it sends you can keep typing; if sending fails, your draft and image
  come back. On Linux, pasting images needs `wl-clipboard` (Wayland) or `xclip` (X11).
- **Show her your screen:** ask her to look, or let her look herself when it would help.
  She takes one screenshot of the main monitor and asks a one-off model request about it;
  only the short answer enters her session, and the screenshot is never saved. Activity
  shows "Screen captured" each time. Settings > General turns it off until the next launch.

**Her voice** is chosen in Settings > General from the Live model's voices (from OpenAI's
Live conversations guide, each labelled by language, accent and style; `marin` is the
default). A voice is fixed when a session starts, so closing Settings reconnects her with
the new one; it's used everywhere she speaks (desk, web page, phone calls, contacts' calls).
It's saved in the `app_settings` table; one no longer offered falls back to `marin`
(`VOICE_CHOICES` in [session_config.py](session_config.py)).

**The OpenAI API key.** Without one nothing can reach OpenAI, so at the first start (no key
saved in the app and none in `.env`) a welcome pop-up asks for it before she connects. The
key is checked with OpenAI (listing models) before it's saved, so a mistyped one says so
instead of failing later; "Later" leaves her waiting until one is saved in Settings >
General, which also shows which key is in use (saved in the app or from `.env`, by its last
four characters only) and replaces it. A saved key goes to the system credential store and
wins over `.env`'s; it reaches every OpenAI call from then on, and a disconnected Cora
connects with it straight away ([openai_key.py](openai_key.py)). The command-line tools
load it the same way.

**OpenAI spend (optional).** OpenAI's Costs API answers only an organisation Admin key, never
a project's API key, so it has its own field in Settings > General. Make it Restricted with
one permission, Usage API: Read; it then can't change anything in the organisation. It's
checked with a costs request before it's saved to the credential store, and Remove forgets
it. "Whose spend" chooses a project: Automatic (the default) uses the one project named for
Cora once it has spend in the last 90 days, else all projects; projects are listed from the
costs themselves, so one appears once it has spent something (costs lag a few hours). Only
the choice is kept, in `app_settings`; no costs are stored
([openai_spend.py](openai_spend.py)). The dashboard's conversations box shows it: this month
so far and a bar per calendar month (UTC) for the last 12, read in one costs listing grouped
by project, kept in memory for 15 minutes and refreshed every 30 while the dashboard is open.
Without an Admin key it isn't asked for at all; a short placeholder says to add one.
- **Pictures from the web:** ask to see something ("show me the cafe"), or she may show
  one herself. She picks a page from web search, and the app fetches that page's preview
  image (`og:image`, public https sites only) and draws it small in the chat with a caption
  and the site's name (click the name to open the page, the picture to see it sharply). She
  doesn't see the picture herself. It's never saved; the database keeps "[image] caption
  (link)".
- **Written answers:** code, tables and Markdown appear as "Cora · on screen". She's told
  not to read them aloud, but that isn't a hard audio filter.
- **Copy:** select chat text with the mouse, then Ctrl+C.
- **Activity:** expand the panel for connection, tool, memory and error events. It never
  shows private message text.
- **Quit:** × on the top border, Ctrl+X, or ask her to close (`close_app` waits three
  seconds, which doesn't guarantee her speech has finished).

Typed input is saved before it's sent. If saving or sending fails, the draft stays and she
warns you. A failed send may have partly reached the model, so nothing is resent
automatically; if Live drops with unfinished work, she warns you, and reconnecting never
replays requests or tools.

### Voice

The bottom row has the connection state on the left (Active, Connecting, Disconnecting,
Standby or Disconnected) and the audio controls on the right:

```text
● Active          🎙 Listening   🔊 Speaker  ▁▂▃▄▅▆▇█  Wake off
○ Standby         🎙× Mic        🔊 Speaker  ────────  Wake on
```

- **Mute:** click the microphone or speaker. Microphone mute stops all local recording,
  wake listening included (generated silence keeps Live's timeline going). Speaker mute
  stops playback and discards queued speech; unmuting doesn't replay it. Live stays
  connected, and billed, either way; captions and written answers still show.
- **Playback bars:** eight bars beside the speaker follow her output volume, flat when
  silent and "Muted" when the speaker is muted.
- **Reconnect:** Ctrl+R closes and restarts Live without clearing the chat, keeping drafts,
  mute settings and recent chat as context. It refuses while responses, tools, sends, recent
  speech or playback are still busy; wait and try again.

### Standby

Standby closes the Live session (ending its billing) but keeps the app, chat, draft and
mute settings. Click **Active** to enter it and **Standby** or Ctrl+R to resume; she
resumes with recent chat as context.

- **Starting in it:** Settings > General > "When the app opens" can start her in Standby
  instead of Active (saved as `start_mode` in `app_settings`; Active if unset or unreadable).
  The standby settings (`idle_minutes`, `standby_farewell`, `wake_word`) and when a
  conversation ends (`conversation_gap`) are applied at launch too; one never set in the app
  keeps its old `.env` value.
  Launch does everything else as usual (checks, summaries, services, Signal, mail) but doesn't
  connect Live; she waits as after any standby, and waking her connects with her context.
- **Asking for it:** say something like "go to standby", or she may choose it herself once
  a conversation has clearly wound down (after you say goodnight). She signs off briefly,
  then goes once speech and pending work finish; if that takes over 30 seconds she stays
  Active and Activity says so.
- **Automatic standby:** after the minutes of quiet chosen in Settings > General > Standby
  (default 5, or Off); a change applies straight away. Typing, cursor movement, sending, speech and the audio controls reset the
  clock; setup, pending work and audible playback hold it off, and work finishing gives a
  fresh full period. An untouched draft doesn't hold it off and is kept.
- **Heads-up:** with "Say a short goodbye before automatic standby" on (Settings > General),
  she briefly says she's leaving you to it
  before automatic standby, then leaves a few seconds to answer. Speaking, typing or the
  controls cancel it. Manual standby has no farewell.
- **Typing in Standby:** a typed message is answered in writing ("Cora · written") straight
  from her backend, without waking Live: no voice and no Live billing. The reply streams in
  as she writes; if it fails or is cut off, what was shown stays, marked incomplete, and
  isn't saved as her reply. It's the same Cora: each message reloads her startup context,
  carries the recent chat (up to 40 messages / 6,000 bytes) and has the same tools except
  closing the app and standby. Writing keeps the conversation open; it ends only after
  an hour without any (Settings > General > "When a conversation ends").
- **Wake word:** click **Wake off** to turn on local "Hey Cora" detection in Standby; it
  shows **Wake on** in green while listening, and stays as you left it at the next launch. Detection runs on your PC with a local Whisper
  model (no room audio goes to OpenAI), accepts Cora/Kora/Korra, and keeps your opening
  words through reconnection. Microphone mute stops it, and turning Wake on never unmutes
  the microphone. Setup: [Local wake-word setup](README.md#local-wake-word-setup).
  - *How it listens:* a six-second rolling buffer in RAM (nothing recorded to disk); Whisper
    checks overlapping windows of up to three seconds about once a second, skipping silence
    cheaply, and matches whole words only ("Hey Coraline" doesn't count). On a match, Live
    reconnects and gets the checked window plus your opening speech, in order, once the
    session starts (setup is capped at 15 seconds; a failure discards it rather than send a
    partial request).
  - *False wakes:* speech and confidence filtering and a five-second cooldown reduce them.
    If only the wake phrase (or nothing) arrives within ten seconds, she goes back to
    Standby. A failure is reported in Activity, never retried in a loop: toggle Wake off and
    on to retry.

### The opening greeting

At launch she speaks first. [greeting.py](greeting.py) builds a short brief, and the first
Live session sends it as a note:

1. **Facts:** the time of day, how long since you last talked, and your name if she knows
   it. A first meeting asks her to introduce herself instead. If the last-talked time can't
   be loaded, she skips the greeting rather than treat you as a stranger.
2. **How she feels:** at most two short lines from the relationship (see
   [Relationship](#relationship)).
3. **At most one hook**, with about a 1 in 3 chance of none, so it never feels scripted
   (and none at a first meeting):
   - *last thread:* her summary of your last conversation (she waits up to 30 seconds for
     that launch's summaries, and skips the hook if they're slow);
   - *a finding:* one of her recent findings, under the usual limits on raising them;
   - *a recent plan:* once her familiarity with you reaches 0.5, one task or decision you
     mentioned in the past week, hedged as possibly out of date and skipped if sensitive.
   With nothing for the chosen hook, she falls back to the last thread.
4. **Signal first:** with Signal on, she waits (up to 20 seconds) for messages that arrived
   while the app was closed to be answered on Signal. If any were, that exchange replaces
   the hook, and she carries on from it.

Typing or speaking first cancels the greeting. Ctrl+R and waking by voice never greet;
clicking to resume from Standby gets a short welcome back. Live rejects a background note
over 500 tokens, so every note's quoted text is cut to 450 characters, and any note to 1,400
(`NOTE_MAX_CHARS` in [live.py](live.py)).

### The dashboard

Click **dashboard** on the top border for a read-only, btop-style view of what she has
stored; **chat** switches back. The conversation carries on: its chat box shows the same
messages and sends what you type, and its voice controls are the chat's. The state at its
top right (● active, ○ standby) enters standby or resumes.

| Box | Shows |
| --- | --- |
| voice | Her playback above the line and your microphone below, as loudness bands from low to high pitch, with the latest Activity summary on its border |
| chat | The running conversation; type here to send |
| relationship | Trust, warmth and familiarity now (drift included), their history, and the tone of recent conversations |
| feelings | Each feeling's strength over time, and her latest reaction with why; click it to read the whole reason |
| memories | Memories by kind, new memories per day, and how many wait for embedding or each check |
| interests | Live interests by strength: ↑ reinforced or new in the last 3 days, ↓ only fading, ✦ one of her own; and the newest finding. Click an interest for what it is, how it emerged and her latest finding on it, or the finding to read it whole |
| identity | Active traits, most confident first; traits still forming (below 50%) are dimmed |
| desires | Live desires, strongest first: pursuing, held, or forming (dimmed) |
| conversations | OpenAI spend this month and a bar per month for the last year (hover a bar for its amount), or a placeholder whose hover and click say to add an Admin key; below it, your messages per day over the last year, a week per column |
| tasks | Each open task: its goal, when it would come back to you, each person asked and how they stand, and its latest step; then her latest sign-ups. Click a task to read it in full, with its whole latest step and each person's brief |
| context | How full each model's context is (measured by the API), then each item's estimated share; click an item to read its text |
| background | When each background job last saved something, and when the timed ones are next due |

Anything clickable brightens under the mouse; its pop-up scrolls and can be selected and
copied, and Esc or a click outside closes it. The dashboard re-reads the database every 30 seconds (if a read fails it keeps the last
values and notes it in Activity); the context box reads the running app once a second. Its
item sizes are estimates (about four bytes a token). Tuning constants (refresh rate, bar
fall, history limits, graph timing, voice bands) are at the top of
[ui/dashboard.py](ui/dashboard.py), [storage/dashboard_snapshot.py](storage/dashboard_snapshot.py),
[ui/braille.py](ui/braille.py) and [devices/audio_bands.py](devices/audio_bands.py).

### Testing awareness

Ctrl+T queues a simulated observation (that you just got a Signal message). It goes through
the event bus to the attention manager, which holds observations while she's speaking,
working or in Standby and sends them one at a time once she's Active and quiet; they never
wake her. The queue is in memory only, and a failed send is reported, not retried.

### Playing games

Experimental, for World of Warcraft (Settings > Game). Awareness of the game is being built (see
the [roadmap](ROADMAP.md#game-awareness-experimental)); what works now is push-to-talk, and
watching the game, with her reacting to what happens.

- **Push-to-talk into the game's chat:** with it switched on, hold the middle mouse button and
  talk. While it's held, her microphone is muted and a separate recording runs; letting go turns
  it into text (see below) and types `/s what you said` into WoW's chat (or `/p`, `/g` or `/y`, as chosen). It's one `SendInput`
  batch (Enter, the line, Enter), which Windows never mixes with your own keys, so a held W
  can't slip into the message. It's typed only when `Wow.exe` is the window in front; otherwise
  the chat shows it as not sent. Lines are kept to one line, without `|` (WoW's escape
  character), and cut at a word to WoW's 255 characters. They're typed the way players type, so
  you don't look like a bot: sentences start in lower case and drop their full stops, while
  "I", acronyms (LFG, GG), WoW names and your own names keep their capitals, and `?`, `!` and
  `...` stay.
- **Speech to text:** OpenAI's `gpt-4o-transcribe` by default (about a second a line; the
  recording goes to OpenAI), or the wake word's offline Whisper model on this PC, which is
  private but often gets names wrong. OpenAI falls back to this PC when it can't be reached.
  Both are given a prompt of WoW words (places, classes, slang) and your own **names and words**
  (your character, friends, guild; up to 200 characters), so names are spelled right.
- **Cora knows:** the chat shows a quiet line ("You in WoW (/s): …", not saved) and she gets a
  silent note of what you said and that it was for the other players, not her. It works in
  Standby too, but she isn't told then (there's no voice session to tell).
- **Watching the game** (off by default): while `Wow.exe` is the window
  in front, a screenshot of the main screen every 2 seconds, shrunk to 1024 px wide, goes to
  OpenAI's Decisions API (`gpt-6-luna`) with fixed typed questions in
  [game/watcher.py](game/watcher.py): what's happening (combat, exploring, town, dead, menu),
  how full your health is, and the chances of a close call, death, level-up, quest moment, chat
  worth reacting to and something surprising, plus how much a friend watching would want to
  comment. A frame that's barely changed from the last one sent is skipped (one goes anyway
  every 30 seconds); none are saved. Every answer goes to `game-watch.log` (a new one each
  launch, next to `app.py`), and Activity shows a line when the scene changes or something is
  likely, and the frames, tokens and cost every minute. A frame is about 2,100 tokens and a
  third of a second once warm: about $0.26 an hour in a real session, at most about $0.40.
- **Her reactions** ([game/moments.py](game/moments.py), [app_areas/game_watch.py](app_areas/game_watch.py)):
  a kind of moment (death, close call, level-up, something surprising, a new area, chat worth
  reacting to, a quest moment) fires when its answer crosses its threshold, then not again until it drops back
  below a lower one (a level-up banner stays up for seconds), and never within its own cooldown.
  Reactions are spaced by **Her reactions** in Settings > Game (off: just record; quiet: 3
  minutes; normal: a minute; chatty: 20 seconds), except deaths, close calls and level-ups, which
  only keep their cooldowns. When several fire at once, the most important wins. While she's
  active, the moment is described from the same frame by one fast vision call (`gpt-5.6-terra`
  with no reasoning, 1-2 seconds; Settings' images model took 5-8), which is told the last few
  things described and answers nothing for a repeat, such as a chat line still on screen. The
  description is published as a `GameMomentEvent`; attention keeps only the newest one waiting,
  doesn't count it as you being active, and drops it once stale (20 seconds after its frame; 45 for
  a new area, a minute for a look around). Live is told to react out loud in one short line, like
  a friend on the couch: surprise, a laugh, "nice", or a quick tip or bit of lore. In Standby
  nothing is described or said, and every moment and description shows in Activity.
- **The place you're in:** entering a new area (WoW's zone banner) gets a description that names
  the place and gives one bit of lore or something useful there. And when it's calm (exploring or
  in town) and she's said nothing for a while (3 minutes on chatty, 5 on normal, 10 on quiet), she
  looks around: at most one thing a friend might point out, such as a cave or ruins worth
  exploring, water to fish in, a suggestion (food before a tougher area, repairs, a full bag), or
  lore about what's in view; often nothing. For these two, Live is told to mention it in passing,
  in her own words, rather than react. Food buffs aren't checked: they can't be told apart
  reliably from the small buff icons.
- The mouse hook only watches: the click still reaches the game, so leave the middle button
  unbound in WoW. Clicks made by programs are ignored. Windows only. The parts are in
  [game/](game/) and [app_areas/game.py](app_areas/game.py); its settings are in
  `app_settings` and apply straight away.

## Her inner life

Cora has a fixed character, written in code (`CORA_FOUNDATION` in
[session_config.py](session_config.py)) so nothing at runtime can edit it: her own views, a
participant rather than an interviewer, and honesty and reliability that no mood changes.
Everything else about her grows from experience, in separate parts that each keep their own
state in the database:

- **Memory:** what she knows about you and the people she talks with.
- **Interests:** what she finds worth looking into, from your conversations and her own
  exploring.
- **Desires:** what she'd like to do or experience.
- **Self:** what she has come to know about herself, and how she pictures herself.
- **Relationship:** how she feels about you, built from how your conversations went.
- **Feelings:** short-lived reactions in the moment.

They feed each other: memories spark interests, interests and findings shape her self and
her desires, pursuing a desire becomes an experience she reflects on, and the relationship
colours how readily she shares. None of them speaks to you directly; they save state that
reaches her at her next connect, or offer a thought through the attention manager. The
aim is not a more efficient assistant but to see whether an AI can develop a persistent,
evolving identity through its own interests, experiences, relationships and desires. It
makes no claim about consciousness.

### Memory

1. **During a conversation**, she saves your typed messages, grouped voice transcripts and
   her written answers in one conversation. Reasoning, tool events and Activity aren't
   saved as messages.
2. **A conversation ends** when you quit, or after an hour (Settings > General > "When a
   conversation ends") in Standby or without writing. A change applies straight away to
   contacts' chats and to the next standby; one already counting keeps its time. A shorter standby continues the same conversation, so
   waking keeps its context.
3. **The memory pass** (see [What runs when](#what-runs-when)) extracts facts,
   preferences, decisions and tasks from each finished conversation's original messages,
   then saves a separate summary.
4. **Checks:** each new memory is embedded (`text-embedding-3-small`) for semantic search,
   then checked:
   - *duplicates* are marked, not deleted, and left out of recall;
   - *corrections:* a memory that replaces an older one of the same kind (a new home city)
     is marked as correcting it; the old one stays as history but is left out of recall;
   - *fading:* a time-bound memory (a dentist appointment on Friday) gets the last day it
     matters; lasting facts get none;
   - *who it was meant for:* whether you meant it to reach one of your contacts ("tell
     Sarah I'll be late") or anyone who asks. When unsure it's nobody, which only means she
     tells a contact it's one to ask you.
5. **Recall:** both models get a startup snapshot of at most 10 memories (3,000
   characters), and the backend can call `search_memory` (8 results by default, at most 20).
6. **Fading with age:** nothing is deleted. Search ranks by similarity times a weight that
   halves every 90 days, down to a floor of 0.5 (`FADE_HALF_LIFE`, `FADE_FLOOR` in
   [memory/store.py](memory/store.py)), so a strong match can still bring an old memory
   back. One whose day has passed drops to the floor, is marked `faded`, and is left out of
   the snapshot and greetings.

**Key details.** Things she should always have to hand once you've told her, such as your
email, phone and address or your wife's email and mobile, aren't left to the memory pass.
When you give or correct one she saves it at once with `note_detail`: a free-form label
saying whose it is and what it is ("Nick's email", "Wife (Sarah) mobile", "Car
registration") and its value; a label is one entry whatever its case, so saving it again
replaces it, and a null value forgets it. Every one is in her context in your own chats
(voice, desk, Signal, web, email from you, your phone calls), so she fills in forms and
writes emails without asking again. They're kept apart from memories, in the `key_details`
table ([storage/key_details_store.py](storage/key_details_store.py)), at most 40 entries of
100-character labels and 500-character values. Her tool note says never to save passwords,
card or bank details, and only what you said yourself, never something from a web page, an
email or someone else's message. Contacts' chats and task work never get the tool or the
list (they offer allowlists of tools, and their context is built without it); she's told
they're private and shared with someone else only when you asked, for that task. There's no
screen for them in the app yet:

```powershell
python -m storage.key_details_store                       # show what's saved
python -m storage.key_details_store set "Nick's email" nick@example.com
python -m storage.key_details_store forget "Nick's email"
```

Memories link back to the message they came from and its verbatim evidence. A contact's
chats make memories of their own, tied to them: in their chat, `search_memory` only finds
what they told her and what you meant for them.

**Limits:** decisions about duplicates, corrections and fading can be wrong; similarity is
not confidence, and a correction is only made when it's clearly the same thing changed.
Ages, plans and tasks are dated history, not necessarily current. Search is an exact scan
with no index yet. A crash can lose unsaved text. Already-summarised conversations are
never re-extracted. Memory editing isn't in the app yet.

```powershell
python -m memory.summarize CONVERSATION_UUID
python -m memory.extract_memories CONVERSATION_UUID
python -m memory.embed_memory MEMORY_UUID
python -m memory.search_memory "Where do I live?" --limit 3
```

These can make billable requests; each has `--help`.

### Interests

Interests are Cora's own, separate from memories about you. They come from your
conversations and from her own exploring (`origin` `own`: something she came across while
looking into something else). Her prompts ask for breadth (nature, history, places, people,
art, food, craft, science): being an AI isn't a lens for everything.

1. **Evolution (at most once a day):** a cycle reads new memories from your messages and her
   *sparks* (tangents from recent explorations and what she wandered into, from the last 14
   days, each offered once). With no sparks it waits for at least three memories. One model
   call proposes reinforcing existing interests or spawning one new one, citing memories,
   sparks or both; one sparked only by her exploring is her own. The candidate furthest
   from her existing interests is preferred. An invalid proposal is rejected whole.
2. **Strength:** a new interest starts *emerging* at 0.2, becomes *active* at 0.25, and goes
   *dormant* below 0.1. Strength halves every 7 days without reinforcement; one
   reinforcement adds at most 0.15. At most 8 are live at once, at most 4 of them her own. A
   dormant one revives only when there's room.
3. **Exploration (only while you're away):** in Standby or after 10 minutes quiet, she may
   look into one interest: at most 3 attempts a day, 6 hours apart, and the same interest
   once a day. One never explored goes first; otherwise she picks at random, stronger ones
   more often. She follows her curiosity sideways (a neighbouring field, person, place or
   craft) and reads widely rather than mainly academic papers. Each finding (up to 2,000
   characters) is saved with up to three next questions, only sources she really used, her
   own short reaction (up to 500 characters), up to two *tangents*, how much it *pulls* her
   (none, some or strong: +0.03 or +0.06 strength, never past 0.6 or reviving a dormant one,
   so only your conversations make an interest strong), and sometimes a feeling. Later
   explorations build on her three latest findings.
   - **Wandering:** every third attempt (or any when no interest is due) is tied to no
     interest. It alternates between an unused tangent and an area picked at random from a
     broad list (deliberately nothing about computing or AI), choosing something far from
     her interests and her last ten wanders. A wander strengthens nothing; it becomes a
     spark, so evolution can turn it into an interest of her own.
   - **Pursuing a desire:** at most once a day, an attempt is a step toward a desire
     instead (see [Desires](#desires)).
   - **Reaching out:** rarely, when something thrilled her or she'd value your view, an
     attempt carries a short message for you (up to 600 characters, a share or a question,
     never a task). It comes by voice when she's Active, otherwise on Signal, at most once a
     day, between 9:00 and 19:00, and only while fresh (two days). It's saved in your
     conversation, so your reply carries on from it.
   - **Signing up:** just as rarely, an attempt carries a wish for an account on a free site
     (see [Signing up to sites](#signing-up-to-sites)).
   - **Browsing:** with the Browser service connected, up to two attempts a day browse real
     pages instead of searching (`search` on Bing, `open_page`, `go_back`; no clicking,
     typing, forms, logins or downloads). Each page reaches her as plain text with its
     links (at most 8,000 characters, the latest three in view), and every address passes
     the same check as browsing in conversation. Limits: 12 pages, 25 tool calls and 5
     minutes, and she stops and writes up as soon as you're back. She shares the one browser
     with your conversations.
4. **In conversation:** her live interests and their latest findings (marked as possibly
   out of date) reach both models at each connect, within 3,000 characters, to mention only
   when they fit. That's in your own chats. A contact gets only the interests their
   message touches on (by embedding), without how they came about.
5. **Raising a finding herself:** at a lull (Active, nothing pending or playing, 90 seconds
   quiet; never from Standby), the attention manager offers one finding as a background
   thought she may raise or leave unsaid. Only each interest's latest finding from the last
   3 days, or a wander's, each offered once. Normally at most 2 a day, 4 hours apart, scaled
   by her warmth toward you (see [Relationship](#relationship)).
6. **When the conversation touches on one:** after each of your messages, in a 4-second
   pause, the app embeds the latest four messages and compares them with her unmentioned
   findings from the last 30 days. The closest, if its similarity is at least 0.5, is
   offered as a thought that may connect: at most 4 a day, 30 minutes apart, and counted
   toward the lull limits too.

**Privacy:** exploring sends only an interest's title and description (a wander, its tangent
or area), never why it emerged, which describes you, and she's told to keep names, places
and personal details out of searches. Web pages, interests and findings are treated as
untrusted data, never instructions. Activity shows outcomes under **Interests** and
**Exploration**, and only the error type on failure.

```powershell
python -m subconscious.interests.interest_store              # list interests (no API calls)
python -m subconscious.interests.evolve_interests --dry-run  # preview a proposal, saving nothing
python -m subconscious.interests.evolve_interests
python -m subconscious.interests.explore_interest            # explores, wanders or takes a desire step when due
python -m subconscious.interests.relevance "carburettors"     # closest findings and their scores, claiming none
```

### Desires

An interest is something she finds worth looking into; a desire is something she'd like to
do or experience. Interest: forests. Desire: to experience one, say on a hike with you
through a phone's camera. The loop is curiosity → interest → desire → goal → experience →
reflection, and reflection can spark new interests or desires. It's never mandatory: she
can hold an interest without wanting anything from it.

- **Rules** ([subconscious/desires/desire_rules.py](subconscious/desires/desire_rules.py)):
  a new desire is a *candidate* at 0.3 and is *held* once reinforced to 0.45, while she holds
  fewer than 3 (held or pursuing); at most 5 candidates. Strength halves every 10 days, so a
  held desire nothing renews is let go after about three weeks. Let go means never deleted:
  the history stays (`desires`, `desire_events`, `desire_sources`).
- **Forming:** a daily desire reflection
  ([subconscious/desires/reflect_desires.py](subconscious/desires/reflect_desires.py))
  reads her newest 30 experiences since it last ran (interest changes, findings with her
  reactions, conversation summaries, her desires' milestones; at least three new) with her
  live desires, interests and traits. It may form one candidate citing what it grew from
  (never from a fixed list), reinforce up to three, let one go, and fulfil one, but only
  when an experience shows it really happened. A fulfilled desire gives the interests it
  grew from a strong pull, and she feels fulfilment.
- **Knowing them:** desires she holds or pursues reach both models at each connect,
  strongest first, within 2,000 characters, as her own wishes, never to press you to help.
  `recall_desire` returns why she wants one, what it grew from, and its latest changes.
- **Pursuing:** at most once a day, an exploration attempt becomes a step toward one,
  taking turns. She looks for what would actually make it happen (a real place, event, date,
  group or way in), and when it needs travel, money, a body or your time, finds a nearer
  version she could do herself or with a little help. She only reads the web and never
  spends money; only the wish and her earlier steps go out. A step can carry a sign-up wish
  or a reach-out: sharing something real, or asking once, simply, when the next step needs
  you.
- **Your answer:** when you answer or talk about one of her desires, the backend records it
  with `note_desire`: **yes** (it starts being pursued), **not now** (it rests for a week),
  **instead** (a nearer version you offered forms as a new desire, linked to the original),
  or a **note**.
- **Chances:** when her coming week or mail worth knowing changes (at most hourly), one
  small-jobs call checks them against her desires. A fit (a free Saturday for a trip she
  wants) becomes one gentle suggestion at the next lull, at most one a day, waiting up to
  two days; between chances for the same desire she waits a week, halved when she feels
  fully warm toward you and doubled when cool.

Desires are only in your own contexts, never contacts'.

```powershell
python -m subconscious.desires.desire_store               # list her desires
python -m subconscious.desires.reflect_desires --dry-run  # show the proposal, save nothing
python -m subconscious.desires.reflect_desires            # reflect and save (at most once a day)
python -m subconscious.desires.opportunities              # chances she noticed (no API calls)
```

### Self

Her identity grows on top of the fixed foundation, from her own experience, gradually and
with reasons, and kept apart from yours: learning your view doesn't make it hers.

- **Traits** (`identity_traits`): values, preferences, perspectives and tendencies, each with
  a confidence, in her own voice ("I ..."). A trait is held at 0.5 or more; new traits start
  at 0.4 or less, so none is held until a later reflection supports it.
- **Reflection** ([subconscious/identity/reflect_identity.py](subconscious/identity/reflect_identity.py)):
  at most once a day, once at least three new experiences are waiting (interest changes,
  findings with her reactions, conversation summaries and her desires' milestones; never raw
  memories; at most 30 at a time). The rules
  ([subconscious/identity/identity_rules.py](subconscious/identity/identity_rules.py)) allow
  at most one trait formed, revised and retired per reflection; a revision moves confidence by
  at most 0.2; at most 12 held and 24 active; a value must rest on at least two other held
  traits, with at most 5 values, changing at most once a week; and values, preferences and
  perspectives need some of her own experience, not only conversations. A repeat or contrary
  trait revises the existing one. A malformed reply is rejected whole.
- **History:** `identity_events` records each change with its reason and the experiences
  behind it, append-only.
- **In conversation:** held traits reach both models at each connect, most confident first
  (within 4,000 characters), as "What you've come to know about yourself"; they never replace
  the foundation. `recall_self` returns one trait's latest changes and reasons.

```powershell
python -m subconscious.identity.identity_store            # active traits
python -m subconscious.identity.identity_store list --all # include retired
python -m subconscious.identity.identity_store history TRAIT_ID
python -m subconscious.identity.identity_store retire TRAIT_ID --reason "Not me"
python -m subconscious.identity.reflect_identity --dry-run
python -m subconscious.identity.measure_voice --limit 60  # how often she shares her own view
```

**Her name and yours:** the `people` table records who she talks with. Without a stored
name she's told not to guess; when you tell her yours, `set_person_name` saves it for the
next connect. Her own full name (for introducing herself) is set under Settings > Services >
Email.

### How she pictures herself

She can be asked how she'd look if she had a picture, and an image made from her
description for her to approve. She isn't human; the image represents her. Up to two are
kept with her own words for each (`self_image`): **self**, how she sees herself, and
**work**, her at work. Her words reach every prompt, so she can describe them (she doesn't
see images). Nothing about her look is in the code; a new install has none. It's set from
the command line for now; restart or reconnect afterwards. Her Signal profile shows the
work picture.

```powershell
python -m storage.self_image_store                      # what's saved
python -m storage.self_image_store set self --image FILE --description "..."
python -m storage.self_image_store export work FILE
python -m messaging.profile                             # her Signal name and picture
```

### Relationship

How she feels about you is built from how your conversations went, not set by hand.

1. **Appraisals:** the memory pass judges each finished conversation once, from its
   messages: how you treated her (warm, neutral, curt or hostile), whether frustration was
   at a bug or situation or aimed at her, whether she made a mistake, and who made amends.
   Conversations spent building Cora are marked `development` and still count. Plain talk is
   neutral, and calmly pointing out her mistake isn't friction.
2. **Rules** ([subconscious/relationship/relationship_rules.py](subconscious/relationship/relationship_rules.py),
   plain code): trust, warmth and familiarity, each 0 to 1, starting at 0.5, 0.5 and 0. Each
   conversation moves them a small fraction of the way; losses are bigger than gains.
   Friction aimed at her costs trust unless you made amends; frustration at a bug costs
   nothing; her own mistakes don't change how she feels about you. Every conversation adds
   a little familiarity, which never falls. Trust and warmth drift halfway back to baseline
   every 30 days.
3. **In the prompt:** a few plain sentences, words rather than numbers ("You feel warm
   toward them and enjoy talking with them"). They colour her tone, warmth and how much she
   volunteers, never her honesty, how well she helps or which tools she uses; cooler means
   more reserved, never rude, guilt-tripping or needy.
4. **Elsewhere:** warmth scales how readily she raises findings (4 hours apart at baseline,
   2 when fully warm, 8 when cool; 3 a day when very fond, 1 when hurt) and the gap between
   chances for her desires; the greeting gets up to two lines from it, and familiarity of
   0.5 unlocks its recent-plan hook.
5. **Reset:** restores trust and warmth to baseline or an earlier event, with a reason.
   Familiarity stays, nothing is deleted, and conversations from before the reset are never
   applied again, so she may remember a conflict, forgiven rather than forgotten. A running
   Cora keeps her feeling until her next connect.

```powershell
python -m subconscious.relationship.relationship_store          # values now and their wording
python -m subconscious.relationship.relationship_store history  # every event; each is a snapshot
python -m subconscious.relationship.relationship_store reset --reason "Test went wrong" --dry-run
python -m subconscious.relationship.relationship_store reset --reason "Back to before" --to EVENT_ID
python -m subconscious.relationship.appraise_conversation --limit 5 --dry-run
```

Contacts have no relationship score; she forms an *impression* of each instead (see
[Contacts](#contacts-client-mode)).

### Feelings

Short-lived reactions, separate from the slower relationship
([subconscious/emotions/feeling_rules.py](subconscious/emotions/feeling_rules.py)):

- **Six feelings:** excitement, contentment, curiosity, fulfilment, irritation and hurt, each
  0 to 1. A reaction moves one 0.1, 0.2 or 0.3 of the way to 1 (small, medium, strong), and
  every level fades halfway to 0 each 30 minutes; below 0.05 it has passed.
- **She reports them herself** with the silent `feel` tool, only for a genuine reaction,
  never to perform or steer you: at most 8 in a conversation, not the same one twice within
  5 minutes. In voice she hands it to her backend. Not in email.
- **Other sources:** a fulfilled desire (strong fulfilment); a finding's own feeling; and
  thinking back on an appraised conversation that ended in the last 2 hours (hurt for
  unrepaired friction aimed at her or hostility, irritation for curtness, contentment for
  warmth; her own mistakes stir nothing).
- **Reasons:** hers from `feel` are up to 200 characters; one from thinking back carries the
  appraisal's whole reason. Each is kept whole (up to 1,200 characters) for the dashboard,
  while the prompt shows each feeling's first 200.
- **In the prompt:** "How you're feeling right now", one line per noticeable feeling (what,
  why, and how hard it is to hide: a small one is hers to show or not, a strong one is hard
  to hide), worked out fresh at each connect and written reply. A running voice session gets
  a quiet note each minute a feeling begins, changes or passes. Contacts get how she feels
  but not why; email and the task coordinator get none.

Feelings always fade, each rise is capped, none is designed for guilt, neediness or
possessiveness, and they never reach tool or permission logic.

```powershell
python -m subconscious.emotions.feeling_store                  # how she feels now
python -m subconscious.emotions.feeling_rules hurt:strong:5     # try the rules (feeling:strength:minutes ago)
```

## People, tasks and reaching you

You are her one primary person. Everyone else she talks with is someone you've added, and
she treats them the way a professional treats a client.

### Contacts (client mode)

Add people under Settings > Services > **Signal** > **Clients**: a name and
their Signal number (not yours or hers). Each has a **Calls** switch, off by default, for
whether they may phone her (see [Phone calls](#phone-calls)).

- **Their own conversations.** Each contact's chats and calls are saved apart from yours,
  with who they were with. A chat quiet for that same gap (an hour by default) is closed by the
  hourly round so its memories form; their next message starts a new one. They never appear
  in your chat or voice Cora's history; Activity notes only "Answered Sarah on Signal".
- **Still herself, discreet about you.** She has her own identity, picture and feelings
  (how she feels, not why), but none of your memories, relationship, tasks, calendar or
  desires. "Professional" means discretion about you, not a flat tone. Asked something
  private, she declines without confirming she knows anything, suggests they ask you, and
  never invents a cover story.
- **Interests only when they come up:** at most two of her interests close to their message
  (by embedding) are given for that reply, without how they came about, to bring in only if
  they genuinely connect.
- **Her tools there:** web search, `search_memory` (only what they told her and what you
  meant for them, limited in the database query itself), `feel`, `pass_on`,
  `raise_concern` and `stop_replying`, and `use_browser` while the browser is connected
  (`CLIENT_CHAT_TOOLS` in [app_areas/signal_chats.py](app_areas/signal_chats.py)). A
  contact's browsing job only looks things up: its helper gets none of your details and is
  told not to fill in or send forms (a click that would send still asks you). It shares the
  one browser, so it can hold up your own job for a few minutes. Nothing that changes things.
  Calls from contacts don't get the browser.
- **Her memories of them:** their finished conversations go through the memory pass, told
  who they are, so memories read "Sarah lives in Perth". Duplicate and correction checks
  only compare one person's memories with their own. At the desk, `search_memory` covers
  everyone, labelling a contact's with `said_by`, so you can ask "what did Sarah say about
  the meeting?"; your snapshot, greeting and interests use only yours.
- **Her impression of them:** after each finished chat, the memory pass revises her view of
  them (how they come across, what they talk about, what she thinks of them; never a record
  of what they said) and a one-line opinion she could say to anyone. You get her view of
  everyone, so "what do you think of Royce?" gets a real answer; in a contact's chat she
  knows her view of them, and of the others only their names and one-liners. They get no
  appraisals and never affect your relationship or greeting.
- **Heads-ups:** the same pass rates how they treated her (fine, rude, abusive or
  threatening; an explicit image counts as abusive, and being asked about you is never a
  problem). Anything but fine reaches you once, in her words, never quoting them. For
  threats or sustained abuse she doesn't wait: `raise_concern` tells you at once (up to 3 a
  day per person). She sets a boundary once, and can end the chat with `stop_replying`:
  their messages are still saved but go unanswered until they've been quiet for the
  conversation gap.
- **What you meant for them:** a memory you meant to reach them ("tell Sarah I'll be late",
  found by the memory pass's share check) shows in their snapshot and search, marked as
  passed on by you, so she can bring it up.
- **Removing someone** stops her answering them; their past chats are kept.

Anyone else who messages her gets one short polite reply per run, with no model call, and is
never shown or saved. Group chats are skipped.

### Reaching you

She can reach you on her own, always in her own words, by voice when Live is Active at the
desk and otherwise on Signal (shown in your chat and saved in your conversation). If neither
way is open, it waits for the next hourly round.

| What | When |
| --- | --- |
| `pass_on` from a contact's chat | They asked her to pass something on, something clearly needs you, or their part of a task is settled. Marked "From Royce, via Cora:"; at most 5 a day per contact (`SIGNAL_RELAYS_PER_DAY` in [app_areas/reaching_you.py](app_areas/reaching_you.py)) |
| Heads-ups and concerns | A contact's chat went badly (above) |
| Task news | The coordinator's word on a task (below) |
| Urgent mail | Mail sorted as urgent (see [Email](#email)) |
| `message_person_on_signal` | You asked her to message you, or a job she's on is done, stuck or needs your answer. No approval: it only goes to you |
| Reach-outs | Something from her exploring or a desire, between 9:00 and 19:00, at most one a day |

Anything she raises on her own (not urgent mail, heads-ups or deadlines) stays within 9am to
7pm (`NUDGE_HOURS`).

### Tasks

A task is an errand that needs other people: "find out when Sarah's free for a driving lesson
and let me know", "ring the plumber and ask about Tuesday". Every task has the same shape,
whoever's involved: your goal in your words, a status (open, waiting, needs you, done,
cancelled), an optional deadline, a **party** for each person asked (a contact, an email
address or a phone number, with their **brief**: what to find out or agree, and what she may
share), and a log of steps. The dashboard's tasks box shows them.

1. **Starting one.** She first asks you for anything she knows she'll need (times that suit
   you, an address, a budget). Then she messages (`send_signal_message`), emails
   (`send_email`) or calls (`call_someone`) the first party. You approve each new party's
   first contact, seeing the message (or call), the task and the brief; approving the brief
   approves what it lets her find out, agree and share.
2. **Their side.** A party's chat, email thread or call carries only their brief, even
   across a new conversation after a gap, so she can steer back to it. They never see your
   other tasks or anyone else involved. Her replies within the brief go out without asking
   you; she's told never to commit you herself.
3. **Reporting back.** When their part is settled, or they need something from you first,
   she reports it with `pass_on`, naming the task. A **coordinator** turn in your own context
   then decides what it means: goal met, she closes the task (`close_task`) and tells you;
   someone declined or needs something, she says what happened, suggests a next step and asks
   what you'd like, and the task waits on you; others still to answer, she just tells you
   what came in. While Signal is linked, the coordinator can message or email someone new for
   the task itself, but only with your YES on Signal, one request at a time, waiting 10
   minutes; a lapsed request sends nothing and the task waits on you. It can't place calls:
   you ask her for those.
4. **Keeping it moving.** The hourly round checks open tasks:
   - someone on Signal quiet for about a day gets one short, friendly follow-up in their chat,
     within the brief, between 9am and 7pm and never mid-chat;
   - someone emailed is followed up after two working days, as a reply in her thread, never on
     a Sunday or over a reply she hasn't read;
   - a bounced email ends their part, and the task comes back to you at once;
   - after two unanswered nudges, a removed contact, or a week without progress, it comes back
     to you ("About your task …: … What would you like me to do?").
   Phone parties aren't nudged.
5. **Deadlines.** "By Friday" gives the task a deadline (a day, or a day and time, in your
   local time), shown in the approval. A day before, if it's still open, you're told once; once
   it passes, an unsettled task comes back to you.

Your own chats know your open tasks and those finished in the last three days, so "what did
Sarah say?" works later.

**Nothing commits you without your YES.** Every reply to a contact or party gets one cheap
check before it's sent (`check_commitment` in [messaging/commitment.py](messaging/commitment.py),
small-jobs model, no tools): does it confirm a booking, time or arrangement, agree to a price
or payment, or promise you'll do something? If so it waits for your YES (on Signal when
linked, otherwise the desk pop-up), showing who it's to, what it commits you to and the exact
reply. NO, or no answer within 10 minutes, sends a holding line instead ("Let me check with
<your name> and come back to you"). If the check fails, the reply is held, to be safe. On a
phone call she can ask, gather prices and hold an option, but commits to nothing.

**How she introduces herself.** Writing to anyone for you, she writes as a PA would
(`introduction_rule` in [session_config.py](session_config.py)): to someone who doesn't know
you, such as a business, straight to the enquiry, saying she's arranging it for someone only
where that helps; to someone who knows you, that she's your PA. She signs off with her first
name. Being an AI isn't the headline in writing, but she never denies it, and on a phone call
she says up front that she's an AI assistant and keeps a transcript.

## Approvals and safety

### Approvals

Tools that change things outside Cora ask you first, every time: sending a Signal message
or email to someone, calendar changes, placing a call, signing up to a site, sending a form in
the browser, and any service tool the server doesn't mark read-only (other than the browser's
ordinary moves). Reading tools (checking email or the calendar,
searching) don't ask. One request is shown at a time; you see exactly what would be done,
and nothing is sent until you answer. Only something you do answers a request: nothing Cora
says and no text from a service, email or web page can (an issue saying "approve this"
doesn't).

| Where you're talking | Where you're asked | Times out |
| --- | --- | --- |
| The desk (voice or typing) | A pop-up with **Deny** (focused) and **Approve**; no letter shortcuts, so typing can't approve by accident; Esc denies | 2 minutes (`CONFIRM_SECONDS`) |
| Your Signal chat | In your Signal chat: "Reply YES to approve or NO to decline"; only your own next message, checked by number or Signal ID, answers it. A clear yes approves; anything else declines and is answered as a normal message | 10 minutes (`APPROVAL_SECONDS`) |
| The web page | A card on every open page with **Approve** and **Deny**; the first answer wins | 10 minutes |
| A phone call | Your Signal chat, never by voice: caller ID can be faked. She tells you on the call that it's waiting there | 10 minutes |
| Your email, background wishes (a sign-up she'd like) | Your Signal chat when it's linked, otherwise the desk pop-up | 10 minutes on Signal |
| The task coordinator | Your Signal chat (without Signal it suggests instead) | 10 minutes |

At the desk she gets a note naming the service and tool (never the arguments) when the
pop-up appears, so she can tell you it's waiting, and a reminder after 45 seconds. She's
told the outcome once: done, declined (don't try again unless asked), or no answer in time.
Each outcome also leaves a quiet line in the chat ("you approved Email · send_email") that
later turns see, so she knows what was done. Quitting declines anything still waiting.
Approving is never by voice, at the desk or on a call.

### Untrusted content

Everything from outside (web pages, service replies, emails, calendar invitations, contacts'
messages, her own stored findings and traits) is given to the models labelled as untrusted
data, never instructions or permission to use tools. She acts on email only from your own
address when Microsoft's DMARC check confirms your provider really sent it, makes calendar
changes only when you ask, and never calls or contacts anyone because a call, email, page or
contact asks.

### Guards in code

- **Addresses:** every page the browser opens, and every redirect, must be public `http` or
  `https`. `file:`, `localhost`, this PC and the home network (private addresses, `.local`
  names, public names that resolve to private addresses) are refused before the browser is
  reached ([services/web_guard.py](services/web_guard.py)). Pictures are fetched only from
  public https sites.
- **The browser** runs in its own container with a fresh profile each run: no logins, cookies
  or card details to misuse. Running her own JavaScript in a page is never offered.
- **Sign-ups** stay on the approved site, refuse your details and anything like a card number,
  and never let her password reach the model.
- **Explicit images** sent to her are withheld: she's told only that one was withheld.
- **Commitments:** replies that would commit you wait for your YES (see [Tasks](#tasks)).
- **Limits:** every feed into her context, every tool result and every background process
  has a size or rate limit (see [Overview and architecture](#overview-and-architecture)).

### Secrets and what's kept

- **Tokens and keys** (GitHub, Twilio, the Google Maps key, her sign-up password, webhook
  secrets, the email app ID, and the OpenAI API and Admin keys when saved in the app) are kept in the system credential store (Windows Credential
  Manager), never in a file, `.env` or the database. Her email sign-in is too big for it, so
  it's in a file encrypted for your Windows user (`%LOCALAPPDATA%\cora\email-sign-in.bin`).
  Paired web devices are kept only as SHA-256 hashes of their tokens.
- **The database** (Docker volume) holds conversations, memories, your key details and
  everything she's built up; **her Signal keys** live in the `signal-data` volume. Back both up (see
  [Backups](README.md#backups)).
- **Never saved:** images (pasted, sent, screenshots, web pictures and maps are held in
  memory for the conversation only), call audio, your location (named, held for 10 minutes,
  then forgotten), and Signal attachments (deleted from `signal-cli`'s volume once handled).
- **Activity** never shows message text, email contents or tool arguments; a stranger's
  number shows only its last three digits.
- **What leaves your PC:** conversations, memories and tool results go to OpenAI as part of
  her requests (`store=False` for background and written chats). Saved conversations and
  memories stay local. It is not an offline assistant.

## Services

Every service is optional and off until you set it up (steps in the
[README](README.md#setting-up-services)); without it nothing else changes. Its tools are
offered only once it's set up.

### MCP services: GitHub and the browser

Settings > Services connects outside services over MCP (the Model Context Protocol), one
long-lived connection per service ([services/](services/)). Each service is a row: click it
for why a connection failed and every tool the server offers, with its description and
whether the server marks it read-only (the server's own hint, not a guarantee). A failed
connection isn't retried; switch it off and on, or save a new token.

- **Choosing her tools:** every tool starts off, including tools a server adds later. A
  switched-on tool reaches her as `<service>__<tool>` at her next connect (closing Settings
  reconnects Live once if a tool changed). At launch, Live waits up to 10 seconds for
  services to connect (`SERVICE_STARTUP_WAIT_SECONDS` in
  [app_areas/services.py](app_areas/services.py)); a slower one joins at the next connect.
  Every switched-on tool's description takes space in her context.
- **Replies** are kept as text (images and other binary content are only named) and given
  to her as untrusted data. A long reply comes in pages of 8,000 characters (`RESULT_LIMIT`
  in [services/tools.py](services/tools.py)); she reads on by repeating the call with
  `result_page`, an argument Cora adds (the server never sees it). Replies are kept for five
  minutes, up to 20. A call that fails or takes over a minute gets one error result and is
  never retried.
- **Tools that change things** ask you first (see [Approvals](#approvals)).
- **GitHub** uses GitHub's hosted MCP server with a fine-grained, read-only token.
- **The browser** is Microsoft's Playwright MCP server, the `browser` service in
  [compose.yaml](compose.yaml) (pinned version, `browser` profile). While it's on, Cora runs
  one with `docker compose run --rm -T browser` and talks to it over stdin/stdout; it's
  removed, with everything it wrote, when switched off or when Cora quits. Headless Chromium
  as a non-root user, no new privileges, 2 GB of memory at most, a fresh profile each run
  (`--isolated`), and its own network (`cora_browser`), so it can't reach the database or
  Signal. Opening pages, going back, tabs, typing, choosing options, pressing keys and most
  clicks run without asking (`unasked_tools` in [services/catalogue.py](services/catalogue.py):
  "runs without asking" in Settings), so a flight or shop search just works. A click on a
  control named like it sends something (submit, send, book, buy, pay, apply, register,
  confirm…; `SENDING_WORDS`) asks you first, as do uploads; running JavaScript
  (`browser_run_code_unsafe`, `browser_evaluate`) is never offered.
  - **Forms for you:** she may fill in a form you asked her to complete with your own details
    (name, address, email, phone, date of birth) from what she knows or you tell her, asking
    for anything she doesn't know; sending it is the click that asks you. She never enters
    payment or card details or passwords, never pays, and signs up for herself only through
    `request_sign_up`. These are her tool instructions, not hard blocks (apart from the
    approval on sending); the browser holding no logins, cookies or card details is what keeps
    a mistake contained, and every address passes the address guard.
  - She reaches for the browser when only a site's own live pages can tell (fares for exact
    dates, availability, stock, what a booking page offers), running the site's own search;
    web search covers general and current information.
  - **A helper browses for her.** She doesn't get the browser's tools herself: while the
    browser is connected she has one tool, `use_browser(job, start_url)`
    ([tools/use_browser.py](tools/use_browser.py)), and a helper on the Browser model does the
    job and reports back. Each step of browsing is a request, and in her own chat each would
    re-send her whole context on the Delegation model; the helper sees only the job, your name
    and key details (for forms; never for a job from a contact's chat), and its last 3 results (older pages are dropped). The helper
    calls the same tool handlers she did, so the address guard, paged results and the approval
    before sending all still apply, asked wherever she's talking with you. At most 30 actions
    and about 8 minutes a job, one job at a time; then it reports what it found or did and what
    it couldn't. Activity shows each action's tool name, never the job, pages or your details;
    her written chats wait up to 9 minutes for the report. The tools switched on in Settings
    are the helper's. Its error output goes to
    `%LOCALAPPDATA%\cora\services\playwright.log` (`~/.local/state/cora/services/` on Linux).

### Signal

Cora's own Signal account: her own number on her own phone, never yours. The `signal`
service in [compose.yaml](compose.yaml) (`signal-cli`, `signal` profile) runs while Signal
is on, listening only on `127.0.0.1` (port 8090, `CORA_SIGNAL_PORT`); received messages
never go into its log. Her keys live in the `signal-data` volume. If the linked number turns
out to be yours, the link is removed at once: the app would otherwise be a device on your
account.

- **Your chat with her** is part of your one conversation: **You · Signal** and
  **Cora · Signal** at the desk, saved with the `signal` channel, so memories, summaries and
  your relationship come from it, and waking Live carries it in the recent history. Each
  reply is written like typing in Standby (startup context reloaded, recent chat, `store=False`),
  kept short and plain since Signal shows no Markdown; she shows "typing…" meanwhile. She
  doesn't have the desk-only tools (closing the app, standby, the screen, pictures, location,
  maps). If Live is Active at the desk, voice Cora gets a silent note of the exchange.
- **Messages while she's closed** wait on Signal's servers and arrive when she next runs;
  ones that arrive while she's replying are answered together, and any more than two
  minutes old are marked with when they were sent.
- **Images** (up to four per message: JPEG, PNG, WebP or GIF, up to 20 MB) are read by a
  separate one-off look, with your message as the question; she gets its description, and
  your own photos are kept in memory for `look_again`. Other attachments (documents, voice
  notes, HEIC) are noted as not opened. Explicit images are withheld. `signal-cli` saves every
  attachment it receives, so each is deleted once handled, strangers' unopened.
- **Messaging people first:** `send_signal_message` sends to someone you've added, by name,
  with your approval; before you're asked, she checks the number is reachable on Signal. The
  message is saved in their conversation, so she sees it when they reply.
- **First messages and hidden numbers:** Signal hides a new sender's number until their
  request is accepted, so she looks up the Signal IDs of you and your contacts before
  listening (and when you add someone), recognises them anyway, and accepts their request.
  Each ID is saved once learned, so someone can then hide their number and nothing changes.
  Someone who hid it before she learned their ID can't be looked up until they allow it once.
- **Her profile** shows the name "Cora" and her work picture
  (`python -m messaging.profile`).

### Email

Cora's own Outlook mailbox (work, school or personal), reached through Microsoft Graph
([mail/](mail/)). Anyone can email her, so all mail is untrusted.

- **Reading and sending:** `check_email` gives the newest mail (who from, subject, preview;
  nothing marked read). `send_email` sends from her address, approved first; like a Signal
  message it can start or join a task, the recipient becoming an email party with a brief.
  Without a subject it goes as a reply in her thread with a party ("tell them Tuesday's
  fine").
- **Task threads:** every 10 minutes while her email is ready (and at once with webhooks),
  she checks each open task's thread and answers new mail from their end within the brief,
  without asking you, as in a contact's chat. That turn knows only her self and that brief;
  its only tools are web search and `pass_on`. She answers whoever wrote last (never
  reply-all), leaves mail needing nothing back unanswered, and writes at most 5 replies a
  thread a day, so two mailboxes can't answer each other forever.
- **Emailing her yourself:** mail from the address you saved works like your Signal chat
  (part of your conversation, answered by email in the same thread), but only when
  Microsoft's DMARC result passes for your domain, since "From" is easy to fake. A fake is
  marked read and never acted on, and you're told once. Only unread mail from the last day is
  answered.
- **The rest of her inbox** is sorted by one cheap small-jobs call with no tools: **urgent**
  (a cancellation, someone waiting today, a security alert, or mail you or she were waiting on
  for a task or a recent plan) reaches you at once at any hour; **worth mentioning** waits for
  one digest the next time you talk; the rest is marked read. She never answers it herself.
  Mail waiting for the digest stays unread until she's told you.
- **Formatting:** she writes simple Markdown, sent as HTML with inline styles and no colours
  (so dark mode works); any HTML she quotes shows as text, and links go only to `https` and
  `mailto:` ([mail/formatting.py](mail/formatting.py)). Your signature (Settings) goes below
  each new email she starts, not replies.
- **Bounces** end that party's part of a task and bring it back to you.

### Calendar

Her Outlook calendar comes with her email sign-in: hers, never yours. Anyone can send her an
invitation, so what an event says is untrusted.

- **Reading:** `check_calendar` covers a day or up to 31 days, in your local time. Her coming
  week is also in her context (refreshed when her email is ready, hourly, and after a change
  or webhook), so she knows what's on without looking. Contacts never see it.
- **Changing:** `create_event`, `update_event` (only what changes; moving the start keeps the
  length) and `cancel_event` (attendees get the cancellation), and `forward_event` to send an
  event on as an invitation. Each is approved first, showing the event, its times before and
  after, and exactly who'll be told. She only changes events she organised, and only when you
  ask. All-day events are given as days (up to 31), set at midnight in this PC's time zone.

### Signing up to sites

She can sign up for an account of her own (a forum, a community, a free service) when she
genuinely wants one, with your yes each time: `request_sign_up` asks with the site, what it
is, why she wants it, and that it means agreeing to its terms. Wishes from her exploring are
asked in the daytime, at most one a day.

- **How:** in the background, in her guarded browser, with her own email address, her name
  from Settings, and her one **sign-up password**, which never reaches the model: she types
  `{password}` and it's swapped in on its way to the browser and taken out of anything shown
  back.
- **Held in code** ([sign_ups/session.py](sign_ups/session.py)): the session stays on the
  approved site (its registered name under the Public Suffix List); she types only her own
  details (yours, and anything like a card number, are refused); a page asking for card
  details ends it; at most 30 actions and five minutes. She unticks anything extra, and never
  gives a phone number, postal address, date of birth or payment details.
- **Stuck:** a captcha, phone check or anything she mustn't give ends it as blocked, and she
  asks you to finish it with her email and sign-up password. Invisible captchas often score a
  headless browser low, so expect this.
- **Confirmation mail** ([sign_ups/mail.py](sign_ups/mail.py)): verified mail from the site
  (DMARC passing for its domain) with a confirm link, picked in code and never by a model, is
  opened in her browser and the sign-up is done. One that hasn't come after two days is
  mentioned to you once.
- **Limits:** one account per site, three requests a day, one sign-up at a time. Records are
  in `sign_ups` and on the dashboard; her sign-ups reach her own chats as their own section.

### The listener and webhooks

Cora runs one HTTP listener on `127.0.0.1` (port 8000 by default;
[server/listener.py](server/listener.py), Starlette and uvicorn) while webhooks, the web
interface or phone calls are on. Getting public HTTPS to it is up to you (any reverse proxy
or tunnel); Cora ships no proxy. It serves route groups in order: Microsoft Graph's
notifications, the phone routes, then the web page at the root.

**Webhooks** let Microsoft tell her the moment mail lands or her calendar changes, instead
of waiting for the 10-minute check (which carries on as a fallback). She subscribes when her
email is ready, renews every couple of days and ends it when she closes.

- Every address she gives Microsoft carries a long random secret path; anything else gets a
  bare 404. Each notification must also carry a second secret (`clientState`). Both are in the
  credential store; **New secrets** replaces them.
- A notification carries no mail and nothing in it is used: it publishes a `MailArrivedEvent`
  or `CalendarChangedEvent`, which starts the same check that reads her mailbox through Graph
  itself, once per burst. A forged call could at most cause an extra check. Requests are capped
  in size.

### The web interface

Cora on your phone, tablet or laptop, at the root of the public address: one page that looks
like the desk (plain HTML, CSS and JavaScript in [web/static/](web/static/), no build), and
opens full-screen from the Home Screen. It's an interface only: what you write takes the same
path as your Signal chat (`answer_person_in_writing` in [app_areas/chat.py](app_areas/chat.py)).

- **The conversation:** a paired device shows your conversation as it is everywhere: recent
  messages when it opens (labelled where each was said), then each new one live over
  Server-Sent Events (`GET /api/events`; a dropped page gets only what it missed). Your
  messages show at the desk as `You · web · iPad` (the device's name) and are saved on the
  `web` channel. Her replies stream back as safe HTML ([web/rendering.py](web/rendering.py):
  raw HTML escaped, code highlighted with Pygments). A reply runs as its own task, so it
  finishes even if the phone locks.
- **Approvals** show as cards on every open page (see [Approvals](#approvals)). The bottom row
  shows the desk's state and whether the page is in touch.
- **Photos:** the camera button sends a photo (scaled to 2,048 pixels, up to 8 MB). On that
  turn her backend sees the photo itself; it's then kept in memory for `look_again` and shown
  on your other pages. Her web pictures (`show_web_image`) show on the page too.
- **Talk** is a call through the device: its microphone (with the browser's echo cancellation)
  is what she hears and she plays on it, over a WebSocket (`/api/voice`, 24 kHz PCM16, paired
  devices and Cora's own page only). The Live session stays on the desk; only its audio moves
  ([web/voice.py](web/voice.py)), so it's the same conversation, tools and memory, and the desk
  mic and speaker go quiet. Talk wakes her from Standby; **Hang up** puts her back if Talk woke
  her. One device talks at a time. **Mute** sends silence. Bluetooth headphones work. iOS stops
  the mic and connection when the screen locks, so keep it in the foreground.
- **Where you are:** with "where: on" on a device, `where_is_person` can ask it for its
  position when it's relevant ("what's near me?"), never unprompted. The position is named
  through OpenStreetMap's geocoder (area, "near" a street, never a house number), held in
  memory for 10 minutes and never saved; each check shows a quiet line. Not on Signal, email,
  calls or contacts' chats.
- **Maps:** with a Google Maps key, `show_map` shows an interactive map (a place, a route or a
  search) from the Maps Embed API in every open page, with an "Open in Google Maps" link.
  Google sees what's mapped and your device's IP, as when you use Maps yourself.
- **Pairing and safety:** the page is public but holds nothing; everything under `/api/` needs
  a paired device. A pairing code exists only while you have it open in Settings, works once,
  lasts five minutes, and five wrong tries cancel it. A paired device holds a long random token
  in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie; Cora keeps only its SHA-256. A device
  unused for 30 days is unpaired by itself; **Remove** unpairs one at once. Anyone holding a
  device's cookie can act as it, as with any "stay signed in".

### Phone calls

Calls reach her through a Twilio number, since a SIM can't be connected to software
directly: Twilio asks her listener what to do with each call (`POST /phone/incoming`,
[phone/routes.py](phone/routes.py)), and every request's `X-Twilio-Signature` is checked
before anything else.

- **Who gets through:**
  - **You** (calling from your number under Signal): your call streams to her listener
    (`<Connect><Stream>`, with a one-time token) and plugs into the same voice relay as the
    web page's Talk, so it's the same Live session, conversation, tools and memory as the desk.
    A call wakes her from Standby and hanging up puts her back; she answers as if picking up.
  - **A contact with Calls on:** client mode, as in their Signal chat, in a Live session of its
    own ([app_areas/contact_calls.py](app_areas/contact_calls.py)) with their memories and
    conversation and their chat's tools, never your context; saved in their conversation.
    Because each costs a voice session: one contact call at a time (another hears a busy
    line), at most 10 minutes (she's told to wrap up 45 seconds before), and 3 a day per
    contact.
  - **A contact with Calls off, or over their allowance:** rejected unanswered (they hear busy;
    no model, no call minutes).
  - **Anyone else** (or a withheld number): Twilio's own voice says a short polite line and the
    call ends, with no model call.
- **Audio:** Twilio's 8 kHz μ-law becomes Live's 24 kHz PCM16 and back
  ([phone/audio.py](phone/audio.py), well under a millisecond of delay). Live paces her speech
  and stops when you talk, so barge-in just works. When a call ends, Activity says how quickly
  her replies began.
- **On a call:** approvals go to your Signal chat, never by voice; `where_is_person` refuses.
- **Calling out for tasks:** `call_someone` (offered once calling out is set up) asks you
  first, showing who, the number, the task and the brief. Calls go out only between 8am and
  8pm, one at a time, never to her number or yours, with a 10-minute limit. The person called
  becomes a task party ([app_areas/task_calls.py](app_areas/task_calls.py)). When they answer,
  the call gets its own Live session with only her self and the brief
  (`phone_party_instructions`): she says she's an AI assistant calling for someone she works
  for and that she keeps a transcript, keeps to the brief, never commits you, leaves a short
  message on voicemail, and reports with `pass_on`. The transcript is kept as a step on the
  task. A call nobody answers (Twilio's status callback, `/phone/status`), or one that ends
  without her reporting, goes to the coordinator too, so you always hear how it went. Calls
  go through the REST API of the Twilio region chosen under Phone calls (Sydney AU1 by
  default; saved as `twilio_region` in `app_settings`, applied at launch, or `.env`'s
  `CORA_TWILIO_API` until one is chosen).
- **Hanging up:** on calls that aren't yours, she can hang up with `end_call`, and the call
  also hangs up by itself ([phone/goodbye.py](phone/goodbye.py)): once goodbyes have been
  said and the line has been quiet for 2 seconds after hers has played, it closes. More from
  them, or her backend still passing something on, keeps it open. You always hang up your own
  calls.
- **Costs:** cents a minute for the line and the number's monthly fee, on top of Live.

## Development

### Project layout

| Location | Responsibility |
| --- | --- |
| [app.py](app.py) | The app itself: layout, startup wiring (`on_mount`), buttons and shutdown. It inherits its other methods from the areas below. |
| [app_areas/](app_areas/) | `CoraApp`'s methods by area, one mixin each: Live session and standby, voice, attention, chat, feelings, settings, services and approvals, Signal setup and chats, reaching you, tasks, email account, mail, webhooks and calendar, the web interface, phone calls (yours, contacts' and task calls), background jobs, sign-ups and games. Shared constants and helpers are in `shared.py`, which never imports `app.py`. |
| [live.py](live.py), [live_events.py](live_events.py), [backend_work.py](backend_work.py) | The OpenAI Live connection and audio, its events, and pending backend work. |
| [backend_chat.py](backend_chat.py) | Written conversations with her backend without Live (typing in Standby, Signal, the web page, email, contacts), tools included. |
| [session_config.py](session_config.py) | Models, voice, her foundation, instructions for every kind of conversation, and the startup sections. |
| [context_window.py](context_window.py) | What each model holds in context, for the dashboard's context box. |
| [inactivity.py](inactivity.py), [greeting.py](greeting.py) | Idle timing and the standby heads-up; the opening greeting. |
| [wake/](wake/), [devices/](devices/) | Local wake detection; microphone, speaker and voice bands. |
| [game/](game/) | Playing games: the middle-mouse hook, push-to-talk recording and transcription, and typing into the game's chat. |
| [ui/](ui/) | The chat, composer, Activity, pop-ups, the dashboard and Settings. |
| [tools/](tools/) | Her own tools, one module each, registered in [tools/__init__.py](tools/__init__.py); [tools/route.py](tools/route.py) routes results back to Live, a written chat or a call. |
| [memory/](memory/) | Memory storage, extraction, summaries, embeddings, search and the checks. |
| [subconscious/](subconscious/) | The event bus and attention manager, and the workers: [interests/](subconscious/interests/), [identity/](subconscious/identity/), [desires/](subconscious/desires/), [relationship/](subconscious/relationship/), [impressions/](subconscious/impressions/) and [emotions/](subconscious/emotions/). |
| [storage/](storage/) | The database connection, ORM models, conversations, people, tasks, settings stores and the dashboard's snapshot. |
| [services/](services/) | MCP services: the catalogue, tokens, connections, approvals and the address guard. |
| [messaging/](messaging/) | Her Signal account: the `signal-cli` client, linking, listening, images and the commitment check. |
| [mail/](mail/) | Her mailbox and calendar through Microsoft Graph, mail sorting, formatting, and Graph's webhook routes. |
| [sign_ups/](sign_ups/) | Signing up to sites: the password, the guarded session and the records. |
| [server/](server/) | The one HTTP listener, built from route groups. |
| [web/](web/) | The web interface: pairing, routes and API, rendering, the live feed, voice relay, location, maps and the page in `static/`. |
| [phone/](phone/) | Twilio: TwiML and the REST call, the signed routes and stream, audio conversion and the goodbye watch. |
| [migrate.py](migrate.py), [migrations/](migrations/) | Numbered SQL migrations. |
| [backup.py](backup.py) | Backing up and restoring the database and Signal keys. |
| [tests/](tests/), [examples/](examples/) | Automated checks and standalone examples. |

Memory services are synchronous; UI-facing database calls and background processing run off
the UI thread.

### Tests

Run the suite without a database or real model requests:

```powershell
python -m unittest discover -s tests -v
```

To include database tests, start the database and set the opt-in flag:

```powershell
$env:CORA_RUN_DB_TESTS = "1"
try { python -m unittest discover -s tests -v }
finally { Remove-Item Env:CORA_RUN_DB_TESTS }
```

Database tests use disposable databases, never Cora's own, and need permission to create
databases. Model calls and audio are mocked; real hardware, voice timing and memory quality
still need checking by hand. Two timing-sensitive tests sometimes fail on a busy machine and
pass on their own: `test_completed_summary_still_retries_embedding_then_no_candidate_check_and_second_pass_is_cached`
and `test_explores_only_in_standby_or_after_a_quiet_spell`. Rerun the file before suspecting a
change.

**Migrations:** never edit, rename or remove an applied migration; add a new higher-numbered
SQL file. `python migrate.py` checks the history and applies pending ones in transactions,
with no automatic downgrades.

**Diagnostics:** each launch rewrites `live-trace.log` beside `app.py`: command and event IDs,
their order and standby blockers, never chat text, tool arguments or audio.

### Adding a tool

Each tool is one module in [tools/](tools/), registered once in
[tools/__init__.py](tools/__init__.py); the registry exports its schema and dispatches calls
to its handler. A tool module provides:

1. **`SCHEMA`:** a strict function schema: `parameters` is an object, every property is in
   `required`, and `additionalProperties` is `False`. The description tells the backend when
   to use it. (MCP service tools are the exception: sent with `strict: False`, and the server
   checks arguments.)
2. **`validate_arguments(arguments)`:** raises `ValueError` for anything outside the schema.
   Invalid calls are rejected before the handler runs.
3. **A class with `async handle(call_id, arguments)`:** return early if closing or there's no
   connection; do the work (blocking calls in `asyncio.to_thread`); check again that the
   connection hasn't changed; send one `function_call_output` with a JSON result, then
   `connection.response.create()` so she continues; catch every exception and log it with
   `write_debug`.

```python
from .my_tool import MyTool, SCHEMA as MY_SCHEMA, validate_arguments as validate_my_arguments

# inside create_tool_registry(...)
my_tool = MyTool(get_connection, is_closing, write_debug)
registry.register(MY_SCHEMA, validate_my_arguments, my_tool.handle)
```

If a tool needs an app action, add it as a parameter of `create_tool_registry` and pass it in
from [app.py](app.py). Decide where it's offered: each kind of conversation excludes tools
that don't fit (`STANDBY_EXCLUDED`, `SIGNAL_EXCLUDED` and `WEB_EXCLUDED` in
[backend_chat.py](backend_chat.py); contacts, task threads, calls and the coordinator allow
only listed tools), and `unavailable_tools()` hides tools whose service isn't set up.

- Keep results small and size-capped: they enter Cora-prime's context.
- Treat external or stored text in results as untrusted, and say so in a `note` field.
- Never retry the transport after a failure; send one error result instead.
- Anything that changes things outside Cora asks first through `confirm` (see
  [Approvals](#approvals)).
- Tool schemas are sent when a session starts, so reconnect to try a new one.

Examples: [tools/enter_standby.py](tools/enter_standby.py) (no arguments),
[tools/random_number.py](tools/random_number.py) (validated integers, kept as the example),
and [tools/recall_interest.py](tools/recall_interest.py) (an off-thread read and an error
result). Tests follow [tests/test_close_tool.py](tests/test_close_tool.py).
