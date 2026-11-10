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
   (somewhere to explore or fish, a suggestion, lore). To check: whether the banner question is
   reliable and the look-arounds are worth hearing or too often nothing.
4. **`watch_game_for(condition, until)`:** her own yes/no questions added to the watcher's list
   (such as "fighting a spider called Githyiss?"), at most about 5, expiring, listed in
   Settings > Game; firing once, as a game event.
5. **The game as she knows it:** zone, level, target and quest in a short note in her session,
   refreshed every minute or on a scene change, so she can talk about the quest or lore unasked.
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
