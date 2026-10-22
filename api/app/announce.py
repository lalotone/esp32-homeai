"""Things the speaker should say on its own, besides alarms/timers/reminders (alerts.py): e.g. the answer to a photo.

Synthesized when queued, so GET /pending hands the speaker ready audio instantly. Small and in memory only: if the
speaker is away, at most MAX_QUEUED wait, and each is dropped after MAX_AGE_S (stale news isn't worth saying).
Quiet hours (QUIET_HOURS, e.g. "23-8") keep these off the speaker at night; alarms and timers aren't affected.
"""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import tts
from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_QUEUED = 3
MAX_AGE_S = 15 * 60

_queue: list[tuple[float, str, bytes]] = []  # (queued at, text, wav)


def quiet_now() -> bool:
    """True inside QUIET_HOURS ("start-end", local hours, may wrap past midnight). Empty = never quiet."""
    try:
        start, end = (int(h) for h in settings.quiet_hours.split("-"))
    except ValueError:
        return False
    hour = datetime.now(ZoneInfo(settings.home_tz)).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


async def add(text: str) -> bool:
    """Queue `text` for the speaker. False if it's quiet hours (the caller can say so)."""
    if quiet_now():
        logger.info("quiet hours: not queued for the speaker")
        return False
    wav = await asyncio.to_thread(tts.synthesize, text)
    _queue.append((time.monotonic(), text, wav))
    del _queue[:-MAX_QUEUED]
    return True


def pop() -> tuple[str, bytes] | None:
    while _queue:
        queued, text, wav = _queue.pop(0)
        if time.monotonic() - queued < MAX_AGE_S:
            return text, wav
    return None


def short(text: str, max_words: int = 45) -> str:
    """The first sentences of `text` that fit in about `max_words` (the full text goes to Telegram)."""
    out, words = [], 0
    for sentence in text.replace("\n", " ").replace("**", "").split(". "):
        n = len(sentence.split())
        if out and words + n > max_words:
            break
        out.append(sentence.strip())
        words += n
    s = ". ".join(out).strip()
    return s if s.endswith((".", "!", "?")) else s + "."
