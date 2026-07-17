"""Speech-to-text via faster-whisper.

The model is loaded lazily on first use and cached for the process lifetime.
`transcribe` is a blocking CPU call — the API layer runs it in a threadpool.
"""
from faster_whisper import WhisperModel

from .config import settings

_model: WhisperModel | None = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    return _model


def transcribe(wav_path: str) -> str:
    """Return the recognized text for a WAV file (any rate; whisper resamples)."""
    model = get_model()
    segments, _info = model.transcribe(
        wav_path,
        language=settings.whisper_language or None,
        vad_filter=True,  # drop silence/noise so empty recordings don't hallucinate
    )
    return " ".join(seg.text.strip() for seg in segments).strip()
