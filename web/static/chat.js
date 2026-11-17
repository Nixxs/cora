// Talking to Cora: the conversation as it is everywhere (desk, voice, Signal, email, here), live,
// and messages sent from here with her reply streaming back as she writes it.

import {
  addApproval, addMap, addMessage, addNotice, addPicture, addRocky, askingRocky, attachPhoto, clearChat, setLink,
  setStatus, setupComposer, startWriting,
} from "./ui.js";
import { hasPhoto, setupPhotos, takePhoto } from "./photo.js";
import { answerLocate, setupLocation } from "./location.js";

const UNREACHABLE = "Couldn't reach Cora. Is her PC on and the web interface switched on?";
const DROPPED = "The connection dropped before her reply finished. It's still shown at the desk.";

// Where each message was said, as the page labels it.
const PLACES = { voice: "voice", keyboard: "desk", screen: "desk", signal: "Signal", email: "email", web: "web" };

// This page's own messages, already shown as they were sent: the feed's copies are skipped.
const sentHere = new Set();
let lastDay = null;
// This page's own device, for labelling what's sent from here.
let thisDevice = null;

function dayLabel(date) {
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "today";
  if (date.toDateString() === yesterday.toDateString()) return "yesterday";
  return date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

/** A quiet line whenever the day changes, so history reads like the TUI's. */
function markDay(date) {
  const day = date.toDateString();
  if (day !== lastDay) {
    lastDay = day;
    addNotice(dayLabel(date));
  }
}

function showFeedMessage(message) {
  if (message.ref && sentHere.has(message.ref)) return;
  const when = new Date(message.at);
  markDay(when);
  addMessage({
    who: message.role === "assistant" ? "Cora" : "You",
    // A web message says which device it came from ("web · iPad").
    via: [PLACES[message.channel] || message.channel, message.role === "user" ? message.device : null]
      .filter(Boolean).join(" · "),
    html: message.html,
    text: message.text ?? "",
    when,
  });
}

/** The live conversation. Browsers reconnect by themselves and say what they last saw. */
function follow(deviceName) {
  thisDevice = deviceName;
  setLink("connecting");
  const events = new EventSource("/api/events", { withCredentials: true });
  events.addEventListener("open", () => setLink("linked", deviceName));
  events.addEventListener("reset", () => {
    clearChat();
    approvals.clear();
    lastDay = null;
  });
  events.addEventListener("message", (event) => showFeedMessage(JSON.parse(event.data)));
  events.addEventListener("approval", (event) => showApproval(JSON.parse(event.data)));
  // A photo the person sent from somewhere else, under the message it came with; and a picture
  // she showed. Both are fetched from Cora's memory while she still keeps them.
  events.addEventListener("photo", (event) => {
    const photo = JSON.parse(event.data);
    if (photo.ref && sentHere.has(photo.ref)) return;
    attachPhoto(`/api/photos/${encodeURIComponent(photo.id)}`);
  });
  events.addEventListener("picture", (event) => {
    const picture = JSON.parse(event.data);
    markDay(new Date(picture.at));
    addPicture({
      src: `/api/photos/${encodeURIComponent(picture.id)}`, caption: picture.caption, url: picture.url,
      when: new Date(picture.at),
    });
  });
  // A map she showed: Google's map in a frame, with a link to Google Maps.
  events.addEventListener("map", (event) => {
    const map = JSON.parse(event.data);
    markDay(new Date(map.at));
    addMap({ caption: map.caption, embed: map.embed, link: map.link, when: new Date(map.at) });
  });
  // Rocky's answer, word for word: to her, wherever she asked him (above her reply), or to you
  // directly (@rocky) from the desk or another page. This page's own are already shown.
  events.addEventListener("rocky", (event) => {
    const rocky = JSON.parse(event.data);
    if (rocky.ref && sentHere.has(rocky.ref)) return;
    markDay(new Date(rocky.at));
    const via = rocky.asker === "you" ? (rocky.ref ? ["web", rocky.device].filter(Boolean).join(" · ") : "desk") : "";
    addRocky({ asker: rocky.asker, question: rocky.question, html: rocky.html, via, when: new Date(rocky.at) });
  });
  events.addEventListener("approval_done", (event) => {
    const done = JSON.parse(event.data);
    approvals.get(done.id)?.settle(done.outcome);
  });
  // A quiet line from Cora's PC, such as each time she checks where you are.
  events.addEventListener("notice", (event) => {
    const notice = JSON.parse(event.data);
    markDay(new Date(notice.at));
    addNotice(notice.text);
  });
  // Cora asks where you are: answered only with "where" on.
  events.addEventListener("locate", (event) => answerLocate(JSON.parse(event.data).id));
  events.addEventListener("status", (event) => setStatus(JSON.parse(event.data).state));
  events.addEventListener("error", async () => {
    // The browser retries by itself while it can; closed means it gave up.
    setLink(events.readyState === EventSource.CLOSED ? "offline" : "reconnecting");
    // Unpaired in Settings: back to the pairing screen.
    const response = await fetch("/api/session", { credentials: "same-origin", cache: "no-store" }).catch(() => null);
    if (response && response.status === 401) location.reload();
  });
}

// Requests for approval shown here, by id, so their outcome can be shown when it arrives.
const approvals = new Map();

async function answerApproval(id, approved) {
  try {
    const response = await fetch("/api/approvals", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, approved }),
    });
    if (response.ok) return null;
    const body = await response.json().catch(() => ({}));
    return body.error || "That didn't go through. Try again.";
  } catch {
    return UNREACHABLE;
  }
}

