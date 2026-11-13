# Roadmap

What's next for Cora. What's built is described in [DOCUMENTATION.md](DOCUMENTATION.md);
the history of how each part was built is in Git.

## Built

- **Talking to her:** voice and typing in one conversation, standby and the local wake
  word, the opening greeting, images (pasting, the screen, pictures from the web), the
  dashboard (with OpenAI spend, given an Admin key) and a model per area (eight, including
  contacts and tasks, exploring, images, and browsing run by a helper with a small context). See [Talking to Cora](DOCUMENTATION.md#talking-to-cora).
- **Her inner life:** memory (with corrections and fading), key details she keeps to hand
  (your and your family's emails, phones and addresses), interests and exploration,
  desires, a developing self, how she pictures herself, a relationship with you, and
  short-lived feelings. See [Her inner life](DOCUMENTATION.md#her-inner-life).
- **People and tasks:** contacts in client mode (with the browser for looking things up),
  impressions and heads-ups, tasks with
  briefs, a coordinator, nudges, deadlines and the commitment check. See
  [People, tasks and reaching you](DOCUMENTATION.md#people-tasks-and-reaching-you).
- **Services:** MCP (GitHub, the browser, filling in forms for you with sending approved),
  Signal, email and calendar, signing up to sites,
  webhooks, the web interface (text, voice, photos, location and maps) and phone calls (you,
  contacts, and calls she places for tasks). See [Services](DOCUMENTATION.md#services).

## Release plan

Everything below except local models makes up **Cora 1.0**, published in this repository as
an open-source project. Cora stays run from source (PostgreSQL in Docker, the app natively);
an installer is paused. Local models follow after 1.0.

1. **Feature freeze (now, about a month):** using Cora day to day (desk, voice, Signal, the
   web page and calls) with no new features. Only fixes, polish from real use, documentation
   and the housekeeping below. Ideas that come up go into this roadmap, not the code.
   Polish in progress: none right now; new items from real use go here.
2. **A finer look for Settings:** it works but looks coarse, so it gets the dashboard's polish.
   Long labels are cut off at the edge (her voice, the API key, the spend lines), so they
   should wrap or become a short label with the detail underneath in a dimmer line. Buttons
   are full-width blocks, so they should be compact and sit beside their input (Save,
   Remove). Switches need their label beside them, statuses need one style (what's in use,
   what's wrong), and spacing should be even, with sections grouped under small headings.
   Applies to all three tabs. The Services tab's workings stay as they are.
3. **The rest of `.env` into Settings:** the settings still in `.env` move into the app, one
   at a time, as the OpenAI key and her voice already have (Settings > General, with the
   general `app_settings` table ready for more; secrets in the credential store). Done: how
   she starts, automatic standby, her goodbye before it, the wake word (the Wake button is
   remembered), when a conversation ends, the second backup folder, and the Twilio region.
   What stays in `.env` on purpose: the OpenAI key as a fallback (one saved in Settings wins),
   wake diagnostics, the email app's fallbacks, and the database ports in `.env.database`.
4. **Release.**

Waiting on devices at home: **Home Assistant** (starting with its demo devices) and
**camera and screen awareness** (see [Awareness](#awareness) below).

## Housekeeping

Nothing is broken; these keep things healthy as she grows.

- **Tool audit.** She has about 30 tools of her own, plus web search and any service tools
  switched on. She picks well today, but choices get less reliable as the list grows,
  especially between similar tools.
  - *Merge close relatives:* `recall_self`, `recall_interest` and `recall_desire` into one
    `recall`; `create_event`, `update_event`, `cancel_event` and `forward_event` into one
    `change_calendar`; `send_signal_message` and `message_person_on_signal` into one Signal
    tool. Each keeps its approval rules.
  - *Offer only what's set up,* as `show_map` and `call_someone` already are
    (`unavailable_tools`): email and calendar tools without her email signed in, Signal
    tools without Signal linked.
  - *Measure it:* a small set of test requests, each with the tool she should pick (or
    none), run against the real model before and after, so changes show whether her choices
    got better or worse.
  - About 30 tools down to about 20, with per-chat exclusions working as now.
- **Flaky tests:** two timing-sensitive tests sometimes fail on a busy machine (see
  [Tests](DOCUMENTATION.md#tests)); make them wait on events rather than time.
- **Checks still to do on real devices:** the automatic hang-up after goodbye on a real
  call; wake-word microphone accuracy and opening-speech continuity; the standby heads-up's
  timing; the playback bars' alignment with real audio; image display in the VS Code
  terminal and Kitty; image pasting on a real Linux desktop.

## Experiments (the `experiments` branch)

Built on their own branch, outside the feature freeze, in small steps that can each be tried
before the next. Each piece gets a sub-branch off `experiments`.

To do on this branch:

- [x] Claude Code 1a: hearing sessions (hooks, a listener of its own, Settings > Claude Code)
- [x] Claude Code 1b: the dashboard panel, and telling you when one finishes or needs you
- [ ] Claude Code 2: asking about them
- [ ] Claude Code 3: running sessions (Agent SDK)
- [ ] Claude Code 4: approving from anywhere
- [ ] Rocky 1: the container and Settings
- [ ] Rocky 2: test her first, with a stand-in Rocky
- [ ] Rocky 3: Rocky as her consultant and friend
- [ ] Rocky 4: a direct line (if the test shows it's needed)

### Claude Code

Cora as a way in to coding and AI projects run in Claude Code. There's no official MCP
server for controlling Claude Code (`claude mcp serve` only offers its file and shell tools),
so this uses two official pieces: **hooks**, which send each session's events to a web
address, and the **Claude Agent SDK** (Python), which runs Claude Code from inside Cora.
Both run on your Claude subscription, never a per-token API key: no `ANTHROPIC_API_KEY` in
Cora's environment (it would take over) and never bare mode (it ignores the subscription).

1. **Watching sessions**, in two halves:
   - *1a, hearing them.* *Built* (see [Claude Code](DOCUMENTATION.md#claude-code)): background
     command hooks in your Claude Code user settings post session start and end, prompts,
     finished, failed and "needs you" to a listener of its own on 127.0.0.1:8001, with a token.
     It's never the public listener, which Caddy forwards to the internet. HTTP hooks were
     passed over: they don't run on SessionStart, and they'd fail visibly whenever Cora is
     closed. Settings > Claude Code adds and removes the hooks and lists the sessions, and
     Activity gets a line for each change. Checked with real VS Code and headless sessions.
   - *1b, telling you.* *Built*: a claude code box on the dashboard (between voice and chat,
     while listening), and Cora letting you know (voice, or Signal when you're away) about a
     session left needing you, finished or failed for 3 minutes, chosen in Settings. A
     `PostToolUse` hook tells her you've answered, so she doesn't chase what you already
     approved. Permission requests (the `PermissionRequest` hook) wait for step 4.
2. **Asking about them:** a tool for listing recent sessions by project and saying what's
   going on in one ("what's Claude doing in cora?").
3. **Running sessions:** an Agent SDK service, following the MCP services' pattern (a
   switch in Settings, one long-lived task, status in the activity log). It starts or
   resumes a session in a project folder, streams progress to the panel and can interrupt.
   Resuming a session that's also open in VS Code forks it, so the two don't write into
   one transcript.
4. **Approving from anywhere:** Claude Code's permission requests come through the Agent
   SDK's permission callback (and the permission-request hook for VS Code sessions) to
   Cora's existing approval prompts, so you can approve or refuse from Signal, the web
   page or the desk.

### Rocky, her consultant

**Rocky** is an uncensored open-weights model running locally, and Cora treats him as her
consultant and her friend. He's a bit slow and very friendly, and he knows all about the
seedier side of life. A small local model won't be as sharp as her, and his persona owns
that: she goes to him for the frank answer, not the clever one, and she's fond of him. She
asks him when you want an answer her main model won't give. He's off most of the time and
only runs when needed. **Ollama** runs in Docker, like
the browser and Signal: a `local-model` profile in [compose.yaml](compose.yaml) with the GPU
passed through and a volume for the downloaded models, so `docker compose up` leaves it
alone. It has an OpenAI-style endpoint, so Cora's existing client works with a different
address. The desk's RTX 5070 (12 GB) runs models up to about 14B parameters entirely on the
GPU, and larger ones more slowly with part of the model in system memory.

1. **The container and Settings:** the compose service, plus a Local model section in
   Settings with which model to use (for example an "abliterated" or uncensored community
   version of an open model), how long it stays up after use, a download button and a test
   button. Cora starts the container when she needs it and stops it after the idle time, the
   way she starts and stops Signal's.
2. **Test her first, before building the rest:** a stand-in consultant that returns fixed,
   frank answers, run against a set of real questions (frank medical, legal, drug-safety and
   adult questions, dark humour, and a few that are clearly over the line). It checks two
   things: whether she passes the question on in full or refuses or waters it down, and
   whether she relays the answer or softens it. The result decides how much of step 4 is
   needed.
3. **A consultant and friend, not a tool:** Rocky's persona is his system prompt, and her
   notes describe him as her consultant and friend, so she talks with him the way she would
   with a friend: a `consult` tool keeps a thread going, so she can follow up, push back
   or ask him to explain, and a new topic starts a new thread. He's offered only while
   switched on (like `show_map`). She goes to him when you ask, or when she'd otherwise
   refuse. The thread is shown word for word at the desk and on the web page as it happens,
   and her tool description says you see his answers yourself, so she gives her own view
   rather than retelling (or softening) his.
4. **A direct line, if the test shows it's needed:** typing "@rocky …" at the desk or on
   the web page sends your words straight to him, and his answer is shown to
   you, so neither the question nor the answer reaches OpenAI. Talking to him by voice
   without OpenAI would need local speech (faster-whisper, already used for the wake word,
   plus a local voice), which belongs with [Local models, after 1.0](#local-models-after-10).

## Ideas for after the freeze

- **Images:** seeing the photo itself on the turn it's sent works on the web page only;
  Signal photos and desk pastes still answer that turn from the description (they're kept
  for `look_again`). Doing the same for them is small. Saving photos on the PC (so she
  could look again days later) would be a separate opt-in feature with its own retention
  and deletion; for now the image itself is never saved.
- **A Google Maps MCP server:** map data as tools (real travel times, step-by-step
  directions, places with hours and ratings) that would work everywhere, voice and Signal
  included. It needs the paid Places and Routes APIs (with a monthly free allowance) and can
  use the same Google Cloud project as the page's maps.
- **Stronger web sign-in,** strongest first: Face ID or Touch ID (a passkey) to approve tools
  from the page, so a stolen cookie can't approve anything; an alert at the desk when a
  device's cookie is used from a new network or browser; cookies that change now and then,
  so a stolen copy and the real device clash.
- **Web page:** push notifications; a PC layout told apart by its mouse
  (`(hover: hover) and (pointer: fine)`) if one is ever needed. Calls with the phone locked
  aren't possible as a web app (only a native app using CallKit could); calling her number
  does the same.
- **Phone calls:** tuning turn detection on calls if real use shows it's needed.

### Awareness

Background producers watching the screen, camera and home sensors (such as the front door
camera or whether the garage is open), each feeding a local model that decides whether
anything interesting is happening; only worthwhile events go to the attention manager, the
same path as findings and webhook awareness. Local models keep raw images and video on the
machine; only short descriptions reach Cora-prime.

- **Home Assistant** is a strong candidate for most of it: its MCP server gives her tools to
  check and control the home when asked, and its automations can call her webhook listener
  so changes (the garage opening) become awareness events, like mail. Its WebSocket API is
  the alternative if webhooks prove too limited. After she's been closed, she reads current
  states (and history for short events such as a doorbell) to catch up. You choose which
  devices she watches in the services screen.
- **Camera** detection (a person at the front door) can run locally in Home Assistant and
  arrive as simple states, so she needs no vision model of her own. Camera awareness starts
  with presence rather than identity, with its own privacy controls; any presence-based
  sleep or wake is opt-in, with delays and cooldowns.
- **Screen** awareness needs her own local producer.
- Security-sensitive actions such as unlocking doors or opening the garage always need
  confirmation. Camera and screen awareness, and the on-demand screen tool, each get an
  on/off control like the microphone, off by default, with a visible indicator while active.

### Game awareness (experimental)

On the `readme_update` branch, for World of Warcraft (WoW Forever) first: Cora aware of the game
as you play, reacting to close calls, deaths, level-ups and chat, and talking about quests and
lore. One small step at a time, each runnable:

1. **Push-to-talk into the game's chat.** *Built* (see
   [Playing games](DOCUMENTATION.md#playing-games)); to check in the game: that WoW takes the
   typed text, and that a held W keeps you running. If WoW ignores the typed characters, paste
   the line from the clipboard instead (Enter, Ctrl+V, Enter in one batch, restoring the
   clipboard). The local `base.en` model proved poor at game names, so OpenAI's transcription
   with a vocabulary prompt is the default; if the first word gets clipped, keep a short
   buffer of audio from before the press.
2. **Screen watcher, logging only.** *Built* (see
   [Playing games](DOCUMENTATION.md#playing-games)); to do in the game: play a session with it on,
   then read `game-watch.log` against what happened to choose thresholds, see which questions
   are reliable (health from small unit frames may not be), and trim question wording, which is
   about half of each frame's 2,100 tokens. A downscaled screenshot every ~2 s (skipping unchanged
   frames, slower out of combat, paused when WoW isn't in front or she's in Standby) to
   OpenAI's Decisions API (`gpt-6-luna`, $0.10 per 1M input tokens, output free) with a fixed
   set of typed questions: scene (a choice), health (a score), close call, death, level-up, new
   quest, chat worth reacting to, something surprising (predicates), worth mentioning (a score).
   Answers, tokens and cost per frame go to Activity, to tune thresholds and see the real cost
   (estimated $0.20–0.70 an hour).
3. **Reactions.** *Built* (see [Playing games](DOCUMENTATION.md#playing-games)). The first
   session's log (13 minutes, 267 frames) set it up: a level-up stayed high for two minutes and
   chat for as long as it was on screen, so each kind fires once and re-arms only after dropping
   back, with its own cooldown, and descriptions skip repeats; "worth mentioning" never passed
   1.64 of 3 and isn't used; close calls and deaths never came up, so they're untested in play.
   To check in the game: whether her reactions feel timely and natural, how often is right, and
   whether close calls fire. Then added: **the place you're in**, a new-area trigger (the zone
   banner) for lore, and a look around when it's calm and quiet for something to point out
   (something striking or funny, lore). Tried in play: she gave directions too often, so every
   game note now tells her the game is new to her, discovered alongside you, and that she's
   company having fun, not a guide (no tips or directions; lookups only when asked, shared as
   news to her too); to check in play. Chat was the worst: the busy General channel kept the chat
   check high, the description quoted lines nobody wrote, and she relayed them ("someone said...").
   Now chat lines are copied word for word, numbered public channels are ignored, only lines new
   since an earlier look are passed on, and she answers them rather than relaying; to check in
   play. Still to see whether close calls and deaths fire when they happen. `/chatlog` (item 6)
   would read chat exactly, with no vision call at all.
4. **`watch_game_for(condition, until)`:** her own yes/no questions added to the watcher's list
   (such as "fighting a spider called Githyiss?"), at most about 5, expiring, listed in
   Settings > Game; firing once, as a game event.
5. **The game as she knows it.** *Built* (see [Playing games](DOCUMENTATION.md#playing-games)):
   name, level, zone, target and tracked quests in a short silent note in her session, read about
   once a minute (sooner after a new area, level-up or quest moment rather than every scene change,
   since combat and exploring flip constantly while questing) and sent only when it changes. To
   check in the game: whether the minimap label and quest tracker read reliably at 1024 px wide,
   whether notes come too often while questing, and whether she uses them naturally ("this is cozy").
6. **Log files:** WoW Forever writes `WoWCombatLog-*.txt` with `/combatlog` (in
   `_forever_/Logs`); still to check how quickly it reaches the disk, and whether `/chatlog`
   and `QuestCache.log` (quest ids) help. Forever's addons can't read the combat log or health
   in combat, so no addon.

Later, a **game profile** per game (its process, questions, screen regions, chat command and
feeds) would let the same parts work for other games; typing into games with kernel
anti-cheat stays off. Cora never sends input to a game on her own.

### Installable Cora (paused)

An installer for non-technical users isn't planned for now. If it returns: keep PostgreSQL
rather than moving to SQLite, with a private bundled PostgreSQL plus pgvector. `pgserver` was
ruled out (unmaintained since 2024, PostgreSQL 16, Python 3.12 at most); portable binaries
would need pgvector built per platform. Running the whole app in a container was ruled out,
since Docker Desktop can't reach the host's microphone, speaker or clipboard.

### Local models, after 1.0

Running Cora-prime on local models only. There may be no local equivalent of the Live
speech-to-speech model yet; the likely alternative is local speech recognition, a local
language model and local text-to-speech, at some cost to latency and natural turn-taking. A
local replacement for the backend model exists, but web search would need a separate
provider (a self-hosted search engine or a search API, possibly through MCP). Local models
also help the awareness items above.

## Long-term direction

The aim isn't a more efficient assistant but to see whether an AI can develop a persistent,
evolving identity through its own interests, experiences, relationships and desires: build
the systems that let her personality and preferences grow through experience, rather than
programming who she should be. It makes no claim about consciousness; it's an experiment in
autonomous behaviour.

- One identity and memory across every way of reaching her: the desk, voice, Signal, email,
  the web and the phone.
- Camera support, permission-bounded local tools, and more outside integrations.
- More initiative of her own, always with explicit budgets, permissions, quiet hours and an
  off switch.
