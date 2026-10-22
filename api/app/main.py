"""FastAPI service: WAV in -> STT -> LLM -> TTS -> WAV out.

The ESP32 records a WAV and POSTs it to /talk. Two upload styles are accepted:

  * raw body    -> Content-Type: audio/wav (or octet-stream), the WAV bytes as-is
                   (simplest for a microcontroller — no multipart to build)
  * multipart   -> form field `audio` = the WAV file (curl -F, browsers)

It replies with the spoken-answer WAV in the response body. Transcript and reply
text also come back as URL-encoded headers (X-Transcript / X-Reply) for debugging.
"""
import asyncio
import io
import logging
import math
import os
import tempfile
import wave
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import alerts, llm, stt, telegram, tts
from .config import settings

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="ESP32 Voice Assistant API")


@app.on_event("startup")
def load_voice() -> None:
    # Loading Qwen3-TTS + encoding Rocky's voice prompt takes ~30 s on a CPU; do it before
    # the first /talk instead of inside it, where it would eat into the ESP32's timeout.
    tts.get_model()
    stt.warmup()
    logger.info("Rocky voice loaded")


# How many /talk requests were answered vs. ignored as silence (see /talk's
# silence gate). The firmware always uploads after a wake, so the API is the
# single place that tracks "woke but said nothing".
_talk_counts = {"answered": 0, "silence": 0}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "counters": dict(_talk_counts)}


@app.get("/stats")
def stats() -> dict:
    return dict(_talk_counts)


_busy = asyncio.Lock()


class _QuietPolls(logging.Filter):
    """The speaker polls /pending every few seconds: keep those out of the access log."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/pending" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_QuietPolls())


PHONE_POLL_S = 20


async def _send_phone_reminders() -> None:
    for a in alerts.phone_due():
        try:
            await telegram.send(f"⏰ {a['label']}" if a["kind"] == "reminder" else f"⏰ {a['label']} timer done")
            alerts.mark_phone_sent(a["id"])
            logger.info("sent %s to the phone: %r", a["kind"], a["label"])
        except telegram.TelegramError as e:
            logger.warning("phone reminder not sent (will retry): %s", e)


async def _phone_loop() -> None:
    """Reminders "on my phone" go out from here, so they arrive even when the speaker is off or away."""
    while True:
        await asyncio.sleep(PHONE_POLL_S)
        await _send_phone_reminders()


@app.on_event("startup")
async def start_phone_loop() -> None:
    if telegram.configured():
        asyncio.get_running_loop().create_task(_phone_loop())


@app.get("/pending")
async def pending() -> Response:
    """Polled by the ESP32 while idle: Rocky's announcement of a due timer/reminder as a WAV, or 204 if none.

    The speaker can't talk on its own, so this is how timers and reminders reach it."""
    if _busy.locked():
        return Response(status_code=204)  # busy answering a question: the next poll gets it
    async with _busy:
        await _send_phone_reminders()  # due phone reminders go out now too, before the speaker drops them
        alert = alerts.pop_due()
        if alert is None:
            return Response(status_code=204)
        text = alerts.announcement(alert)
        logger.info("announcing %s: %r", alert["kind"], text)
        wav_bytes = await run_in_threadpool(tts.synthesize, text)
    return Response(content=wav_bytes, media_type="audio/wav", headers={"X-Reply": quote(text)})


@app.post("/talk")
async def talk(request: Request, audio: UploadFile | None = File(default=None)) -> Response:
    # One question at a time. A CPU-only Rocky voice takes ~1 min per answer; if the ESP32 gives up and asks
    # again, stacking a second Whisper + TTS run on top of the first doubles memory and gets the whole
    # process OOM-killed. Answer "busy" right away instead.
    if _busy.locked():
        logger.warning("POST /talk rejected: still answering the previous request")
        raise HTTPException(status_code=503, detail="Busy answering the previous request")
    async with _busy:
        return await _talk(request, audio)


def _input_rms_dbfs(path: str) -> float:
    """RMS of a 16-bit PCM WAV in dBFS (0 dBFS = full scale).

    -inf means "no usable speech level" -- the file isn't 16-bit PCM, is empty,
    or is digitally silent. The silence gate treats -inf as "nothing said".
    """
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                return -math.inf
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    except (wave.Error, EOFError):
        return -math.inf
    if pcm.size == 0:
        return -math.inf
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    if rms <= 0:
        return -math.inf
    return 20.0 * math.log10(rms / 32768.0)


def _silence_wav(seconds: float) -> bytes:
    """A short all-silence WAV at the output rate, so the ESP32 has a valid
    (inaudible) reply to play when nothing was said."""
    n = int(seconds * settings.output_sample_rate)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(settings.output_sample_rate)
        w.writeframes(b"\x00\x00" * n)
    return out.getvalue()


def _silence_response() -> Response:
    """The "woke but said nothing" reply: a silent WAV + marker headers."""
    return Response(
        content=_silence_wav(settings.silence_reply_seconds),
        media_type="audio/wav",
        headers={
            # URL-encoded so non-ASCII text can't break the header.
            "X-Transcript": quote(""),
            "X-Reply": quote("silence"),
        },
    )


async def _talk(request: Request, audio: UploadFile | None) -> Response:
    # Prefer the multipart field if present; otherwise take the raw request body.
    if audio is not None:
        data = await audio.read()
    else:
        data = await request.body()

    ctype = request.headers.get("content-type", "")
    logger.info("POST /talk  content-type=%r  bytes=%d", ctype, len(data))

    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(data)
        in_path = tmp.name

    try:
        # Silence gate: the firmware uploads whatever it captured (even pure
        # silence), so the API is the place that decides "nothing was said".
        dbfs = await run_in_threadpool(_input_rms_dbfs, in_path)
        logger.info("input rms=%.1f dBFS (silence threshold %.1f dBFS)", dbfs, settings.silence_threshold_dbfs)
        if dbfs < settings.silence_threshold_dbfs:
            _talk_counts["silence"] += 1
            logger.info("Silent input: skipping STT/LLM/TTS (silence #%d)", _talk_counts["silence"])
            return _silence_response()

        # STT and TTS are blocking/CPU-bound -> keep them off the event loop.
        transcript = await run_in_threadpool(stt.transcribe, in_path)
        logger.info("transcript=%r", transcript)
        if not transcript:
            # Audio present but nothing recognizable -- treat the same as silence.
            _talk_counts["silence"] += 1
            logger.info("Empty transcript: skipping STT/LLM/TTS (silence #%d)", _talk_counts["silence"])
            return _silence_response()

        reply = await llm.chat(transcript)
        wav_bytes = await run_in_threadpool(tts.synthesize, reply)
    finally:
        os.unlink(in_path)

    _talk_counts["answered"] += 1
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            # URL-encoded so non-ASCII text can't break the header.
            "X-Transcript": quote(transcript),
            "X-Reply": quote(reply),
        },
    )
