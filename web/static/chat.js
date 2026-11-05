// Talking to Cora: the conversation as it is everywhere (desk, voice, Signal, email, here), live,
// and messages sent from here with her reply streaming back as she writes it.

import {
  addApproval, addMessage, addNotice, addPicture, attachPhoto, clearChat, setLink, setStatus, setupComposer,
  startWriting,
} from "./ui.js";
import { hasPhoto, setupPhotos, takePhoto } from "./photo.js";

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
  events.addEventListener("approval_done", (event) => {
    const done = JSON.parse(event.data);
    approvals.get(done.id)?.settle(done.outcome);
  });
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

/** Her reply arrives one JSON object a line: pieces as she writes, then the finished reply. */
async function readReply(response, draft) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let written = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    const lines = buffered.split("\n");
    buffered = lines.pop();
    for (const text of lines) {
      if (!text.trim()) continue;
      const update = JSON.parse(text);
      if (update.type === "delta") {
        written += update.text;
        draft.update(written);
      } else if (update.type === "done") {
        draft.finish(update.html);
        return;
      } else if (update.type === "error") {
        draft.fail();
        addNotice(update.message);
        return;
      }
    }
  }
  // The stream ended without a finished reply: the connection dropped part way.
  draft.fail();
  addNotice(DROPPED);
}

function newRef() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function send(text) {
  const ref = newRef();
  sentHere.add(ref);
  // A photo chosen with the camera button goes with this message; she sees it herself.
  const photo = await takePhoto();
  if (!text && !photo) return;
  markDay(new Date());
  addMessage({ who: "You", via: thisDevice ? `web · ${thisDevice}` : "web", text, photo: photo?.url });
  const draft = startWriting("web");
  let response;
  try {
    response = await fetch("/api/messages", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(photo ? { text, ref, image: photo.base64 } : { text, ref }),
    });
  } catch {
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
    const body = await response.json().catch(() => ({}));
    draft.fail();
    addNotice(body.error || UNREACHABLE);
    return;
  }
  try {
    await readReply(response, draft);
  } catch {
    draft.fail();
    addNotice(DROPPED);
  }
}

export function startChat(deviceName) {
  setupComposer(send, hasPhoto);
  setupPhotos(addNotice);
  follow(deviceName);
}
