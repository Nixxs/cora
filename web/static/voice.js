// Talking to Cora on this device: a call. Talk makes this device's microphone what she hears and
// plays her voice here (the desk mic and speaker go quiet), waking her if she's in standby;
// Hang up hands both back to the desk, and if Talk woke her she goes back to standby.
//
// Audio goes both ways over one WebSocket as raw 24 kHz mono 16-bit samples. Her pieces are
// scheduled to play straight after one another; the microphone is resampled to 24 kHz from
// whatever rate the device uses and sent in 40 ms pieces.
//
// The microphone is turned on before the audio engine is made. With Bluetooth headphones, iOS
// switches them to their headset mode (with a mic) the moment the mic starts, which changes the
// sample rate; an engine made before that kept the old rate, so playback crackled and the mic
// gave silence. Made after, it starts at the headset's rate. (A page using the mic may start
// sound, so this still works from the tap.) If headphones come or go mid-call, the audio is
// rebuilt on the new route.
//
// Mute stops what she hears from this device (the mic sends silence) while you still hear her.
// The bars show her voice upward; downward, your mic while you're speaking, and otherwise a
// mirror of hers, so her speech fills both halves.

import { onStatus } from "./ui.js";

const SAMPLE_RATE = 24000;
const BAR_COUNT = 12;
// Played this far ahead of now, so small gaps in arrival don't break up her voice.
const LEAD_SECONDS = 0.08;
// The microphone goes to Cora in pieces this long (960 samples at 24 kHz = 40 ms).
const MIC_PIECE_SAMPLES = 960;
// You count as speaking when the mic's loudness (RMS, after noise suppression) passes this, and
// for HEARD_HOLD_MS after, so the lower bars don't flicker between yours and the mirror.
const SPEAKING_LEVEL = 0.02;
const HEARD_HOLD_MS = 600;

const panel = document.getElementById("voice-panel");
const bars = document.getElementById("voice-bars");
const text = document.getElementById("voice-text");
const talkButton = document.getElementById("talk-button");
const muteButton = document.getElementById("mute-button");

let audio = null;     // AudioContext during a call
let analyser = null;  // her voice, for the top half of the bars
let micAnalyser = null; // the person's microphone, for the bottom half
let mic = null;       // { stream, source, node } during a call
let socket = null;
let nextStart = 0;    // when the next piece of her voice starts, on audio's clock
const playing = new Set();
let state = "idle";   // idle, connecting, talking
let deskState = null; // the desk's Live state: active, standby, connecting…
let note = "";        // a one-off line, such as why a call ended
let muted = false;    // this device's mic sends silence
let heardAt = 0;      // when the mic last picked you up speaking

function describe() {
  if (note) return note;
  if (state === "connecting") return "Connecting…";
  if (state === "talking") {
    if (muted) return "Muted · she can't hear you; you still hear her.";
    return deskState === "active" ? "On a call here · the desk mic and speaker are quiet." : "Waking Cora…";
  }
  return deskState === "active" ? "Tap Talk to talk to her here." : "She's in standby · Talk wakes her.";
}

function show(newState = state) {
  state = newState;
  panel.dataset.state = state;
  text.textContent = describe();
  talkButton.textContent = state === "idle" ? "Talk" : "Hang up";
  talkButton.classList.toggle("on", state !== "idle");
  muteButton.hidden = state !== "talking";
  muteButton.textContent = muted ? "Unmute" : "Mute";
  muteButton.classList.toggle("on", muted);
  muteButton.setAttribute("aria-pressed", String(muted));
}

// ── The audio engine: microphone first, then playback at the route's own rate ──

async function startAudio() {
  if (navigator.audioSession) navigator.audioSession.type = "play-and-record";
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  audio = new AudioContext();
  await audio.resume();
  analyser = audio.createAnalyser();
  analyser.fftSize = 64;
  analyser.smoothingTimeConstant = 0.6;
  analyser.connect(audio.destination);
  await audio.audioWorklet.addModule("mic-worklet.js");
  const source = audio.createMediaStreamSource(stream);
  // What the mic picks up, for the bars (it's only read, never played).
  micAnalyser = audio.createAnalyser();
  micAnalyser.fftSize = 64;
  micAnalyser.smoothingTimeConstant = 0.6;
  source.connect(micAnalyser);
  const node = new AudioWorkletNode(audio, "mic-capture");
  // Connected through silence so the browser keeps it running; nothing of it is heard here.
  const silent = audio.createGain();
  silent.gain.value = 0;
  source.connect(node).connect(silent).connect(audio.destination);
  node.port.onmessage = (event) => sendMic(event.data);
  mic = { stream, source, node };
  // Still muted after headphones change mid-call.
  applyMute();
  startedAt = performance.now();
  nextStart = 0;
  carry = new Float32Array(0);
  position = 0;
  pending = [];
}

/** A muted track gives silence: the mic's pieces still go, so her side of the call carries on. */
function applyMute() {
  mic?.stream.getAudioTracks().forEach((track) => { track.enabled = !muted; });
}

function setMuted(on) {
  muted = on;
  applyMute();
  show();
}

function stopAudio() {
  if (mic) {
    mic.node.port.onmessage = null;
    mic.source.disconnect();
    mic.node.disconnect();
    mic.stream.getTracks().forEach((track) => track.stop());
    mic = null;
  }
  for (const source of playing) {
    try { source.stop(); } catch { /* already ended */ }
  }
  playing.clear();
  if (audio) audio.close().catch(() => {});
  audio = null;
  analyser = null;
  micAnalyser = null;
}

/** Headphones connected or disconnected mid-call: the same call, on the new route. Starting the
 * mic can itself announce a device change, so not within a couple of seconds of a start. */
