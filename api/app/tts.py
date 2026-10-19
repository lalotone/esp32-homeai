"""Text-to-speech in Rocky's voice (Project Hail Mary), resampled for the ESP32.

Qwen3-TTS 12Hz Base clones the voice zero-shot from a reference clip plus its exact
transcript (scripts/get_rocky_voice.sh copies them in from the Rocky voice project).
The model and the encoded voice prompt are loaded once and cached for the process
lifetime; `synthesize` is a blocking call, so the API layer runs it in a threadpool.

Lessons carried over from tuning the voice by ear:
  * short replies are generated in one pass: one call per sentence sounds slow and stiff
  * each chunk gets a token budget from its word count, and one cooler retry if it hits
    it (a runaway that never stops talking)

The model speaks at 24 kHz; we downsample to `output_sample_rate` (16 kHz default) and
emit 16-bit mono PCM.
"""
import io
import re
import threading
import wave

import numpy as np
import torch

from .config import settings

MODEL_RATE = 24000      # Qwen3-TTS 12Hz tokenizer decodes to 24 kHz
TOKENS_PER_S = 12       # 12 Hz codec
SECONDS_PER_WORD = 0.6  # Rocky speaks ~0.45 s/word; leave headroom
MAX_CHUNK_CHARS = 300
TAIL_SILENCE_S = 0.5    # see synthesize(): keeps the ESP32 from looping the last bit of speech

_model = None
_prompt = None
_lock = threading.Lock()  # the model isn't re-entrant


def get_model():
    """Load the model and encode Rocky's voice prompt on first use."""
    global _model, _prompt
    with _lock:
        if _model is None:
            from qwen_tts import Qwen3TTSModel

            cuda = settings.tts_device.startswith("cuda")
            _model = Qwen3TTSModel.from_pretrained(
                settings.tts_model,
                device_map=settings.tts_device,
                dtype=torch.bfloat16 if cuda else torch.float32,
            )
            with open(settings.tts_ref_text) as f:
                ref_text = f.read().strip()
            _prompt = _model.create_voice_clone_prompt(ref_audio=settings.tts_ref_audio, ref_text=ref_text)
    return _model


def _chunks(text: str) -> list[str]:
    """Group whole sentences into chunks of up to MAX_CHUNK_CHARS (one chunk for a short reply)."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    chunks: list[str] = []
    for s in sentences:
        while len(s) > MAX_CHUNK_CHARS:
            cut = s.rfind(",", 0, MAX_CHUNK_CHARS)
            cut = cut if cut > 40 else MAX_CHUNK_CHARS
            chunks.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if not s:
            continue
        if chunks and len(chunks[-1]) + 1 + len(s) <= MAX_CHUNK_CHARS:
            chunks[-1] += " " + s
        else:
            chunks.append(s)
    return chunks


def _trim(audio: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    """Drop leading/trailing near-silence."""
    loud = np.flatnonzero(np.abs(audio) > threshold)
    return audio[loud[0]:loud[-1] + 1] if loud.size else audio


def _generate(chunk: str) -> np.ndarray:
    words = max(1, len(re.findall(r"[\w.']+", chunk)))
    budget_s = SECONDS_PER_WORD * words + 1.5
    temperature = settings.tts_temperature
    for _ in range(2):
        wavs, _sr = _model.generate_voice_clone(
            text=chunk, language=settings.tts_language, voice_clone_prompt=_prompt,
            max_new_tokens=int(TOKENS_PER_S * budget_s), temperature=temperature,
        )
        audio = np.asarray(wavs[0], np.float32)
        if audio.size / MODEL_RATE < budget_s - 0.25:
            break
        temperature = min(temperature, 0.5)  # hit the cap: most likely a runaway, retry cooler
    return _trim(audio)


def _resample_int16(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear resample of a mono int16 signal. Good enough for a small speaker."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    dst_len = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
    resampled = np.interp(x_new, x_old, audio.astype(np.float32))
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def synthesize(text: str) -> bytes:
    """Render `text` in Rocky's voice to a complete WAV byte string at the configured output rate."""
    get_model()
    gap = np.zeros(int(0.18 * MODEL_RATE), np.float32)
    parts: list[np.ndarray] = []
    with _lock, torch.inference_mode():
        for chunk in _chunks(text):
            parts += [_generate(chunk), gap]
    speech = np.concatenate(parts[:-1]) if parts else np.zeros(1, np.float32)

    # End on silence. When playback finishes, the firmware's I2S DMA keeps looping whatever is left in its
    # buffer; a reply that ends mid-sound (we trim tightly above) then repeats as a "tatatata" until reset.
    # A 20 ms fade-out + 0.5 s of zeros makes that leftover buffer silent.
    fade = min(speech.size, int(0.02 * MODEL_RATE))
    speech[speech.size - fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    speech = np.concatenate([speech, np.zeros(int(TAIL_SILENCE_S * MODEL_RATE), np.float32)])

    audio = (np.clip(speech, -1.0, 1.0) * 32767).astype(np.int16)
    audio = _resample_int16(audio, MODEL_RATE, settings.output_sample_rate)

    out = io.BytesIO()
    with wave.open(out, "wb") as wav_out:
        wav_out.setnchannels(1)
        wav_out.setsampwidth(2)  # 16-bit
        wav_out.setframerate(settings.output_sample_rate)
        wav_out.writeframes(audio.tobytes())
    return out.getvalue()
