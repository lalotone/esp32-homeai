"""The shopping list Rocky keeps: add, remove, read, clear (and undo the last clear).

Stored as STATE_DIR/shopping.json next to the timers, so it survives restarts. Items are short free
text ("2 litres of milk"); adding one that's already there (case-insensitive) is a no-op. Clearing
keeps the cleared items once, so a misheard "clear the list" can be undone.
"""
import json
import logging
import os
import threading

from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_ITEMS = 100

_lock = threading.Lock()
_state = {"items": [], "last_cleared": []}
_loaded = False


def _path() -> str:
    return os.path.join(settings.state_dir, "shopping.json")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_path()) as f:
            _state.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning("could not read %s: %r", _path(), e)


def _save() -> None:
    try:
        os.makedirs(settings.state_dir, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, _path())
    except OSError as e:
        logger.warning("could not save %s: %r", _path(), e)


def _clean(items: list[str]) -> list[str]:
    return [i.strip()[:60] for i in items if i and i.strip()]


def add(items: list[str]) -> dict:
    with _lock:
        _load()
        have = {i.lower() for i in _state["items"]}
        added = []
        for item in _clean(items):
            if item.lower() not in have and len(_state["items"]) < MAX_ITEMS:
                _state["items"].append(item)
                have.add(item.lower())
                added.append(item)
        if added:
            _save()
        return {"added": added, "list": list(_state["items"])}


def remove(items: list[str]) -> dict:
    """Remove items matching the given text (exact or contained, case-insensitive: "milk" removes "2 milk")."""
    with _lock:
        _load()
        removed = []
        for want in (w.lower() for w in _clean(items)):
            match = next((i for i in _state["items"] if i.lower() == want), None) or \
                next((i for i in _state["items"] if want in i.lower()), None)
            if match:
                _state["items"].remove(match)
                removed.append(match)
        if removed:
            _save()
        return {"removed": removed, "not_found": [i for i in _clean(items) if not any(i.lower() in r.lower() for r in removed)],
                "list": list(_state["items"])}


def items() -> list[str]:
    with _lock:
        _load()
        return list(_state["items"])


def clear() -> dict:
    with _lock:
        _load()
        cleared = list(_state["items"])
        if cleared:
            _state["last_cleared"] = cleared
            _state["items"] = []
            _save()
        return {"cleared": cleared}


def undo_clear() -> dict:
    with _lock:
        _load()
        back = [i for i in _state["last_cleared"] if i.lower() not in {x.lower() for x in _state["items"]}]
        _state["items"] = back + _state["items"]
        _state["last_cleared"] = []
        _save()
        return {"restored": back, "list": list(_state["items"])}
