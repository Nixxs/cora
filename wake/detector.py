from pathlib import Path
import re

import numpy as np
from faster_whisper import WhisperModel
import soxr


DEFAULT_MODEL = (
    Path(__file__).resolve().parents[1] / "models" / "wake"
    / "faster-whisper-base.en"
)
WAKE_NAMES = ("cora", "kora", "korra")


def match_wake_phrase(text: str) -> str:
    words = re.findall(r"[a-z]+", text.casefold())
    for first, second in zip(words, words[1:]):
        if first == "hey" and second in WAKE_NAMES:
            return f"hey {second}"
    return ""


class WakeDetector:
    """Offline CPU Whisper on short snapshots; no downloads or cloud calls."""

    def __init__(self, model_dir: Path = DEFAULT_MODEL):
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
            path = model_dir / name
            if not path.is_file():
                raise FileNotFoundError(f"Missing wake model component: {path.name}")
        self.model = WhisperModel(
            str(model_dir), device="cpu", compute_type="int8", cpu_threads=2,
            local_files_only=True,
        )
        self.last_transcript = ""
        self.last_match = ""

    def process(self, pcm: bytes) -> bool:
        self.last_transcript = ""
        self.last_match = ""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        # Skip silence cheaply; Silero VAD below also rejects non-speech noise.
        if samples.size < 7200 or float(np.sqrt(np.mean(samples * samples))) < 0.003:
            return False
        audio = soxr.resample(samples, 24000, 16000)
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, temperature=0,
            condition_on_previous_text=False, without_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_speech_duration_ms": 150, "min_silence_duration_ms": 350},
        )
        # Decoding is lazy: consume the generator in this off-thread call.
        segments = list(segments)
        self.last_transcript = " ".join(segment.text.strip() for segment in segments).strip()
        confident_text = " ".join(
            segment.text for segment in segments
            if segment.no_speech_prob < 0.6 and segment.avg_logprob > -1.0
        )
        self.last_match = match_wake_phrase(confident_text)
        return bool(self.last_match)