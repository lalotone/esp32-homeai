"""Speech-to-text via faster-whisper.

The model is loaded lazily on first use and cached for the process lifetime.
`transcribe` is a blocking CPU call — the API layer runs it in a threadpool.
"""
import wave

import numpy as np
from faster_whisper import WhisperModel

from .config import settings

_model: WhisperModel | None = None

WHISPER_RATE = 16000


def _load_pcm_wav(wav_path: str) -> np.ndarray | None:
    """Decode a 16-bit PCM WAV to the float32 16 kHz mono array whisper wants.

    Passing samples skips faster-whisper's PyAV decoder: faster-whisper 1.2.1 calls
    av.open(..., metadata_errors=...), which PyAV 19 no longer accepts, and requirements.txt
    doesn't pin av. Returns None for anything that isn't 16-bit PCM WAV so the caller can
    fall back to the file path.
    """
    try:
        with wave.open(wav_path, "rb") as w:
            if w.getsampwidth() != 2:
                return None
            rate, channels = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError):
        return None
    audio = pcm.reshape(-1, channels).mean(axis=1) / 32768.0
    if rate != WHISPER_RATE and audio.size:
        n = int(round(audio.size * WHISPER_RATE / rate))
        audio = np.interp(np.linspace(0, audio.size, n, endpoint=False), np.arange(audio.size), audio)
    return audio.astype(np.float32)


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    return _model


def warmup() -> None:
    """One throwaway pass at startup: the first CUDA transcription is ~10 s slower than the rest."""
    noise = (np.random.default_rng(0).standard_normal(WHISPER_RATE) * 0.01).astype(np.float32)
    segments, _info = get_model().transcribe(noise, language=settings.whisper_language or None, vad_filter=False)
    list(segments)  # transcribe() is lazy: consuming the segments runs it


def transcribe(wav_path: str) -> str:
    """Return the recognized text for a WAV file (any rate; whisper resamples)."""
    model = get_model()
    audio = _load_pcm_wav(wav_path)
    segments, _info = model.transcribe(
        wav_path if audio is None else audio,
        language=settings.whisper_language or None,
        vad_filter=True,  # drop silence/noise so empty recordings don't hallucinate
        # Names it should expect: an English Whisper otherwise mishears local names ("Zaragoza" as "Maragona").
        initial_prompt=settings.whisper_prompt or None,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()
