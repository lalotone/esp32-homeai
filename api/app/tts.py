"""Text-to-speech via the standalone Piper binary, resampled for the ESP32.

We shell out to the precompiled `piper` executable (see scripts/download_piper.sh)
rather than the piper-tts pip package, whose piper-phonemize dependency ships no
Linux wheels. Piper synthesizes at its voice's native rate (usually 22.05 kHz);
we downsample to `output_sample_rate` (16 kHz default) and emit 16-bit mono PCM.
"""
import io
import os
import subprocess
import tempfile
import wave

import numpy as np

from .config import settings


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
    """Render `text` to a complete WAV byte string at the configured output rate."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out_path = tmp.name

    try:
        proc = subprocess.run(
            [settings.piper_bin, "--model", settings.piper_model,
             "--output_file", out_path],
            input=text.encode("utf-8"),
            capture_output=True,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"piper exited {proc.returncode}: {err}")

        with wave.open(out_path, "rb") as wav_in:
            src_rate = wav_in.getframerate()
            frames = wav_in.readframes(wav_in.getnframes())
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)

    audio = np.frombuffer(frames, dtype=np.int16)
    audio = _resample_int16(audio, src_rate, settings.output_sample_rate)

    out = io.BytesIO()
    with wave.open(out, "wb") as wav_out:
        wav_out.setnchannels(1)
        wav_out.setsampwidth(2)  # 16-bit
        wav_out.setframerate(settings.output_sample_rate)
        wav_out.writeframes(audio.tobytes())
    return out.getvalue()
