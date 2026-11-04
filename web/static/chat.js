// Talking to Cora: each message goes to her and her reply streams back as she writes it.
// Messages join the same conversation as the desk, Signal and voice.

import { addMessage, addNotice, setupComposer, startWriting } from "./ui.js";

const UNREACHABLE = "Couldn't reach Cora. Is her PC on and the web interface switched on?";

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
  addNotice("The connection dropped before her reply finished. It's still shown at the desk.");
}

async function send(text) {
  addMessage({ who: "You", via: "web", text });
  const draft = startWriting("web");
  let response;
  try {
    response = await fetch("/api/messages", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
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
    addNotice("The connection dropped before her reply finished. It's still shown at the desk.");
  }
}

export function startChat() {
  addNotice("messages here join your conversation with Cora");
  setupComposer(send);
}
