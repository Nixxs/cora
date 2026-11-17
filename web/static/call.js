// A call she placed for a task, as at the desk: shown while it rings, while she's on it and for a
// while after, and a tap opens a pop-up (the Claude Code one) with its transcript as it comes.
// Everything from the call is shown as text, never as markup.

const panel = document.getElementById("call-panel");
const list = document.getElementById("call-list");
const dialog = document.getElementById("claude-dialog");
const dialogTitle = document.getElementById("claude-dialog-title");
const dialogBody = document.getElementById("claude-dialog-body");

const LIST_MS = 3000;
const FOLLOW_MS = 1000;
let following = false;
let followTimer = null;
let listed = "";
let shownJson = "";

/** "didn't connect" -> "didn-t-connect", for the dot's colour. */
const slug = (state) => String(state).replace(/[^a-z]+/gi, "-");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function getCall() {
  try {
    const response = await fetch("/api/call", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()).call || null;
  } catch {
    return null;
  }
}

function entry(call) {
  const button = el("button", "claude-session");
  button.type = "button";
  const head = el("span", "claude-head");
  head.append(el("span", `claude-dot call-dot state-${slug(call.state)}`, "●"),
              el("span", "claude-project", `call with ${call.who}`),
              el("span", "claude-meta", `${call.state} · ${call.for}`));
  button.append(head);
  const latest = call.lines[call.lines.length - 1];
  if (latest) button.append(el("span", "claude-detail", `${latest.who === "them" ? "Them" : "Cora"}: ${latest.said}`));
  button.addEventListener("click", follow);
  return button;
}

function showList(call) {
  panel.hidden = !call;
  document.getElementById("app").classList.toggle("with-call", Boolean(call));
  // Only redrawn when something changed, so a tap isn't lost to a redraw.
  const shown = JSON.stringify(call);
  if (shown === listed) return;
  listed = shown;
  list.replaceChildren(...(call ? [entry(call)] : []));
}

function showFollowed(call) {
  // Unchanged: left as it is, so what you've selected stays selected.
  const json = JSON.stringify(call);
  if (json === shownJson && dialogBody.childElementCount) return;
  shownJson = json;
  if (!call) {
    dialogTitle.textContent = "Call";
    dialogBody.replaceChildren(el("p", "claude-none", "The call isn't shown any more."));
    return;
  }
  dialogTitle.textContent = `Call with ${call.who} · ${call.state} · ${call.for}`;
  const atEnd = dialogBody.scrollHeight - dialogBody.scrollTop - dialogBody.clientHeight < 24;
  const first = !dialogBody.childElementCount;
  const parts = [el("p", "claude-where", `${call.number} · for "${call.goal}"`)];
  if (!call.lines.length) parts.push(el("p", "claude-none", "Nothing said yet."));
  for (const line of call.lines) {
    const said = el("p", `call-line ${line.who === "them" ? "them" : "cora"}`);
    said.append(el("span", "who", line.who === "them" ? "Them: " : "Cora: "), document.createTextNode(line.said));
    parts.push(said);
  }
  dialogBody.replaceChildren(...parts);
  // Kept at the newest, unless you've scrolled up to read.
  if (atEnd || first) dialogBody.scrollTop = dialogBody.scrollHeight;
}

async function refresh() {
  const call = await getCall();
  showList(call);
  if (following) showFollowed(call);
}

function follow() {
  following = true;
  shownJson = "";
  dialogBody.replaceChildren();
  dialogTitle.textContent = "Call";
  dialog.showModal();
  refresh();
  clearInterval(followTimer);
  followTimer = setInterval(() => { if (!document.hidden) refresh(); }, FOLLOW_MS);
}

export function setupCall() {
  dialog.addEventListener("close", () => {
    following = false;
    clearInterval(followTimer);
  });
  refresh();
  setInterval(() => { if (!document.hidden && !following) refresh(); }, LIST_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}
