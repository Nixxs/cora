// The page's building blocks: messages, notices, approvals, the status bar and the composer.
// The preview (preview.js) drives them with sample data; later the connection to Cora will.

const output = document.getElementById("chat-output");
const input = document.getElementById("chat-input");
const composer = document.getElementById("composer");
const statusButton = document.getElementById("chat-status");
const bars = document.getElementById("audio-bars");

const STATES = {
  active: "● Active",
  standby: "◐ Standby",
  connecting: "◌ Connecting",
  disconnected: "○ Disconnected",
};

function escapeText(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span.innerHTML;
}

function time(date = new Date()) {
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// Follow new messages only when already at the bottom, so reading back isn't interrupted.
function nearBottom() {
  return output.scrollHeight - output.scrollTop - output.clientHeight < 60;
}

function append(element) {
  const follow = nearBottom();
  output.append(element);
  if (follow) output.scrollTop = output.scrollHeight;
  return element;
}

/**
 * A message. who: "You" or "Cora". via: where it came from or went ("web", "Signal", "voice"…).
 * html: Cora's Markdown, already rendered and made safe by the server; text: plain text, escaped here.
 */
export function addMessage({ who, via = "", html, text = "", when = new Date() }) {
  const message = document.createElement("article");
  message.className = `message ${who === "Cora" ? "cora" : "you"}`;
  message.innerHTML = `
    <div class="label"><span class="who">${who}</span><span class="via">${via ? ` · ${escapeText(via)}` : ""}</span><span class="when">${time(when)}</span></div>
    <div class="body">${html ?? `<p>${escapeText(text).replace(/\n/g, "<br>")}</p>`}</div>`;
  return append(message);
}

/** A reply streaming in: update(text) as it arrives, then finish(html) or fail(). */
export function startWriting(via = "web") {
  const message = addMessage({ who: "Cora", via: "writing…", text: "" });
  message.classList.add("writing");
  const body = message.querySelector(".body");
  const label = message.querySelector(".via");
  return {
    update(text) {
      const follow = nearBottom();
      body.innerHTML = `<p>${escapeText(text).replace(/\n/g, "<br>")}</p>`;
      if (follow) output.scrollTop = output.scrollHeight;
    },
    finish(html) {
      message.classList.remove("writing");
      label.textContent = ` · ${via}`;
      body.innerHTML = html;
    },
    fail() {
      message.classList.remove("writing");
      message.classList.add("incomplete");
      label.textContent = ` · ${via}`;
    },
  };
}

/** A quiet line for something that happened, like "Cora went to standby". */
export function addNotice(text) {
  const notice = document.createElement("div");
  notice.className = "notice";
  notice.textContent = text;
  return append(notice);
}

/** A tool waiting for your yes; onAnswer(true|false) once tapped. */
export function addApproval({ title, detail, onAnswer }) {
  const card = document.createElement("div");
  card.className = "approval";
  card.innerHTML = `
    <div class="what">⚠ ${escapeText(title)}</div>
    <div>${escapeText(detail)}</div>
    <div class="actions">
      <button type="button" class="yes">Approve</button>
      <button type="button" class="no">Deny</button>
    </div>`;
  for (const [selector, answer] of [[".yes", true], [".no", false]]) {
    card.querySelector(selector).addEventListener("click", () => {
      card.classList.add("answered");
      card.querySelector(".what").textContent = answer ? "✓ Approved" : "✕ Denied";
      onAnswer?.(answer);
    });
  }
  return append(card);
}

/** Active, standby, connecting or disconnected, as the desk shows it. */
export function setStatus(state) {
  statusButton.className = `state-${state}`;
  statusButton.textContent = STATES[state];
}

const BAR_CHARS = " ▁▂▃▄▅▆▇█";

/** Eight audio bars from levels 0..1, or a flat line when there's no sound. */
export function setBars(levels) {
  bars.textContent = levels?.length
    ? levels.map((level) => BAR_CHARS[Math.round(Math.max(0, Math.min(1, level)) * 8)]).join("")
    : "";
}

/** Calls onSend(text) when you send; Enter sends, Shift+Enter adds a line. */
export function setupComposer(onSend) {
  const grow = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
    composer.classList.toggle("has-text", input.value.trim() !== "");
  };
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    grow();
    onSend(text);
  });
}

/**
 * iOS Safari doesn't shrink the page for the on-screen keyboard, it covers it. Size the app
 * to the visible area instead, so the composer stays just above the keyboard.
 */
export function fitToViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const fit = () => {
    const follow = nearBottom();
    document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
    // Safari scrolls the page to show the focused box; the layout already does that.
    window.scrollTo(0, 0);
    if (follow) output.scrollTop = output.scrollHeight;
  };
  viewport.addEventListener("resize", fit);
  fit();
}

export { statusButton };