function showApproval(request) {
  const asked = new Date(request.at);
  markDay(asked);
  const until = request.seconds ? new Date(asked.getTime() + request.seconds * 1000) : null;
  approvals.set(request.id, addApproval({
    title: request.title,
    detail: request.detail,
    until,
    onAnswer: (approved) => answerApproval(request.id, approved),
  }));
}

/** Her reply arrives one JSON object a line: pieces as she writes, then the finished reply. In a
 * group chat Rocky's turn comes first the same way (rocky_* pieces, into rocky's bubble). */
async function readReply(response, draft, rocky = null) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let written = "";
  let rockyWritten = "";
  let rockyDone = rocky === null;
  const endRocky = () => {
    if (!rockyDone) rocky.fail();
    rockyDone = true;
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    const lines = buffered.split("\n");
    buffered = lines.pop();
    for (const text of lines) {
      if (!text.trim()) continue;
      const update = JSON.parse(text);
      if (rocky && update.type.startsWith("rocky_")) {
        if (update.type === "rocky_delta") {
          rockyWritten += update.text;
          rocky.update(rockyWritten);
        } else if (update.type === "rocky_status") {
          rockyWritten = "";
          rocky.status(update.text);
        } else if (update.type === "rocky_done") {
          rocky.finish(update.html);
          rockyDone = true;
        } else if (update.type === "rocky_none") {
          rockyDone = true;
          rocky.fail(update.message);
        }
        continue;
      }
      if (update.type === "delta") {
        endRocky();
        written += update.text;
        draft.update(written);
      } else if (update.type === "status") {
        // What Rocky is doing on the web; what he writes after starts afresh.
        written = "";
        draft.status?.(update.text);
      } else if (update.type === "done") {
        endRocky();
        draft.finish(update.html);
        return;
      } else if (update.type === "error") {
        endRocky();
        draft.fail();
        addNotice(update.message);
        return;
      }
    }
  }
  // The stream ended without a finished reply: the connection dropped part way.
  endRocky();
  draft.fail();
  addNotice(DROPPED);
}

function newRef() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// "@rocky …": to Rocky alone, never to Cora (as the desk's chat_submission and Cora's PC check too).
const DIRECT_ROCKY = /^@rocky\b[\s:,]*/i;

// Who your messages go to from this page: Cora, Rocky alone (as if each began "@rocky"), or both
// as a group chat (Rocky answers first, then Cora, who sees his answer). Each device keeps its
// own, in this browser only; Cora unless changed.
const MODES = ["cora", "rocky", "group"];
const MODE_NAMES = { cora: "Cora", rocky: "Rocky", group: "Group" };
const MODE_PLACEHOLDERS = {
  cora: "Type to Cora…", rocky: "Type to Rocky (private)…", group: "Type to Cora and Rocky…",
};
const MODE_LABELS = {
  cora: "Messages go to Cora: tap for Rocky",
  rocky: "Messages go to Rocky alone, Cora doesn't see them: tap for a group chat",
  group: "A group chat: Rocky answers first, then Cora, who sees his answer: tap for Cora",
};
const MODE_KEY = "cora-chat-mode";
let chatMode = "cora";

