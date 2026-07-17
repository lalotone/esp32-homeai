"""FastAPI service: WAV in -> STT -> LLM -> TTS -> WAV out.

The ESP32 records a WAV and POSTs it to /talk. Two upload styles are accepted:

  * raw body    -> Content-Type: audio/wav (or octet-stream), the WAV bytes as-is
                   (simplest for a microcontroller — no multipart to build)
  * multipart   -> form field `audio` = the WAV file (curl -F, browsers)

It replies with the spoken-answer WAV in the response body. Transcript and reply
text also come back as URL-encoded headers (X-Transcript / X-Reply) for debugging.
"""
import logging
import os
import tempfile
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import llm, stt, tts

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="ESP32 Voice Assistant API")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/talk")
async def talk(request: Request, audio: UploadFile | None = File(default=None)) -> Response:
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
        # STT and TTS are blocking/CPU-bound -> keep them off the event loop.
        transcript = await run_in_threadpool(stt.transcribe, in_path)
        logger.info("transcript=%r", transcript)
        if not transcript:
            raise HTTPException(status_code=422, detail="Could not transcribe audio")

        reply = await llm.chat(transcript)
        wav_bytes = await run_in_threadpool(tts.synthesize, reply)
    finally:
        os.unlink(in_path)

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            # URL-encoded so non-ASCII text can't break the header.
            "X-Transcript": quote(transcript),
            "X-Reply": quote(reply),
        },
    )