let rebuilding = false;
let startedAt = 0;
async function rebuildAudio() {
  if (state !== "talking" || rebuilding || performance.now() - startedAt < 2000) return;
  rebuilding = true;
  try {
    stopAudio();
    await startAudio();
  } catch {
    hangUp("The audio stopped when the headphones changed. Tap Talk to carry on.");
  } finally {
    rebuilding = false;
  }
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

// ── The microphone, resampled to 24 kHz and sent in 40 ms pieces ──

let carry = new Float32Array(0); // samples kept for the next resampling step
let position = 0;                // where the next output sample falls in carry
let pending = [];                // 16-bit samples waiting to fill a piece

function sendMic(input) {
  if (!socket || socket.readyState !== WebSocket.OPEN || state !== "talking" || !audio) return;
  const ratio = audio.sampleRate / SAMPLE_RATE;
  const data = new Float32Array(carry.length + input.length);
  data.set(carry);
  data.set(input, carry.length);
  let at = position;
  while (at + 1 < data.length) {
    const index = Math.floor(at);
    const fraction = at - index;
    const sample = data[index] * (1 - fraction) + data[index + 1] * fraction;
    pending.push(Math.max(-32768, Math.min(32767, Math.round(sample * 32767))));
    at += ratio;
  }
  const keep = Math.floor(at);
  carry = data.slice(keep);
  position = at - keep;
  while (pending.length >= MIC_PIECE_SAMPLES) {
    socket.send(Int16Array.from(pending.splice(0, MIC_PIECE_SAMPLES)).buffer);
  }
}

// ── The call ──

function onMessage(event) {
  if (typeof event.data !== "string") {
    playChunk(event.data);
    return;
  }
  const message = JSON.parse(event.data);
  if (message.type === "listening") {
    // Connected: now the call itself.
    socket.send(JSON.stringify({ type: "talk", on: true }));
  } else if (message.type === "talking") {
    note = "";
    show("talking");
  } else if (message.type === "talk_ended") {
    hangUp(message.reason === "taken over" ? "Another device took over the call."
      : message.reason === "standby" ? "Cora went into standby, so the call ended." : "");
  }
}

async function talk() {
  note = "";
  show("connecting");
  try {
    await startAudio();
  } catch {
    stopAudio();
    note = "Couldn't use the microphone. Allow it for this app and try again.";
    show("idle");
    return;
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/api/voice`);
  socket.binaryType = "arraybuffer";
  socket.addEventListener("message", onMessage);
  socket.addEventListener("close", () => {
    if (state === "connecting") hangUp("Couldn't reach Cora. Is the web interface on?");
    else if (state === "talking") hangUp("The call dropped. Tap Talk to carry on.");
  });
}

/** Ends the call here; the desk gets its mic and speaker back as the connection closes. */
function hangUp(message = "") {
  const closing = socket;
  socket = null;
  if (closing && closing.readyState <= WebSocket.OPEN) closing.close();
  stopAudio();
  if (navigator.audioSession) navigator.audioSession.type = "auto";
  muted = false;
  note = message;
  show("idle");
}

/** Loudness across a few bands (0 to 1 each), from an analyser; none when there isn't one. */
function bandLevels(source) {
  const levels = new Uint8Array(source ? source.frequencyBinCount : 0);
  if (source) source.getByteFrequencyData(levels);
  // The lower bands carry speech; spread the bars over them.
  return (index) => (levels.length ? levels[Math.min(levels.length - 1, Math.floor(index * 1.4) + 1)] / 255 : 0);
}

/** Whether the mic picks you up speaking now, or did a moment ago. */
const timeDomain = new Float32Array(64);
function speaking() {
  if (!micAnalyser || muted) return false;
  micAnalyser.getFloatTimeDomainData(timeDomain);
  let sum = 0;
  for (const sample of timeDomain) sum += sample * sample;
  const now = performance.now();
  if (Math.sqrt(sum / timeDomain.length) >= SPEAKING_LEVEL) heardAt = now;
  return now - heardAt < HEARD_HOLD_MS;
}

/** The bars, split at a centre line as the TUI's dashboard shows them: her voice upward in her
 * green; downward, the person's microphone in the "You" blue while they speak, and otherwise her
 * voice mirrored in green. Flat when quiet or no call is on. */
function drawBars() {
  const hers = bandLevels(analyser);
  const mirror = !speaking();
  const lower = mirror ? hers : bandLevels(micAnalyser);
  bars.childNodes.forEach((column, index) => {
    column.firstChild.style.transform = `scaleY(${Math.max(0.1, hers(index))})`;
    column.lastChild.style.transform = `scaleY(${Math.max(0.1, lower(index))})`;
    column.lastChild.classList.toggle("mirror", mirror);
  });
  requestAnimationFrame(drawBars);
}

export function setupVoice() {
  for (let index = 0; index < BAR_COUNT; index += 1) {
    const column = document.createElement("span");
    column.className = "column";
    const her = document.createElement("span");
    her.className = "her";
    const you = document.createElement("span");
    you.className = "you";
    column.append(her, you);
    bars.append(column);
  }
  talkButton.addEventListener("click", () => {
    if (state === "idle") talk();
    else hangUp();
  });
  muteButton.addEventListener("click", () => setMuted(!muted));
  onStatus((desk) => {
    deskState = desk;
    show();
  });
  navigator.mediaDevices?.addEventListener("devicechange", rebuildAudio);
  // A locked phone or a backgrounded app ends the call; say so when it comes back.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state !== "idle" && (!socket || socket.readyState > WebSocket.OPEN)) {
      hangUp("The call ended while the app was in the background. Tap Talk to carry on.");
    }
  });
  show("idle");
  requestAnimationFrame(drawBars);
}
