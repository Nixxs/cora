// WB0 design preview: sample messages and a pretend Cora, nothing connected.
// Tap the status to cycle its states; send a message to see a reply stream in.

import {
  addMessage, addNotice, addApproval, startWriting, setStatus, setBars,
  setupComposer, fitToViewport, statusButton,
} from "./ui.js";

const minutesAgo = (minutes) => new Date(Date.now() - minutes * 60_000);

addNotice("earlier today");

addMessage({ who: "You", via: "Signal", text: "Can you find a time for the dentist next week? Not Monday.", when: minutesAgo(140) });
addMessage({
  who: "Cora", via: "Signal", when: minutesAgo(139),
  html: `<p>Tuesday or Thursday afternoon both look clear. Thursday at 2pm has nothing near it, so you wouldn't be rushing.</p>
         <p>Want me to email them and ask for Thursday?</p>`,
});

addNotice("at the desk");

addMessage({ who: "You", via: "voice", text: "What was that python thing for reading a file line by line again?", when: minutesAgo(32) });
addMessage({
  who: "Cora", via: "on screen", when: minutesAgo(31),
  html: `<p>Open it with <code>with</code> and loop over the file itself. It reads one line at a time, so big files are fine:</p>
<pre><code><span class="tok-key">with</span> <span class="tok-fn">open</span>(<span class="tok-str">"notes.txt"</span>, encoding=<span class="tok-str">"utf-8"</span>) <span class="tok-key">as</span> file:
    <span class="tok-key">for</span> number, line <span class="tok-key">in</span> <span class="tok-fn">enumerate</span>(file, start=<span class="tok-num">1</span>):
        <span class="tok-fn">print</span>(number, line.rstrip())  <span class="tok-com"># drop the newline</span></code></pre>
         <p><code>rstrip()</code> takes off the trailing newline each line still carries.</p>`,
});

addMessage({ who: "You", via: "web", text: "Thanks! And what's on tomorrow?", when: minutesAgo(3) });
addMessage({
  who: "Cora", via: "web", when: minutesAgo(3),
  html: `<p>A fairly light day:</p>
         <ul>
           <li><strong>10:00</strong> Standup, 15 minutes</li>
           <li><strong>13:30</strong> Lunch with Sam at <a href="#">the cafe on Smith St</a></li>
           <li><strong>16:00</strong> Dentist: still waiting on their reply</li>
         </ul>
         <p>I'll tell you when the dentist writes back.</p>`,
});

addApproval({
  title: "Cora wants to send an email",
  detail: "To: reception@smilesdental.example — “Could we book Thursday at 2pm?”",
  onAnswer: (yes) => addNotice(yes ? "approved, email sent" : "denied, nothing sent"),
});

// A pretend Cora: streams a reply word by word, then shows it finished.
const REPLIES = [
  { text: "Got it. I've noted that, and I'll bring it up if it's relevant later.",
    html: "<p>Got it. I've noted that, and I'll bring it up if it's relevant later.</p>" },
  { text: "Here's what I'd do: check the calendar first, then email them with two options so they can pick one.",
    html: "<p>Here's what I'd do:</p><ol><li>Check the calendar first</li><li>Email them with <strong>two options</strong> so they can pick one</li></ol>" },
  { text: "Good question. Honestly, I'm not sure, so let me look it up rather than guess.",
    html: "<p>Good question. Honestly, I'm not sure, so let me look it up rather than guess.</p>" },
];
let replyIndex = 0;

function pretendReply() {
  const reply = REPLIES[replyIndex++ % REPLIES.length];
  const draft = startWriting("web");
  const words = reply.text.split(" ");
  let shown = 0;
  setTimeout(function next() {
    shown += 1 + Math.floor(Math.random() * 2);
    draft.update(words.slice(0, shown).join(" "));
    if (shown < words.length) setTimeout(next, 60 + Math.random() * 90);
    else setTimeout(() => draft.finish(reply.html), 250);
  }, 700);
}

setupComposer((text) => {
  addMessage({ who: "You", via: "web", text });
  pretendReply();
});

// Tap the status to see each state; Active plays some pretend audio bars.
const ORDER = ["standby", "connecting", "active", "disconnected"];
let state = 0;
let barsTimer = null;
statusButton.addEventListener("click", () => {
  state = (state + 1) % ORDER.length;
  setStatus(ORDER[state]);
  clearInterval(barsTimer);
  setBars(null);
  if (ORDER[state] === "active") {
    barsTimer = setInterval(() => setBars(Array.from({ length: 8 }, () => Math.random() ** 1.6)), 90);
  }
});

document.getElementById("settings-button").addEventListener("click", () => addNotice("settings come in a later step"));

fitToViewport();
