"""Timers and reminders: set by the LLM's tools, announced by the speaker when due.

The ESP32 can only talk when asked, so it polls GET /pending while idle; when an alert is due the
API answers with Rocky's spoken announcement instead of 204. Alerts are kept in a small JSON file
(STATE_DIR/alerts.json) so a pod restart doesn't lose them; one that came due while nobody polled
(restart, speaker off) is still announced on the next poll, up to STALE_AFTER_S late.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_ALERTS = 20
STALE_AFTER_S = 6 * 3600   # a reminder more than 6 h overdue is dropped instead of announced out of the blue

# Weekdays (Mon=0) a repeating alarm rings on; "once" alerts are deleted when they fire.
REPEATS = {"once": None, "daily": {0, 1, 2, 3, 4, 5, 6}, "weekdays": {0, 1, 2, 3, 4}, "weekends": {5, 6}}

_lock = threading.Lock()
_alerts: list[dict] = []   # {"id", "kind": "timer"|"reminder"|"alarm", "label", "due": epoch s, "repeat", ...}
_loaded = False


def _path() -> str:
    return os.path.join(settings.state_dir, "alerts.json")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_path()) as f:
            _alerts.extend(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("could not read %s: %r", _path(), e)


def _save() -> None:
    try:
        os.makedirs(settings.state_dir, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_alerts, f)
        os.replace(tmp, _path())
    except OSError as e:
        logger.warning("could not save %s: %r", _path(), e)


def _describe(a: dict, tz) -> dict:
    due = datetime.fromtimestamp(a["due"], tz)
    left = max(0, int(a["due"] - time.time()))
    return {"kind": a["kind"], "label": a["label"], "at": due.strftime("%a %H:%M"), "to_phone": a.get("phone", False),
            "repeat": a.get("repeat", "once"),
            "in": f"{left // 3600}h {left % 3600 // 60}m {left % 60}s" if left >= 3600 else f"{left // 60}m {left % 60}s"}


def _on_allowed_day(due: float, repeat: str, tz) -> float:
    """`due`, or the same local time on the next day the repeat allows (DST-safe: rebuilt from the local clock)."""
    days = REPEATS.get(repeat)
    local = datetime.fromtimestamp(due, tz)
    for _ in range(8):
        if days is None or local.weekday() in days:
            return local.timestamp()
        local = datetime.combine(local.date() + timedelta(days=1), dtime(local.hour, local.minute), tzinfo=tz)
    return local.timestamp()


def _next_occurrence(a: dict, tz) -> float:
    local = datetime.fromtimestamp(a["due"], tz)
    nxt = datetime.combine(local.date() + timedelta(days=1), dtime(local.hour, local.minute), tzinfo=tz)
    return _on_allowed_day(nxt.timestamp(), a["repeat"], tz)


def _reschedule(a: dict, tz) -> None:
    """Put a repeating alert back at its next occurrence (caller holds the lock)."""
    now = time.time()
    nxt = dict(a, id=uuid.uuid4().hex[:8], phone_sent=False)
    nxt["due"] = _next_occurrence(a, tz)
    while nxt["due"] <= now:  # several missed while the API was down: skip to the next one ahead
        nxt["due"] = _next_occurrence(nxt, tz)
    _alerts.append(nxt)


def add(kind: str, label: str, due: float, tz, phone: bool = False, repeat: str = "once") -> dict:
    if repeat not in REPEATS:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
    with _lock:
        _load()
        if len(_alerts) >= MAX_ALERTS:
            raise RuntimeError(f"already {MAX_ALERTS} timers and reminders")
        a = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label.strip()[:80] or kind,
             "due": _on_allowed_day(due, repeat, tz), "repeat": repeat,
             "created": time.time(), "phone": phone, "phone_sent": False}
        _alerts.append(a)
        _save()
    logger.info("alert set: %s", a)
    return _describe(a, tz)


def listing(tz) -> list[dict]:
    with _lock:
        _load()
        return [_describe(a, tz) for a in sorted(_alerts, key=lambda a: a["due"])]


def cancel(label: str | None, tz, kind: str | None = None) -> list[dict]:
    """Cancel the alerts whose label contains `label` (case-insensitive; empty = any), of `kind` if given."""
    with _lock:
        _load()
        needle = (label or "").strip().lower()
        gone = [a for a in _alerts if (not needle or needle in a["label"].lower()) and (not kind or a["kind"] == kind)]
        for a in gone:
            _alerts.remove(a)
        if gone:
            _save()
    return [_describe(a, tz) for a in gone]


def pop_due() -> dict | None:
    """The oldest due alert, removed from the list (None when nothing is due). A repeating one is put back at its
    next occurrence."""
    now = time.time()
    tz = ZoneInfo(settings.home_tz)
    with _lock:
        _load()
        for a in [a for a in _alerts if now - a["due"] > STALE_AFTER_S]:
            logger.info("dropping stale alert: %s", a)
            _alerts.remove(a)
            if a.get("repeat", "once") != "once":
                _reschedule(a, tz)
        due = sorted((a for a in _alerts if a["due"] <= now), key=lambda a: a["due"])
        if not due:
            _save()
            return None
        _alerts.remove(due[0])
        if due[0].get("repeat", "once") != "once":
            _reschedule(due[0], tz)
        _save()
        return due[0]


def upcoming(kind: str, within_s: float) -> list[dict]:
    """Alerts of `kind` due within the next `within_s` seconds (not yet due)."""
    now = time.time()
    with _lock:
        _load()
        return [dict(a) for a in _alerts if a["kind"] == kind and now < a["due"] <= now + within_s]


def phone_due() -> list[dict]:
    """Due alerts that should also go to the phone and haven't yet (whether or not the speaker announced them)."""
    now = time.time()
    with _lock:
        _load()
        return [dict(a) for a in _alerts if a.get("phone") and not a.get("phone_sent") and a["due"] <= now]


def mark_phone_sent(alert_id: str) -> None:
    with _lock:
        for a in _alerts:
            if a["id"] == alert_id:
                a["phone_sent"] = True
                _save()


def announcement(a: dict) -> str:
    """What Rocky says when it fires. Fixed text (no LLM round trip): the speaker should talk right away."""
    if a["kind"] == "timer":
        label = a["label"] if a["label"] != "timer" else ""
        return f"Beep, beep, beep! {label + ' timer' if label else 'Timer'} done. Time, time, time!"
    if a["kind"] == "alarm":  # fallback only: alarms normally ring with a briefing prepared ahead (main.py)
        return f"Beep, beep, beep! Wake up, wake up, wake up! It is {datetime.fromtimestamp(a['due'], ZoneInfo(settings.home_tz)):%H:%M}."
    return f"Rocky reminder: {a['label']}. Rocky remember for you, good, good, good."


def due_at(hhmm: str, day_offset: int, tz) -> float:
    """Epoch seconds for HH:MM local time, `day_offset` days from today (rolls to tomorrow if already past)."""
    # The model writes times as "07:30", "7:30 AM", "22.42" or just "7": accept them all.
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.h](\d{2}))?\s*([ap])?\.?\s*m?\.?\s*", hhmm.lower())
    if not m:
        raise ValueError(f"time not understood: {hhmm!r} (want HH:MM)")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "p" and hour < 12:
        hour += 12
    elif m.group(3) == "a" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"not a time of day: {hhmm!r}")
    now = datetime.now(tz)
    due = (now + timedelta(days=day_offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now and day_offset == 0:
        due += timedelta(days=1)
    return due.timestamp()
