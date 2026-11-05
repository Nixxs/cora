// The page's building blocks: messages, notices, approvals, the status bar and the composer.
// The preview (preview.js) drives them with sample data; later the connection to Cora will.

const output = document.getElementById("chat-output");
const input = document.getElementById("chat-input");
const composer = document.getElementById("composer");
const statusButton = document.getElementById("chat-status");
const bars = document.getElementById("audio-bars");

// The desk's Live states, labelled as the TUI labels them.
const STATES = {
  active: "● Active",
  standby: "○ Standby",
  connecting: "◌ Connecting…",
  disconnecting: "◌ Disconnecting…",
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

// Whether the chat is following the latest messages: true while you're at the bottom, false once
// you scroll back to read. Growth doesn't scroll by itself, so this is only set by scrolling.
let following = true;
output.addEventListener("scroll", () => { following = nearBottom(); }, { passive: true });
// A message that grows after it's shown (a map or picture filled in, or loading) keeps the
// bottom in view while following.
const growing = typeof ResizeObserver === "function"
  ? new ResizeObserver(() => { if (following) output.scrollTop = output.scrollHeight; })
  : null;

function append(element) {
  const follow = nearBottom();
  output.append(element);
  growing?.observe(element);
  if (follow) output.scrollTop = output.scrollHeight;
  return element;
}

/**
 * A message. who: "You" or "Cora". via: where it came from or went ("web", "Signal", "voice"…).
 * html: Cora's Markdown, already rendered and made safe by the server; text: plain text, escaped here.
 */
export function addMessage({ who, via = "", html, text = "", when = new Date(), photo = null }) {
  const message = document.createElement("article");
  message.className = `message ${who === "Cora" ? "cora" : "you"}`;
  message.innerHTML = `
    <div class="label"><span class="who">${who}</span><span class="via">${via ? ` · ${escapeText(via)}` : ""}</span><span class="when">${time(when)}</span></div>
    <div class="body">${html ?? (text ? `<p>${escapeText(text).replace(/\n/g, "<br>")}</p>` : "")}</div>`;
  if (photo) {
    // The photo this page sent, shown from the phone itself (a local address, never uploaded again).
    const image = document.createElement("img");
    image.className = "photo";
    image.src = photo;
    image.alt = "Photo sent to Cora";
    message.querySelector(".body").prepend(image);
  }
  openLinksOutside(message);
  return append(message);
}

// Links in her replies open in the browser, not inside the Home Screen app in place of Cora.
function openLinksOutside(element) {
  for (const link of element.querySelectorAll(".body a")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
}

/** An image from Cora's memory; once she no longer keeps it, a quiet line says so instead. */
function keptImage(src, alt) {
  const image = document.createElement("img");
  image.className = "photo";
  image.src = src;
  image.alt = alt;
  image.addEventListener("error", () => {
    const gone = document.createElement("p");
    gone.className = "gone";
    gone.textContent = "(no longer kept)";
    image.replaceWith(gone);
  });
  return image;
}

/** A photo the person sent from another device or the desk: under their latest message. */
export function attachPhoto(src) {
  const yours = output.querySelectorAll(".message.you");
  const message = yours[yours.length - 1];
  if (!message) return;
  const follow = nearBottom();
  message.querySelector(".body").append(keptImage(src, "Photo sent to Cora"));
  if (follow) output.scrollTop = output.scrollHeight;
}

/** A picture Cora showed, with her caption and a link to the page it's from. */
export function addPicture({ src, caption, url, when = new Date() }) {
  const message = addMessage({ who: "Cora", via: "picture", text: "", when });
  const body = message.querySelector(".body");
  body.append(keptImage(src, caption || "A picture from Cora"));
  const line = document.createElement("p");
  line.className = "caption";
  line.textContent = caption ? `${caption} · ` : "";
  try {
    // Only web links: her tool only gives https pages, but nothing else is made clickable here.
    if (!/^https?:\/\//i.test(url)) throw new Error("not a web link");
    const link = document.createElement("a");
    link.href = url;
    link.textContent = new URL(url).hostname;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    line.append(link);
  } catch {
    // No usable link: the caption alone.
  }
  body.append(line);
  return message;
}

// Only Google's own map frames and links are shown; anything else in a map item is ignored.
const MAP_EMBED = "https://www.google.com/maps/embed/v1/";
const MAP_LINK = "https://www.google.com/maps/";

/** A map Cora showed: Google's interactive map, its caption, and a link that opens Google Maps
 * (the Maps app on a phone). */
export function addMap({ caption, embed, link, when = new Date() }) {
  const message = addMessage({ who: "Cora", via: "map", text: "", when });
  const body = message.querySelector(".body");
  if (typeof embed === "string" && embed.startsWith(MAP_EMBED)) {
    const frame = document.createElement("iframe");
    frame.className = "map";
    frame.src = embed;
    frame.title = caption || "A map from Cora";
    frame.loading = "lazy";
    frame.allowFullscreen = true;
    // The page's address goes with it: that's what the key is restricted to.
    frame.referrerPolicy = "strict-origin-when-cross-origin";
    body.append(frame);
  }
  const line = document.createElement("p");
  line.className = "caption";
  line.textContent = caption ? `${caption} · ` : "";
  if (typeof link === "string" && link.startsWith(MAP_LINK)) {
    const open = document.createElement("a");
    open.href = link;
    open.textContent = "Open in Google Maps";
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    line.append(open);
  }
  body.append(line);
  return message;
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
      const follow = nearBottom();
      body.innerHTML = html;
      openLinksOutside(message);
      if (follow) output.scrollTop = output.scrollHeight;
    },
    fail() {
      message.classList.remove("writing");
      message.classList.add("incomplete");
      label.textContent = ` · ${via}`;
    },
  };
}

/** Empties the chat, for a fresh load of the conversation. */
export function clearChat() {
  // Stop watching the old messages, so they can go.
  growing?.disconnect();
  output.replaceChildren();
  following = true;
}

/** A quiet line for something that happened, like "Cora went to standby". */
export function addNotice(text) {
  const notice = document.createElement("div");
  notice.className = "notice";
  notice.textContent = text;
  return append(notice);
}

/** A tool waiting for your yes; onAnswer(true|false) once tapped. */
const OUTCOMES = { approved: "✓ Approved", declined: "✕ Declined", lapsed: "◌ Lapsed: nothing was done" };

/**
 * A tool waiting for the person's yes, with exactly what it would do. onAnswer(true|false) sends
 * the answer and resolves to an error message, or null when it was taken. settle(outcome) shows
 * how it ended (approved, declined or lapsed), from whichever page answered.
 */
export function addApproval({ title, detail, until, onAnswer }) {
  const card = document.createElement("div");
  card.className = "approval";
  card.innerHTML = `
    <div class="what">⚠ Cora would like to use ${escapeText(title)}</div>
    <pre class="detail">${escapeText(detail)}</pre>
    <div class="actions">
      <button type="button" class="yes">Approve</button>
      <button type="button" class="no">Deny</button>
    </div>
    <div class="note">Nothing happens until you answer${until ? ` · lapses at ${time(until)}` : ""}.</div>`;
  const buttons = card.querySelectorAll(".actions button");
  const note = card.querySelector(".note");
  for (const [selector, answer] of [[".yes", true], [".no", false]]) {
    card.querySelector(selector).addEventListener("click", async () => {
      buttons.forEach((button) => { button.disabled = true; });
      note.textContent = answer ? "Approving…" : "Declining…";
      const error = await onAnswer(answer);
      if (error && !card.classList.contains("answered")) {
        buttons.forEach((button) => { button.disabled = false; });
        note.textContent = error;
      }
    });
  }
  append(card);
  return {
    settle(outcome) {
      card.classList.add("answered", `outcome-${outcome}`);
      card.querySelector(".what").textContent = `${OUTCOMES[outcome] || outcome} · ${title}`;
      note.textContent = "";
    },
  };
}

/** The desk's Live state (active, standby, connecting, disconnecting or disconnected). */
export function setStatus(state) {
  if (!STATES[state]) return;
  statusButton.className = `state-${state}`;
  statusButton.textContent = STATES[state];
  statusButton.hidden = false;
  document.dispatchEvent(new CustomEvent("cora-status", { detail: state }));
}

/** Calls back with the desk's Live state each time it changes (the voice panel follows it). */
export function onStatus(callback) {
  document.addEventListener("cora-status", (event) => callback(event.detail));
  // The state may have arrived already.
  if (!statusButton.hidden) callback(statusButton.className.replace("state-", ""));
}

const LINKS = { connecting: "connecting…", reconnecting: "reconnecting…", offline: "offline" };

/** Whether this page is in touch with Cora: connecting, linked, reconnecting, or offline. */
export function setLink(state, deviceName) {
  const link = document.getElementById("link-status");
  // Connecting looks like reconnecting: waiting, not wrong.
  link.className = state === "connecting" ? "reconnecting" : state;
  link.textContent = state === "linked" ? `web · ${deviceName}` : LINKS[state];
}

const BAR_CHARS = " ▁▂▃▄▅▆▇█";

/** Eight audio bars from levels 0..1, or a flat line when there's no sound. */
export function setBars(levels) {
  bars.textContent = levels?.length
    ? levels.map((level) => BAR_CHARS[Math.round(Math.max(0, Math.min(1, level)) * 8)]).join("")
    : "";
}

/** Calls onSend(text) when you send; Enter sends, Shift+Enter adds a line. */
export function setupComposer(onSend, hasPhoto = () => false) {
  const grow = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
    composer.classList.toggle("has-text", input.value.trim() !== "" || hasPhoto());
  };
  composer.addEventListener("photo-changed", grow);
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
    // A photo can go on its own.
    if (!text && !hasPhoto()) return;
    input.value = "";
    onSend(text);
    grow();
  });
}

