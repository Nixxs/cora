// The page's start: is this device paired? If so, the chat; if not, the pairing screen.
// A link from the QR code in Settings (…/#pair=CODE) pairs straight away.

import { fitToViewport, setupLayoutReport } from "./ui.js";

const pairScreen = document.getElementById("pair-screen");
const app = document.getElementById("app");
const form = document.getElementById("pair-form");
const codeInput = document.getElementById("pair-code");
const pairButton = document.getElementById("pair-button");
const pairError = document.getElementById("pair-error");

const standalone = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;

/** A name for Settings' device list, from what the browser says it is. */
function deviceName() {
  const agent = navigator.userAgent;
  // iPads ask for desktop sites, so they say Macintosh; they have touch, Macs don't.
  const name = /iPhone/.test(agent) ? "iPhone"
    : /iPad/.test(agent) || (/Macintosh/.test(agent) && navigator.maxTouchPoints > 1) ? "iPad"
    : /Android/.test(agent) ? (/Mobile/.test(agent) ? "Android phone" : "Android tablet")
    : /Macintosh/.test(agent) ? "Mac"
    : /Windows/.test(agent) ? "Windows PC"
    : /Linux/.test(agent) ? "Linux PC"
    : "Browser";
  return standalone ? `${name} (Home Screen app)` : name;
}

async function session() {
  try {
    const response = await fetch("/api/session", { credentials: "same-origin", cache: "no-store" });
    if (response.ok) return await response.json();
  } catch {
    // Offline or Cora isn't running: shown as not paired, with a reason.
    return { paired: false, offline: true };
  }
  return { paired: false };
}

async function pair(code) {
  pairButton.disabled = true;
  pairError.textContent = "";
  try {
    const response = await fetch("/api/pair", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name: deviceName() }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      await showApp(body.device);
      return;
    }
    pairError.textContent = body.error || "That didn't work. Try again.";
  } catch {
    pairError.textContent = "Couldn't reach Cora. Is her PC on and the web interface switched on?";
  } finally {
    pairButton.disabled = false;
  }
}

function showPairing(offline) {
  app.hidden = true;
  pairScreen.hidden = false;
  if (offline) pairError.textContent = "Couldn't reach Cora. Is her PC on and the web interface switched on?";
  // The hint about the Home Screen is for iPhones and iPads in Safari, not the app itself.
  document.getElementById("pair-hint").hidden = standalone || !/iPhone|iPad|Macintosh/.test(navigator.userAgent)
    || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints <= 1);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (codeInput.value.trim()) pair(codeInput.value);
  });
  // Typed as K7QF-M2XP whatever the case or spacing.
  codeInput.addEventListener("input", () => {
    const plain = codeInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
    codeInput.value = plain.length > 4 ? `${plain.slice(0, 4)}-${plain.slice(4)}` : plain;
  });
}

async function showApp(deviceName) {
  pairScreen.hidden = true;
  app.hidden = false;
  const { startChat } = await import("./chat.js");
  startChat(deviceName);
  const { setupVoice } = await import("./voice.js");
  setupVoice();
}

// The code from the QR link is read, then taken out of the address so it isn't left in history.
const linked = new URLSearchParams(location.hash.slice(1)).get("pair");
if (linked) history.replaceState(null, "", location.pathname);

fitToViewport();
setupLayoutReport(document.getElementById("link-status"));
const state = await session();
if (state.paired) {
  await showApp(state.device);
} else {
  showPairing(state.offline);
  if (linked) {
    codeInput.value = linked;
    codeInput.dispatchEvent(new Event("input"));
    pair(linked);
  }
}
