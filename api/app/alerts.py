"""Timers and reminders: set by the LLM's tools, announced by the speaker when due.

The ESP32 can only talk when asked, so it polls GET /pending while idle; when an alert is due the
API answers with Rocky's spoken announcement instead of 204. Alerts are kept in a small JSON file
(STATE_DIR/alerts.json) so a pod restart doesn't lose them; one that came due while nobody polled
(restart, speaker off) is still announced on the next poll, up to STALE_AFTER_S late.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_ALERTS = 20
STALE_AFTER_S = 6 * 3600   # a reminder more than 6 h overdue is dropped instead of announced out of the blue

_lock = threading.Lock()
_alerts: list[dict] = []   # {"id", "kind": "timer"|"reminder", "label", "due": epoch s, "created": epoch s}
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
            "in": f"{left // 3600}h {left % 3600 // 60}m {left % 60}s" if left >= 3600 else f"{left // 60}m {left % 60}s"}


def add(kind: str, label: str, due: float, tz, phone: bool = False) -> dict:
    with _lock:
        _load()
        if len(_alerts) >= MAX_ALERTS:
            raise RuntimeError(f"already {MAX_ALERTS} timers and reminders")
        a = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label.strip()[:80] or kind, "due": due,
             "created": time.time(), "phone": phone, "phone_sent": False}
        _alerts.append(a)
        _save()
    logger.info("alert set: %s", a)
    return _describe(a, tz)


def listing(tz) -> list[dict]:
    with _lock:
        _load()
        return [_describe(a, tz) for a in sorted(_alerts, key=lambda a: a["due"])]


def cancel(label: str | None, tz) -> list[dict]:
    """Cancel the alerts whose label contains `label` (case-insensitive), or all when it's empty."""
    with _lock:
        _load()
        needle = (label or "").strip().lower()
        gone = [a for a in _alerts if not needle or needle in a["label"].lower()]
        for a in gone:
            _alerts.remove(a)
        if gone:
            _save()
    return [_describe(a, tz) for a in gone]


def pop_due() -> dict | None:
    """The oldest due alert, removed from the list (None when nothing is due)."""
    now = time.time()
    with _lock:
        _load()
        for a in [a for a in _alerts if now - a["due"] > STALE_AFTER_S]:
            logger.info("dropping stale alert: %s", a)
            _alerts.remove(a)
        due = sorted((a for a in _alerts if a["due"] <= now), key=lambda a: a["due"])
        if not due:
            return None
        _alerts.remove(due[0])
        _save()
        return due[0]


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
    return f"Rocky reminder: {a['label']}. Rocky remember for you, good, good, good."


def due_at(hhmm: str, day_offset: int, tz) -> float:
    """Epoch seconds for HH:MM local time, `day_offset` days from today (rolls to tomorrow if already past)."""
    hour, minute = (int(x) for x in hhmm.strip().split(":"))
    now = datetime.now(tz)
    due = (now + timedelta(days=day_offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now and day_offset == 0:
        due += timedelta(days=1)
    return due.timestamp()