/** The status bar's safe area at the top, as the device reports it to CSS. */
function safeTop() {
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;visibility:hidden;height:env(safe-area-inset-top)";
  document.body.append(probe);
  const height = probe.getBoundingClientRect().height;
  probe.remove();
  return Math.round(height);
}

/**
 * iOS Safari doesn't shrink the page for the on-screen keyboard, it covers it. The page is
 * pinned to the screen's edges (style.css); this lifts its bottom edge by the keyboard's height,
 * so the composer stays just above it.
 *
 * The keyboard's height is how much the visible area shrank from its height with no keyboard.
 *
 * iOS Home Screen apps (seen on iOS 26) also lay the page out in an area shorter than the screen
 * by the status bar's height, anchored at the top, so the bottom of the screen is left outside
 * the page. With no keyboard, the page's bottom edge is pushed down by that missing part.
 */
export function fitToViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const root = document.documentElement;
  const typing = () => ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  const standalone = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  let full = viewport.height;
  // Measured on iOS 26: an iPhone app's window reaches the screen's bottom while its layout area
  // stops short (874 and 812, short by the 62-point status bar), so content placed below is drawn.
  // An iPad app's window itself ends short (1133 and 1101, short by 32), so nothing below can be
  // drawn. The numbers alone look the same, so only iPhones are stretched.
  const iphone = /iPhone/.test(navigator.userAgent);
  // For iPhone-only spacing in style.css.
  root.classList.toggle("iphone", iphone);
  /** How far short of the screen's height the page's area stops, in an iPhone Home Screen app. */
  const missing = () => {
    if (!standalone || !iphone) return 0;
    // iOS keeps screen width and height as in portrait, whichever way the phone is turned.
    const landscape = window.matchMedia("(orientation: landscape)").matches;
    const screenHeight = landscape ? Math.min(screen.width, screen.height) : Math.max(screen.width, screen.height);
    const short = screenHeight - root.clientHeight;
    // Only exactly the status bar's worth (62 and 62 on an iPhone). Anything else, such as an
    // iPad app in a window smaller than the screen, isn't this quirk and is left alone.
    return short > 0 && Math.abs(short - safeTop()) <= 2 ? short : 0;
  };
  const fit = () => {
    const follow = nearBottom();
    // With nothing focused there's no keyboard: this is the whole height (it changes on rotating).
    if (!typing()) full = viewport.height;
    const keyboard = Math.max(0, Math.round(full - viewport.height - viewport.offsetTop));
    root.style.setProperty("--keyboard", `${keyboard}px`);
    // Above the keyboard when it's open; otherwise down to the screen's real bottom.
    root.style.setProperty("--bottom", keyboard > 0 ? `${keyboard}px` : `${-missing()}px`);
    root.classList.toggle("keyboard-open", keyboard > 0);
    // Safari scrolls the page to show the focused box; the layout already does that.
    window.scrollTo(0, 0);
    if (follow) output.scrollTop = output.scrollHeight;
  };
  viewport.addEventListener("resize", fit);
  viewport.addEventListener("scroll", fit);
  // Focus changes before the keyboard has fully opened or closed; the resize follows.
  document.addEventListener("focusout", () => setTimeout(fit, 50));
  // iOS fills in the safe areas a moment after the page starts, and may change them when the app
  // comes back to the front, without a resize: so checked again once settled, and on return.
  window.addEventListener("load", fit);
  window.addEventListener("pageshow", fit);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") setTimeout(fit, 100);
  });
  for (const delay of [100, 500, 1500]) setTimeout(fit, delay);
  fit();
}

