"""Recording while the push-to-talk button is held, and turning it into text.

OpenAI's transcription is the default (far better with names); the same offline faster-whisper
model as her wake word (models/wake) is the choice on this PC, and the fallback when OpenAI
can't be reached. Both are given the game's words, so names come out spelled right. The audio
is kept in memory only until it's transcribed."""

import io
import threading
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
# Shorter than this is a stray click; longer is cut off, in case the button sticks.
MIN_SECONDS = 0.3
MAX_SECONDS = 30.0
# What Whisper says over silence or noise; never worth sending to a game.
NOT_SPEECH = {"", "you", "thank you", "thank you.", "thanks for watching!", "[blank_audio]", "(silence)"}

CLOUD_MODEL = "gpt-4o-transcribe"
CLOUD_TIMEOUT_SECONDS = 10
# How long an idle connection to OpenAI is kept (the library's default is 5 seconds, shorter than
# a long line held down, so letting go would often wait for a new one).
KEEPALIVE_SECONDS = 30.0
# Trimming the silence before and after speaking: 20 ms frames, quiet below this level (or a
# twentieth of the loudest frame), keeping a little either side so soft first and last sounds stay.
TRIM_FRAME = 320
TRIM_FLOOR = 0.003
TRIM_SHARE = 0.05
TRIM_PAD_SECONDS = 0.25
# Settings > Game's choice, by saved value.
TRANSCRIPTION_CHOICES = ("openai", "local")
DEFAULT_TRANSCRIPTION = "openai"
# The person's own names and words (Settings > Game), added to the game's.
MAX_VOCABULARY_CHARS = 200
# Words World of Warcraft players say that a speech model spells wrong.
GAME_WORDS = (
    "World of Warcraft", "Azeroth", "Kalimdor", "Eastern Kingdoms", "Teldrassil", "Darnassus", "Dolanaar",
    "Darkshore", "Auberdine", "Ashenvale", "Stormwind", "Ironforge", "Orgrimmar", "Westfall", "Elwynn",
    "Duskwood", "Stranglethorn", "Night Elf", "Kaldorei", "druid", "Moonkin", "Feral", "Guardian",
    "Restoration", "Balance", "hunter", "paladin", "warlock", "rogue", "shaman", "mage", "priest", "warrior",
    "Horde", "Alliance", "quest", "questline", "mob", "mobs", "aggro", "pull", "tank", "healer", "DPS", "LFG",
    "dungeon", "raid", "buff", "debuff", "rez", "AFK", "BRB",
)
# Short chat slang ("GG", "lol", "ty", "np", "loot") is left out on purpose: the model leans towards
# whatever the prompt contains, so it turned ordinary speech ("G'day") into slang nobody said.


def vocabulary(own_words: str = "") -> str:
    """The prompt that steers spelling: the person's own words first, then the game's."""
    own = own_words.strip()[:MAX_VOCABULARY_CHARS]
    return ("Spoken by an Australian player in World of Warcraft. "
            + (f"Names: {own}. " if own else "") + "Words: " + ", ".join(GAME_WORDS) + ".")


def is_speech(audio: np.ndarray) -> bool:
    """Long and loud enough to be worth transcribing."""
    return audio.size >= MIN_SECONDS * SAMPLE_RATE and float(np.sqrt(np.mean(audio * audio))) >= 0.003


def trimmed(audio: np.ndarray) -> np.ndarray:
    """The audio without the silence before and after speaking, so there's less to send and
    transcribe; empty when nothing is loud enough to be speech."""
    frames = audio.size // TRIM_FRAME
    if frames == 0:
        return audio
    levels = np.sqrt(np.mean(audio[:frames * TRIM_FRAME].reshape(frames, TRIM_FRAME) ** 2, axis=1))
    loud = np.flatnonzero(levels >= max(TRIM_FLOOR, TRIM_SHARE * float(levels.max())))
    if loud.size == 0:
        return audio[:0]
    pad = int(TRIM_PAD_SECONDS * SAMPLE_RATE)
    start = max(0, int(loud[0]) * TRIM_FRAME - pad)
    end = min(audio.size, (int(loud[-1]) + 1) * TRIM_FRAME + pad)
    return audio[start:end]


