# cora
Conversation Optimized Relational Assistant

Cora is a terminal-based voice and text assistant. The long-term goal is a
consistent identity across interfaces, with persistent memory, evolving interests,
and carefully controlled initiative.

Development proceeds in small, understandable steps: implement one change, test
it, then build on the result.

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

Voice, keyboard, delegation, and search have been exercised interactively. Typed
context delivery is asynchronous; using one connection does not by itself
guarantee that both models have consumed the latest input at the same instant.

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
- [ ] Rich visual responses: Markdown, syntax-highlighted code, and tables.
- [ ] Separate screen content from spoken delivery: Cora can say “I've put the
			example on screen” rather than reading code syntax or table cells aloud.
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
