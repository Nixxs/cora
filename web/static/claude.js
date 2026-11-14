// Claude Code's sessions under her voice, as at the desk: shown while Cora listens for them, each
// one opening a pop-up that follows it (what it's doing and its latest exchanges, as they come).
// Everything from a session is shown as text, never as markup.

const panel = document.getElementById("claude-panel");
const list = document.getElementById("claude-list");
const count = document.getElementById("claude-count");
const dialog = document.getElementById("claude-dialog");
const dialogTitle = document.getElementById("claude-dialog-title");
const dialogBody = document.getElementById("claude-dialog-body");

const LIST_MS = 3000;
const FOLLOW_MS = 2000;
let following = null;
let followTimer = null;
let listed = "";

/** "needs you" -> "needs-you", for the dot's colour. */
const slug = (state) => String(state).replace(/[^a-z]+/gi, "-");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function getJson(url) {
  try {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

function entry(session) {
  const button = el("button", "claude-session");
  button.type = "button";
  const head = el("span", "claude-head");
  head.append(el("span", `claude-dot state-${slug(session.state)}`, "●"), el("span", "claude-project", session.project),
              el("span", "claude-meta", `${session.state} · ${session.ago}`));
  button.append(head);
  if (session.detail) button.append(el("span", "claude-detail", session.detail));
  button.addEventListener("click", () => follow(session.id));
  return button;
}

async function refresh() {
  const found = await getJson("/api/claude-code");
  const listening = Boolean(found && found.listening);
  panel.hidden = !listening;
  document.getElementById("app").classList.toggle("with-claude", listening);
  if (!listening) return;
  const sessions = found.sessions || [];
  // Only redrawn when something changed, so a tap isn't lost to a redraw.
  const shown = JSON.stringify(sessions);
  if (shown === listed) return;
  listed = shown;
  list.replaceChildren(...(sessions.length ? sessions.map(entry) : [el("p", "claude-none", "no sessions yet")]));
  const waiting = sessions.filter((session) => session.state === "needs you").length;
  count.textContent = waiting ? `${waiting} need${waiting === 1 ? "s" : ""} you` : "";
}

function showFollowed(session) {
  if (!session) {
    dialogTitle.textContent = "Claude Code";
    dialogBody.replaceChildren(el("p", "claude-none", "This session isn't listed any more."));
    return;
  }
  dialogTitle.textContent = `${session.project} · ${session.state} for ${session.for}`;
  const atEnd = dialogBody.scrollHeight - dialogBody.scrollTop - dialogBody.clientHeight < 24;
  const first = !dialogBody.childElementCount;
  const parts = [el("p", "claude-where", `${session.folder}\nlast heard ${session.last_heard}`)];
  if (session.prompt) parts.push(el("p", "claude-asked", `Asked: ${session.prompt}`));
  if (session.detail && session.detail !== session.prompt) parts.push(el("p", "claude-latest", `Latest: ${session.detail}`));
  parts.push(el("p", "claude-rule", "latest exchanges"));
  if (session.exchanges === null) {
    parts.push(el("p", "claude-none", "Its transcript can't be read (yet)."));
  } else if (!session.exchanges.length) {
    parts.push(el("p", "claude-none", "Nothing said yet."));
  }
  for (const exchange of session.exchanges || []) {
    const said = el("p", `claude-said ${exchange.who === "you" ? "you" : "claude"}`);
    said.append(el("span", "who", exchange.who === "you" ? "You" : "Claude"), document.createTextNode(` ${exchange.said}`));
    parts.push(said);
  }
  dialogBody.replaceChildren(...parts);
  // Kept at the newest, unless you've scrolled up to read.
  if (atEnd || first) dialogBody.scrollTop = dialogBody.scrollHeight;
}

async function followOnce() {
  if (!following) return;
  const session = await getJson(`/api/claude-code/${encodeURIComponent(following)}`);
  if (following) showFollowed(session);
}

function follow(sessionId) {
  following = sessionId;
  dialogBody.replaceChildren();
  dialogTitle.textContent = "Claude Code";
  dialog.showModal();
  followOnce();
  clearInterval(followTimer);
  followTimer = setInterval(() => { if (!document.hidden) followOnce(); }, FOLLOW_MS);
}

export function setupClaudeCode() {
  document.getElementById("claude-dialog-close").addEventListener("click", () => dialog.close());
  // A tap on the dimmed backdrop closes it too.
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    following = null;
    clearInterval(followTimer);
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, LIST_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}