/**
 * What the device reports about the screen, for fixing layout on a real phone: tap the device
 * name at the bottom right five times quickly to show or hide it. Updates as things change.
 */
export function setupLayoutReport(trigger) {
  let taps = 0;
  let timer = null;
  let panel = null;
  const inset = (side) => {
    const probe = document.createElement("div");
    probe.style.cssText = `position:fixed;visibility:hidden;height:env(safe-area-inset-${side})`;
    document.body.append(probe);
    const value = probe.getBoundingClientRect().height;
    probe.remove();
    return Math.round(value);
  };
  const report = () => {
    if (!panel) return;
    const viewport = window.visualViewport;
    const screen = document.querySelector(".screen:not([hidden])")?.getBoundingClientRect();
    const root = document.documentElement;
    panel.textContent = [
      `screen ${window.screen.width}×${window.screen.height}`,
      `inner ${window.innerWidth}×${window.innerHeight}`,
      `client ${root.clientWidth}×${root.clientHeight}`,
      viewport ? `visual ${Math.round(viewport.width)}×${Math.round(viewport.height)} top ${Math.round(viewport.offsetTop)}` : "visual none",
      `safe top ${inset("top")} bottom ${inset("bottom")}`,
      `keyboard ${getComputedStyle(root).getPropertyValue("--keyboard").trim()} bottom ${getComputedStyle(root).getPropertyValue("--bottom").trim()}`,
      screen ? `page ${Math.round(screen.top)}→${Math.round(screen.bottom)}` : "page ?",
      `standalone ${navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches}`,
    ].join("\n");
  };
  trigger.addEventListener("click", () => {
    taps += 1;
    clearTimeout(timer);
    timer = setTimeout(() => { taps = 0; }, 1500);
    if (taps < 5) return;
    taps = 0;
    if (panel) {
      panel.remove();
      panel = null;
      return;
    }
    panel = document.createElement("pre");
    panel.className = "layout-report";
    document.body.append(panel);
    report();
  });
  window.visualViewport?.addEventListener("resize", report);
  window.addEventListener("resize", report);
  setInterval(report, 1000);
}

export { statusButton };
