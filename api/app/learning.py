"""Rocky learns from his mistakes, in two ways.

  * Right away: "that was wrong" gets a question back, and the user's correction is saved as a note (memory.py) that goes
    in every prompt from then on. They can hear it ("what do you remember?") and remove it ("forget...") like any note.
  * Later: every complaint, and every failure he notices himself (an answer refused as a false claim, a tool error), goes
    to STATE_DIR/feedback.jsonl, to find what the code or the prompts should do better. It stays on this machine.
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_LOG_BYTES = 512 * 1024  # roughly the last few thousand entries: past this, the older half goes

_WRONG = re.compile(r"^\W*(?:no\W+|nope\W+|rocky\W+|hey rocky\W+|hmm\W+|sorry\W+)*(?:"
                    r"that(?:'?s| was| is)(?: not right| wrong| incorrect| not correct| not true| not it)|"
                    r"you(?:'re| are| were| got (?:that|it)) wrong|wrong answer|you misheard|you heard wrong|"
                    r"not what i (?:asked|said|meant))\b\W*(.*)$", re.I | re.S)
_NEVER_MIND = re.compile(r"^\W*(never ?mind|forget it|nothing|no matter|leave it|it doesn'?t matter|no)\W*$", re.I)
ASK_WHAT = "Sorry, sorry. What is right, question?"
NO_CHANGE = "Good. No change."
# A correction needs words: "that was wrong, sorry" has nothing to learn from.
_MIN_CORRECTION_WORDS = 2

_lock = threading.Lock()
_awaiting = False


def _path() -> str:
    return os.path.join(settings.state_dir, "feedback.jsonl")


def log(kind: str, **fields) -> None:
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **fields}
    with _lock:
        try:
            os.makedirs(settings.state_dir, exist_ok=True)
            with open(_path(), "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if os.path.getsize(_path()) > MAX_LOG_BYTES:
                with open(_path()) as f:
                    lines = f.readlines()
                tmp = _path() + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(lines[len(lines) // 2:])
                os.replace(tmp, _path())
        except OSError as e:
            logger.warning("could not write %s: %r", _path(), e)


@dataclass
class Turn:
    reply: str | None = None   # answer with this, the model isn't needed
    correcting: bool = False   # the model takes it, and saves what it learns


def on_turn(transcript: str, last: dict) -> Turn:
    """What this turn means for learning. `last` is the exchange the user may be complaining about."""
    global _awaiting
    if _awaiting:
        _awaiting = False
        if _NEVER_MIND.match(transcript):
            return Turn(reply=NO_CHANGE)
        log("correction", said=transcript, about=last)
        return Turn(correcting=True)
    wrong = _WRONG.match(transcript)
    if not wrong:
        return Turn()
    rest = wrong.group(1).strip()
    log("said_wrong", said=transcript, about=last)
    if len(rest.split()) >= _MIN_CORRECTION_WORDS:
        return Turn(correcting=True)
    _awaiting = True
    return Turn(reply=ASK_WHAT)
