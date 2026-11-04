"""Step-by-step mode: Rocky walks the user through a recipe or a repair, hands-free ("next", "back", "repeat").

The model writes the steps once (the start_guide tool). From then on the moving around is plain code, so "next" can't be
misunderstood and needs no model call. Questions still go to the model, which is told the current step. Stored as
STATE_DIR/guide.json: a restart or the night doesn't lose the place. The board keeps listening longer while a guide is
on (X-Guide: on), and after that "Hey Rocky, next" still works.
"""
import json
import logging
import os
import re
import threading
import time

from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_STEPS = 25
MAX_STEP_CHARS = 240
MAX_AGE_S = 24 * 3600  # a guide left alone for a day is over

_LEAD = r"(?:(?:ok|okay|good|yes|yeah|right|thanks|thank you|please|rocky|hey rocky|alright)\s+)*"
_TAIL = r"(?:\s+(?:please|rocky|thanks|thank you))*"
_NEXT = re.compile(rf"{_LEAD}(?:next(?: step| one)?|continue|go on|move on|keep going|what'?s next|what is next|done|"
                   rf"finished that|got it|ready){_TAIL}")
_BACK = re.compile(rf"{_LEAD}(?:back|go back|step back|previous(?: step| one)?|last step|the step before){_TAIL}")
_REPEAT = re.compile(rf"{_LEAD}(?:repeat(?: that| it| the step)?|again|say (?:that|it) again|one more time|what was that|"
                     rf"pardon|what(?:'?s| is) the step|read (?:it|that) again){_TAIL}")
_WHERE = re.compile(rf"{_LEAD}(?:what step(?: am i on| are we on| is this)?|where am i|where are we|how many(?: steps)? "
                    rf"(?:are )?left|how much(?: is)? left){_TAIL}")
_STOP = re.compile(rf"{_LEAD}(?:stop|stop (?:the )?(?:guide|steps)|end (?:the )?guide|cancel (?:the )?guide|"
                   rf"finish (?:the )?guide|we are done|we'?re done|i am done|i'?m done|that'?s enough|enough){_TAIL}")
_GOTO = re.compile(rf"{_LEAD}(?:go to |back to |skip to |jump to )?step (\w+){_TAIL}")
_WORDS = {w: i + 1 for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve thirteen "
                                         "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}

_lock = threading.Lock()
_state: dict = {}
_loaded = False


def _path() -> str:
    return os.path.join(settings.state_dir, "guide.json")


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


def _on() -> bool:
    """Call with the lock held."""
    _load()
    if _state and time.time() - _state.get("at", 0) > MAX_AGE_S:
        _state.clear()
        _save()
    return bool(_state.get("steps"))


def _say() -> str:
    i, steps = _state["i"], _state["steps"]
    return f"Step {i + 1} of {len(steps)}. {steps[i]}"


def active() -> bool:
    with _lock:
        return _on()


def start(title: str, steps: list[str] | str) -> dict:
    if isinstance(steps, str):  # the model sometimes sends one text instead of a list
        steps = steps.splitlines()
    clean = [re.sub(r"^\W*(?:step\s*)?\d*\W*", "", " ".join(str(s).split()), flags=re.I)[:MAX_STEP_CHARS] for s in steps]
    clean = [s for s in clean if s][:MAX_STEPS]
    if len(clean) < 2:
        return {"error": "give at least two short steps"}
    with _lock:
        _state.clear()
        _state.update(title=" ".join(str(title).split())[:80], steps=clean, i=0, at=time.time())
        _save()
        return {"started": _state["title"], "steps": len(clean), "say_only_this_now": _say()}


def handle(transcript: str) -> str | None:
    """The spoken reply to a guide command, or None if this isn't one (a question: the model takes it)."""
    text = re.sub(r"[^\w\s']", " ", transcript.lower().replace("’", "'"))
    text = " ".join(text.split())
    with _lock:
        if not _on():
            return None
        steps = _state["steps"]
        if _STOP.fullmatch(text):
            title = _state["title"]
            _state.clear()
            _save()
            logger.info("guide stopped: %s", title)
            return "Good. Guide finished."
        if _NEXT.fullmatch(text):
            if _state["i"] >= len(steps) - 1:
                _state.clear()
                _save()
                return "That was last step. Good, good, good!"
            _state["i"] += 1
        elif _BACK.fullmatch(text):
            if _state["i"] == 0:
                return "This is first step. " + _say()
            _state["i"] -= 1
        elif _WHERE.fullmatch(text):
            left = len(steps) - _state["i"] - 1
            return f"Step {_state['i'] + 1} of {len(steps)}. {left} left."
        elif _REPEAT.fullmatch(text):
            pass
        else:
            goto = _GOTO.fullmatch(text)
            if not goto:
                return None
            word = goto.group(1)
            n = int(word) if word.isdigit() else _WORDS.get(word, 0)
            if not 1 <= n <= len(steps):
                return f"No step {word}. Guide has {len(steps)} steps."
            _state["i"] = n - 1
        _state["at"] = time.time()
        _save()
        return _say()


def prompt_block() -> str:
    """For the system prompt, so a question ("how long was that?") is about the step they're on."""
    with _lock:
        if not _on():
            return ""
        i, steps = _state["i"], _state["steps"]
        return (f" A step-by-step guide is on: \"{_state['title']}\", now step {i + 1} of {len(steps)}: {steps[i]} Answer "
                "questions about this step briefly. Moving between steps is automatic: never say you moved.")
