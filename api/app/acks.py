"""Rocky's instant "looking, looking" while a slow answer is on its way.

An answer is one WAV, sent when it's complete, so a web search means ~8 s of silence. The board also opens GET /ack
next to its POST /talk, both with the same id, and this answers it as soon as Rocky has a reason to speak: a slow tool
started, or the question is taking longer than ACK_AFTER_S. A quick answer gets 204 and no clip at all.

The clips are made once and kept in STATE_DIR/acks, so a clip that sounds right stays the same across restarts. Delete
a file to have it made again.
"""
import asyncio
import contextvars
import logging
import os
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import settings

logger = logging.getLogger("uvicorn.error")

CLIPS = {
    "look": ("Looking, looking.", "I look now."),
    "search": ("Searching, searching.", "I search now, friend."),
    "think": ("Thinking, thinking.", "Work, work, work."),
}
TOOL_CLIP = {
    "look_through_camera": "look", "look_at_last_photo": "look", "zoom_last_photo": "look",
    "add_photo_item_to_shopping_list": "look", "web_search": "search",
}
MAX_SLOTS = 8  # a board that opens /ack and never sends the question leaves its slot until the wait ends


@dataclass
class _Slot:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False  # the question arrived
    done: bool = False     # the answer is complete: too late for a clip
    kind: str | None = None


_slots: dict[str, _Slot] = {}
_wav: dict[str, bytes] = {}
_last: dict[str, str] = {}
# A contextvar, not module state: only the /talk turn that opened it counts, not the Live View watcher or an alarm's
# briefing running tools at the same moment.
_current: contextvars.ContextVar[_Slot | None] = contextvars.ContextVar("ack_slot", default=None)


def _slot(turn: str) -> _Slot:
    while len(_slots) >= MAX_SLOTS and turn not in _slots:
        _slots.pop(next(iter(_slots)))
    return _slots.setdefault(turn, _Slot())


def open_turn(turn: str | None) -> None:
    """The question arrived (turn: the board's X-Ack-Id, None from a client that doesn't ask for clips)."""
    if turn:
        slot = _slot(turn)
        slot.started = True
        _current.set(slot)


def close_turn(turn: str | None) -> None:
    """The answer is complete."""
    if turn:
        _current.set(None)
        slot = _slots.get(turn)
        if slot:
            slot.done = True
            slot.event.set()


def tool_started(name: str) -> None:
    slot = _current.get()
    kind = TOOL_CLIP.get(name)
    if slot and kind and not slot.kind:
        slot.kind = kind
        slot.event.set()


def _pick(kind: str) -> bytes | None:
    texts = [t for t in CLIPS[kind] if t in _wav]
    if not texts:
        return None
    text = random.choice([t for t in texts if t != _last.get(kind)] or texts)  # not the same one twice in a row
    _last[kind] = text
    return _wav[text]


async def wait(turn: str) -> bytes | None:
    """GET /ack: the clip to play now, or None if the answer was quick (or the question never came)."""
    slot = _slot(turn)
    if settings.ack_after_s <= 0:
        return None
    try:
        await asyncio.wait_for(slot.event.wait(), settings.ack_after_s)
    except TimeoutError:
        if slot.started and not slot.done and not slot.kind:
            slot.kind = "think"
    finally:
        _slots.pop(turn, None)
    return None if slot.done or not slot.kind else _pick(slot.kind)


def prepare(synthesize: Callable[[str], bytes]) -> None:
    """Load the clips, making the missing ones. A clip that fails is only a missing "looking, looking"."""
    if settings.ack_after_s <= 0:
        return
    folder = os.path.join(settings.state_dir, "acks")
    for text in (t for texts in CLIPS.values() for t in texts):
        path = os.path.join(folder, re.sub(r"\W+", "_", text).strip("_").lower() + ".wav")
        try:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    _wav[text] = f.read()
                continue
            _wav[text] = synthesize(text)
            os.makedirs(folder, exist_ok=True)
            with open(path, "wb") as f:
                f.write(_wav[text])
        except Exception as e:
            logger.warning("acknowledgment %r not ready: %r", text, e)
    logger.info("%d acknowledgment clips ready", len(_wav))