function showMode() {
  const button = document.getElementById("mode-button");
  button.textContent = `${MODE_NAMES[chatMode]} ›`;
  button.className = `mode-${chatMode}`;
  button.setAttribute("aria-label", MODE_LABELS[chatMode]);
  document.getElementById("chat-input").placeholder = MODE_PLACEHOLDERS[chatMode];
}

/** The mode chip, shown only when there's somebody besides Cora to talk to. */
function setupMode(rockyOffered) {
  if (!rockyOffered) {
    // No model for Rocky yet: Cora only (what was chosen before is kept for when he's back).
    document.getElementById("mode-button").hidden = true;
    return;
  }
  try {
    const saved = localStorage.getItem(MODE_KEY);
    if (MODES.includes(saved)) chatMode = saved;
  } catch {
    // No storage here (a private window): Cora each time.
  }
  showMode();
  document.getElementById("mode-button").addEventListener("click", () => {
    chatMode = MODES[(MODES.indexOf(chatMode) + 1) % MODES.length];
    try {
      localStorage.setItem(MODE_KEY, chatMode);
    } catch {
      // Still switched for now; just not remembered.
    }
    showMode();
  });
}

/** Your question straight to Rocky: shown here as you asked, then his answer (or why not). */
async function askRocky(question, ref) {
  if (hasPhoto()) addNotice("Rocky can't see photos: yours is still attached for your next message to Cora.");
  markDay(new Date());
  const pending = askingRocky(question, thisDevice ? `web · ${thisDevice}` : "web");
  let response;
  try {
    response = await fetch("/api/rocky", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, ref }),
    });
  } catch {
    pending.fail(UNREACHABLE);
    return;
  }
  if (response.status === 401) {
    location.reload();
    return;
  }
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    pending.fail(body.error || "Rocky couldn't answer.");
    return;
  }
  try {
    // Streamed as he writes, the same way as her replies.
    await readReply(response, pending);
  } catch {
    pending.fail(DROPPED);
  }
}

async function send(text) {
  const ref = newRef();
  sentHere.add(ref);
  const direct = DIRECT_ROCKY.exec(text);
  if (direct || chatMode === "rocky") {
    const question = direct ? text.slice(direct[0].length).trim() : text;
    if (!question && hasPhoto()) {
      addNotice("Rocky can't see photos: type a question for him. Your photo is still attached.");
      return;
    }
    await askRocky(question, ref);
    return;
  }
  // A photo chosen with the camera button goes with this message; she sees it herself.
  const photo = await takePhoto();
  if (!text && !photo) return;
  markDay(new Date());
  addMessage({ who: "You", via: thisDevice ? `web · ${thisDevice}` : "web", text, photo: photo?.url });
  // A group chat: Rocky's turn first, under your message, then hers. He can't see photos, so a
  // message with one is hers alone.
  const group = chatMode === "group" && !photo && Boolean(text);
  const rocky = group ? askingRocky(null) : null;
  const draft = startWriting("web");
  const body = photo ? { text, ref, image: photo.base64 } : { text, ref };
  if (group) body.group = true;
  let response;
  try {
    response = await fetch("/api/messages", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    rocky?.fail();
    draft.fail();
    addNotice(UNREACHABLE);
    return;
  }
  if (response.status === 401) {
    // Unpaired in Settings: back to the pairing screen.
    location.reload();
    return;
  }
  if (!response.ok || !response.body) {
    const answer = await response.json().catch(() => ({}));
    rocky?.fail();
    draft.fail();
    addNotice(answer.error || UNREACHABLE);
    return;
  }
  try {
    await readReply(response, draft, rocky);
  } catch {
    rocky?.fail();
    draft.fail();
    addNotice(DROPPED);
  }
}

export function startChat(deviceName, rockyOffered = false) {
  setupMode(rockyOffered);
  setupComposer(send, hasPhoto);
  setupPhotos(addNotice);
  setupLocation(addNotice);
  follow(deviceName);
}
