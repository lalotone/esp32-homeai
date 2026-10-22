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
import json
import logging
import math
import os
import tempfile
import wave
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import alerts, llm, songs, stt, telegram, tools, tts
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


# Wake-up alarms ring with the morning briefing. Composing it (tools + LLM + TTS of ~80 words) takes ~15 s, as long
# as the speaker's /pending timeout, so it's prepared ahead and /pending only hands over the audio.
ALARM_PREPARE_S = 120
_alarm_audio: dict[str, tuple[str, bytes]] = {}  # alert id -> (text, wav)


async def _prepare_alarms() -> None:
    for a in alerts.upcoming("alarm", ALARM_PREPARE_S):
        if a["id"] in _alarm_audio:
            continue
        try:
            async with httpx.AsyncClient() as client:
                briefing = await tools.get_morning_briefing(client)
            at = datetime.fromtimestamp(a["due"], ZoneInfo(settings.home_tz)).strftime("%H:%M")
            if isinstance(briefing.get("now"), dict):
                briefing["now"]["local_time"] = at  # composed ~2 min early: say the time it rings, not now
            text = await llm.chat(
                f"[Wake-up alarm ({a['label']}) ringing at {at}. Briefing data: {json.dumps(briefing, ensure_ascii=False)}]"
                " Wake the user up, Rocky style: start with \"Beep, beep, beep! Wake up, wake up, wake up!\", then a"
                " warm, short morning briefing as usual, using the time above. Under 80 words.", from_user=False)
            wav_bytes = await run_in_threadpool(tts.synthesize, text)
            _alarm_audio[a["id"]] = (text, wav_bytes)
            logger.info("alarm %s prepared for %s: %r", a["id"], at, text)
        except Exception as e:  # the plain "wake up" fallback still rings
            logger.warning("could not prepare alarm %s: %r", a["id"], e)


async def _alarm_loop() -> None:
    while True:
        await asyncio.sleep(20)
        await _prepare_alarms()


@app.on_event("startup")
async def start_alarm_loop() -> None:
    asyncio.get_running_loop().create_task(_alarm_loop())


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
        prepared = _alarm_audio.pop(alert["id"], None)
        if prepared:
            text, wav_bytes = prepared
        else:
            text = alerts.announcement(alert)
            wav_bytes = await run_in_threadpool(tts.synthesize, text)
        logger.info("announcing %s: %r", alert["kind"], text)
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
        # "What song is this?": this recording is the music sample the identify_song tool asked for, so it goes to
        # Shazam instead of the silence gate and Whisper (music isn't speech, and may be quiet).
        if songs.take_armed():
            result = await songs.recognize(data)
            sent = False
            if result.get("match") and telegram.configured():  # the song name on the phone, to find it later
                try:
                    await telegram.send(songs.phone_message(result))
                    sent = True
                except telegram.TelegramError as e:
                    logger.warning("song not sent to the phone: %s", e)
            reply = await llm.chat(songs.as_prompt(result, sent), from_user=False)
            wav_bytes = await run_in_threadpool(tts.synthesize, reply)
            return Response(content=wav_bytes, media_type="audio/wav",
                            headers={"X-Transcript": quote("(music)"), "X-Reply": quote(reply)})

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

        if songs.is_request(transcript):
            songs.arm()  # the next recording is the music sample, whether or not the model calls identify_song
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
