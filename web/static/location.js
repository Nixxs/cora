// Where you are, for Cora, only when she asks and only with "where" switched on (WB6). Off until
// you turn it on; the choice stays on this device. Each answer goes to Cora's PC once and isn't kept.

const KEY = "cora.where";
const button = document.getElementById("where-button");
let notify = () => {};

function isOn() {
  try {
    return localStorage.getItem(KEY) === "on";
  } catch {
    return false;
  }
}

function setOn(on) {
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    // Not remembered (private browsing): on for this visit only.
  }
  show(on);
}

function show(on) {
  button.textContent = on ? "where: on" : "where: off";
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", String(on));
  button.title = on ? "Cora can check where you are when it matters to what you ask"
    : "Cora can't check where you are";
}

function position(options) {
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, options));
}

async function reply(body) {
  await fetch("/api/location", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => null);
}

/** Cora asked where you are: the position if "where" is on, or why not. */
export async function answerLocate(id) {
  if (!isOn() || !navigator.geolocation) {
    await reply({ id, error: "off" });
    return;
  }
  try {
    const fix = await position({ enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
    await reply({ id, latitude: fix.coords.latitude, longitude: fix.coords.longitude, accuracy: fix.coords.accuracy });
  } catch (error) {
    await reply({ id, error: error && error.code === 1 ? "denied" : "unavailable" });
  }
}

export function setupLocation(onNotice) {
  notify = onNotice;
  if (!navigator.geolocation) return;
  show(isOn());
  button.hidden = false;
  button.addEventListener("click", async () => {
    if (isOn()) {
      setOn(false);
      notify("Cora can't check where you are now.");
      return;
    }
    // Asked now, from your tap, so the device's permission prompt comes up here and not mid-reply.
    try {
      await position({ enableHighAccuracy: false, timeout: 15000, maximumAge: 600000 });
      setOn(true);
      notify("Cora can check where you are when it matters to what you ask. You'll see a note each time.");
    } catch (error) {
      setOn(false);
      notify(error && error.code === 1
        ? "This device didn't allow location for Cora's page. It can be allowed in its settings."
        : "Couldn't get a location just now. Try again.");
    }
  });
}