def clean(text: str) -> str:
    text = text.strip()
    return "" if text.casefold() in NOT_SPEECH else text


def wav_bytes(audio: np.ndarray) -> bytes:
    """16 kHz mono 16-bit WAV, in memory."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(SAMPLE_RATE)
        file.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


class CloudTranscriber:
    """OpenAI's transcription; raises on any failure so the caller can fall back."""

    def __init__(self, model: str = CLOUD_MODEL, client=None):
        self.model = model
        self.client = client
        self.lock = threading.Lock()

    def connect(self):
        """The client, kept so later lines reuse its connection rather than opening a new one."""
        with self.lock:
            if self.client is None:
                import httpx2
                from openai import DefaultHttpxClient, OpenAI

                self.client = OpenAI(
                    timeout=CLOUD_TIMEOUT_SECONDS, max_retries=0,
                    http_client=DefaultHttpxClient(limits=httpx2.Limits(
                        max_connections=10, max_keepalive_connections=5, keepalive_expiry=KEEPALIVE_SECONDS)),
                )
            return self.client

    def warm(self) -> bool:
        """Opens the connection while the button is held, so letting go doesn't wait for one. A
        failure (False) is left for transcribe() to meet (and fall back from)."""
        try:
            self.connect().models.retrieve(self.model)
            return True
        except Exception:
            return False

    def transcribe(self, audio: np.ndarray, own_words: str = "") -> str:
        if not is_speech(audio):
            return ""
        try:
            result = self.connect().audio.transcriptions.create(
                model=self.model, file=("speech.wav", wav_bytes(audio), "audio/wav"),
                language="en", prompt=vocabulary(own_words),
            )
        except Exception:
            # A fresh client next time, in case the key changed or the connection went bad.
            self.client = None
            raise
        return clean(result.text)


class Recorder:
    """start() opens the microphone and keeps what it hears; stop() closes it and returns the audio
    (float32 at 16 kHz). The microphone is shared: Live's own stream keeps running alongside."""

    def __init__(self):
        self.stream = None
        self.chunks: list[np.ndarray] = []
        self.lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self.stream is not None

    def start(self) -> None:
        import sounddevice as sd

        if self.stream is not None:
            return
        with self.lock:
            self.chunks = []
        limit = int(MAX_SECONDS * SAMPLE_RATE)

        def callback(indata, frames, time_info, status):
            with self.lock:
                if sum(len(chunk) for chunk in self.chunks) < limit:
                    self.chunks.append(indata[:, 0].copy())

        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                     blocksize=800, callback=callback)
        self.stream.start()

    def stop(self) -> np.ndarray:
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self.lock:
            chunks, self.chunks = self.chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


class Transcriber:
    """Offline CPU Whisper, loaded on first use (or by load(), to have it ready)."""

    def __init__(self, model_dir: Path | None = None):
        self.model_dir = model_dir
        self.model = None
        self.lock = threading.Lock()

    def load(self) -> None:
        from faster_whisper import WhisperModel
        from wake.detector import DEFAULT_MODEL

        with self.lock:
            if self.model is None:
                self.model = WhisperModel(
                    str(self.model_dir or DEFAULT_MODEL), device="cpu", compute_type="int8", cpu_threads=4,
                    local_files_only=True,
                )

    def transcribe(self, audio: np.ndarray, own_words: str = "") -> str:
        """What was said, or "" for too short, silence or noise."""
        if not is_speech(audio):
            return ""
        self.load()
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=5, temperature=0, condition_on_previous_text=False,
            without_timestamps=True, vad_filter=True, initial_prompt=vocabulary(own_words),
        )
        return clean(" ".join(segment.text.strip() for segment in segments
                              if segment.no_speech_prob < 0.6 and segment.avg_logprob > -1.0))
