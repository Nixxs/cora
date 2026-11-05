// Cora's voice on this device: tap Listen, and her speech from the Live session at the desk plays
// here (the desk speaker goes quiet meanwhile). The bars follow what is actually playing.
//
// Her audio arrives over a WebSocket as raw 24 kHz mono 16-bit samples, only while she speaks,
// and each piece is scheduled to play straight after the one before. iOS only lets sound start
// after a tap, and keeps playing through the silent switch only when told this is playback.

const SAMPLE_RATE = 24000;
const BAR_COUNT = 12;
// Played this far ahead of now, so small gaps in arrival don't break up her voice.
const LEAD_SECONDS = 0.08;

const panel = document.getElementById("voice-panel");
const bars = document.getElementById("voice-bars");
const text = document.getElementById("voice-text");
const button = document.getElementById("listen-button");

let audio = null;     // AudioContext while listening
let analyser = null;  // what the bars read
let socket = null;
let nextStart = 0;    // when the next piece of her voice starts, on audio's clock
const playing = new Set();
let state = "idle";

const TEXT = {
  idle: "Her voice plays at the desk.",
  connecting: "Connecting…",
  listening: "Listening here · the desk speaker is quiet.",
  stopped: "Stopped listening. Tap Listen to hear her here again.",
};

function show(newState, message) {
  state = newState;
  panel.dataset.state = newState;
  text.textContent = message || TEXT[newState];
  button.textContent = newState === "listening" || newState === "connecting" ? "Stop" : "Listen here";
  button.classList.toggle("on", newState === "listening");
}

function playChunk(data) {
  const samples = new Int16Array(data);
  if (!samples.length || !audio) return;
  const buffer = audio.createBuffer(1, samples.length, SAMPLE_RATE);
  const channel = buffer.getChannelData(0);
  for (let index = 0; index < samples.length; index += 1) channel[index] = samples[index] / 32768;
  const source = audio.createBufferSource();
  source.buffer = buffer;
  source.connect(analyser);
  const start = Math.max(audio.currentTime + LEAD_SECONDS, nextStart);
  source.start(start);
  nextStart = start + buffer.duration;
  playing.add(source);
  source.onended = () => playing.delete(source);
}

async function listen() {
  show("connecting");
  try {
    // Made on the tap itself: iOS allows sound to start only from one.
    audio = new AudioContext();
    if (navigator.audioSession) navigator.audioSession.type = "playback";
    await audio.resume();
    analyser = audio.createAnalyser();
    analyser.fftSize = 64;
    analyser.smoothingTimeConstant = 0.6;
    analyser.connect(audio.destination);
  } catch {
    stop("stopped", "This browser couldn't play audio.");
    return;
  }
  nextStart = 0;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/api/voice`);
  socket.binaryType = "arraybuffer";
  socket.addEventListener("message", (event) => {
    if (typeof event.data !== "string") {
      playChunk(event.data);
    } else if (JSON.parse(event.data).type === "listening") {
      show("listening");
    }
  });
  socket.addEventListener("close", () => {
    if (state === "connecting") stop("stopped", "Couldn't start listening. Is the web interface on?");
    else if (state === "listening") stop("stopped");
  });
}

function stop(newState = "idle", message) {
  const closing = socket;
  socket = null;
  if (closing && closing.readyState <= WebSocket.OPEN) closing.close();
  for (const source of playing) {
    try { source.stop(); } catch { /* already ended */ }
  }
  playing.clear();
  if (audio) audio.close().catch(() => {});
  audio = null;
  analyser = null;
  show(newState, message);
}

/** The bars: her voice's loudness across a few bands, flat when she's quiet or nothing plays. */
function drawBars() {
  const levels = new Uint8Array(analyser ? analyser.frequencyBinCount : 0);
  if (analyser) analyser.getByteFrequencyData(levels);
  bars.childNodes.forEach((bar, index) => {
    // The lower bands carry speech; spread the bars over them.
    const level = levels.length ? levels[Math.min(levels.length - 1, Math.floor(index * 1.4) + 1)] / 255 : 0;
    bar.style.transform = `scaleY(${Math.max(0.14, level)})`;
  });
  requestAnimationFrame(drawBars);
}

export function setupVoice() {
  for (let index = 0; index < BAR_COUNT; index += 1) bars.append(document.createElement("span"));
  button.addEventListener("click", () => {
    if (state === "listening" || state === "connecting") stop();
    else listen();
  });
  // A locked phone or a backgrounded app ends the sound; say so when it comes back.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state === "listening" && (!socket || socket.readyState > WebSocket.OPEN)) {
      stop("stopped");
    }
  });
  show("idle");
  requestAnimationFrame(drawBars);
}
