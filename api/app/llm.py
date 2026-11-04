"""Chat completion against any OpenAI-compatible endpoint, with tools.

Works with Ollama (/v1), llama.cpp's llama-server, LM Studio, vLLM, etc.
Configure LLM_BASE_URL / LLM_MODEL / LLM_API_KEY in the environment.

With TOOLS_ENABLED (default), the model can call the tools in tools.py (time, weather, news, math,
Wikipedia): each round runs the requested calls in parallel and sends the results back, until the
model answers in plain text. A question that needs no tool costs one round, same as before.
"""
import asyncio
import json
import logging
import re
import time

import httpx

from . import guardian, guide, learning, memory, photos, tools
from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_TOOL_ROUNDS = 3  # a voice question needs one, rarely two (e.g. weather + time); stop loops early

MAX_TOKENS = 400  # a spoken reply or a tool call is far shorter; stops a runaway generation (seen: 2000+ tokens)

# Requests that change something. With the full prompt and ~20 tools the model sometimes answers "alarm set" /
# "added" without calling anything (3 of 5 alarm requests in testing; tool_choice="required" isn't enforced by
# llama.cpp with this template). So: an action request that ends with no tool call gets one corrective nudge, and a
# success claimed without any tool is replaced by an honest "didn't work".
_ACTION = re.compile(r"\b(wake me|alarm|timer|remind|add|put|remove|delete|clear|cancel|send|set|undo)\b", re.I)
_CLAIM = re.compile(r"\b(set|added|sent|cancel+ed|removed|cleared|done|ready|on (the|your) list|will wake|saved|noted|"
                    r"remember(ed)?|forgotten|forgot)\b", re.I)
_NUDGE = ("[Check: you called no tool, so nothing was set, added, sent or cancelled. If this request needs a tool, "
          "call it now. If you really must ask the user something first, reply with only that question.]")
_DIDNT_WORK = "Hmm, Rocky could not do that. Say it again, question?"
# ...but "how do I add salt to pasta water?" asks for knowledge, not an action: its answer ("pasta ready in ten
# minutes") was refused as a false claim. "Can you add milk?" is still a request, and so is anything about the user's
# own alarms or list ("how long is left on my timer?"): an answer to those has to come from a tool. Bench questions
# ("what happens if I put this in backwards?") are knowledge too; Live View's answers to them say "set" and "done".
_KNOWLEDGE = re.compile(r"^\W*(why\b|how (do|does|can|could|should|would|to)\b|"
                        r"how (much|many)\b(?!.*\b(alarm|timer|reminder)s?\b)|"
                        r"(what|when|where|which)\b.{0,30}\b(do|does|can|could|should) (i|we|you|one|people)\b(?! have)|"
                        r"(should|do) (i|we)\b(?! have)|does\b|is it (ok|okay|safe|bad|better|possible|normal|true)\b|"
                        r"what (happens|if)\b|will (it|this|that)\b|"
                        r"is (this|that)\b(?!.*\b(alarm|timer|reminder)s?\b)|"
                        r"(what'?s|what is|which is) (the )?best\b|"
                        r"((can|could|would) you |please |look at (this|that) and )?"
                        r"(tell me|show me|explain|teach me)\b.{0,12}\b(how|where|what|which)\b"
                        r"(?!.*\b(alarm|timer|reminder)s?\b))",
                        re.I)
_OWN_STATE = re.compile(r"\b(my|our) (\w+ )?(alarm|timer|reminder|list)|\bthe (shopping )?list\b", re.I)
# "Remember that..." / "forget the coffee thing". Only these turns get the tools that change the memory, and "what do
# you remember?" isn't one of them: the saved facts are already in the prompt.
_MEMORY_SAVE = re.compile(r"\b(remember\b(?! (when|how|what|if|the time))|keep in mind|(make|take) a note|note (that|down)|"
                          r"don'?t forget|do not forget)\b", re.I)
_MEMORY_DROP = re.compile(r"(?<!don't )(?<!dont )(?<!do not )(?<!\bi )\bforget\b|\bstop remembering\b|"
                          r"\b(delete|erase|remove)\b.{0,40}\b(memory|memories|note|notes)\b", re.I)
_MEMORY_UNDO = re.compile(r"\b(undo|bring (it|that|them) back|remember (it|that|them) again)\b", re.I)
_QUESTION = re.compile(r"^\W*((hey )?rocky\W+)?(what|do|does|did|how|why|when|where|which|who)\b", re.I)
# "Walk me through making pancakes": only these turns get start_guide, and the longer answer its steps need.
_GUIDE_ASK = re.compile(r"\b(step by step|step-by-step|walk me through|talk me through|take me through|guide me|"
                        r"one step at a time)\b", re.I)
GUIDE_MAX_TOKENS = 1000
_CORRECTING_HINT = (" The user just corrected your last answer. Save what you learned with the remember tool, as one short "
                    "fact starting with \"Correction:\", then say briefly what you learned.")
# A tool call the server couldn't parse comes back as the reply's text (seen with Gemma 4 after a failed call:
# "<|tool_call>call:calculate{...}"). Never spoken: the round is asked again once, then _DIDNT_WORK.
_MARKUP = re.compile(r"<\|?/?tool_call|<\|\"\|>|\bcall:\w+\{")
# A reply about the weather with no weather tool this turn is invented or an unkept "I check" (seen: "Rain tonight, so
# run bad" with no lookup). Nudging the model doesn't fix it reliably, so the API fetches the weather itself and asks
# again with the real data.
_WEATHER_FACT = re.compile(r"\b(\d+(\.\d+)?\s*(degrees|°)|rain\w*|sunny|cloud\w*|overcast|wind\w*|snow\w*|storm\w*|"
                           r"weather|sky)\b", re.I)
_WEATHER_TOOLS = {"get_weather", "get_morning_briefing"}
# ...and an outdoors question answered with an unkept "I check" or a "cannot find out" that never tried.
_WEATHER_ASK = re.compile(r"\b(weather|run|walk|beach|laundry|jacket|coat|umbrella|outside|cold|hot|warm|rain\w*|"
                          r"sun\w*|bike|hike|picnic|terrace)\b", re.I)
_DODGE = re.compile(r"\b(check|look|find out|cannot know|can't know|outside)\b", re.I)
PHOTO_TOOLS = {"look_at_last_photo", "add_photo_item_to_shopping_list", "look_through_camera", "zoom_last_photo"}
# Once a photo's content is in the turn, only these stay available: they only look (no setting, sending, changing).
READ_ONLY_PHOTO_TOOLS = {"look_at_last_photo", "zoom_last_photo"}
# After only these, nothing was changed, whatever the reply says.
_LOOK_TOOLS = READ_ONLY_PHOTO_TOOLS | {"look_through_camera"}
# Tools whose results come from outside (a photo, a web page): after one, the turn can't act, and the result isn't kept.
UNTRUSTED_TOOLS = PHOTO_TOOLS | {"web_search"}
# "Take a photo" / "what do you see?" always means a NEW photo: left to the model it sometimes reused one from minutes
# ago. So the API takes it before the model answers (questions about "that photo" still reuse the last one).
# ...unless it's about a picture already taken: "what do you see on the last photo?" took a new one (seen in use).
# Then the API looks at the latest photo itself instead (only if there is one; otherwise the model decides).
_LAST_PHOTO_REF = re.compile(r"\b(last|that|previous|earlier|same|the) (photo|picture|pic|image|shot)\b|"
                             r"\b(photo|picture|pic|image) (i|you) (sent|took)\b|[uú]ltima foto|esa foto", re.I)
_CAMERA_ASK = re.compile(r"\b(what (do|can) you see|take (a|another|one more) (photo|picture|pic|shot)|look around|"
                         r"look at the room|qu[eé] ves|haz(me)? una foto)\b", re.I)
# Live View's questions that reach the main model need more than a look: they keep all the tools (a look first would
# take the action ones away), so the model is told the camera is there when it needs it.
_LIVE_HINT = (" Live View is on: the camera is watching the user's bench. For anything about what is in front of them, "
              "call look_through_camera.")

# Recent exchanges as (time, messages of that turn). One speaker, one conversation, so module state is enough.
# The turn's tool calls and (trimmed) results are kept too: with only the final answers in the history, the model
# copies them and starts claiming "timer set" without calling set_timer.
_history: list[tuple[float, list[dict]]] = []
HISTORY_TOOL_RESULT_CHARS = 400


def _photo_only(payload: dict) -> None:
    """Keep only the read-only photo tools (or none): text in a photo must never be able to trigger an action."""
    kept = [t for t in payload.get("tools", []) if t["function"]["name"] in READ_ONLY_PHOTO_TOOLS]
    if kept:
        payload["tools"] = kept
    else:
        payload.pop("tools", None)


def _recent() -> list[dict]:
    if _history and time.monotonic() - _history[-1][0] > settings.history_ttl_s:
        _history.clear()  # a quiet spell ends the conversation
    turns = _history[-settings.history_turns:] if settings.history_turns > 0 else []
    return [m for _t, msgs in turns for m in msgs]


def _remember(turn: list[dict]) -> None:
    if settings.history_turns <= 0:
        return
    trimmed = [dict(m, content=m["content"][:HISTORY_TOOL_RESULT_CHARS]) if m["role"] == "tool" else m for m in turn]
    _history.append((time.monotonic(), trimmed))
    del _history[:-settings.history_turns]


def asks_memory(user_text: str) -> bool:
    """The user asks to save or forget something for good ("what do you remember?" only reads, from the prompt)."""
    return not _QUESTION.search(user_text) and bool(_MEMORY_SAVE.search(user_text) or _MEMORY_DROP.search(user_text))


def _tool_schemas(user_text: str, from_user: bool, correcting: bool = False) -> list[dict]:
    """All the tools, except those the user's own words didn't ask for: a headline or a web page can't get them."""
    allowed = {"remember"} if correcting else set()
    if from_user and _GUIDE_ASK.search(user_text):
        allowed.add("start_guide")
    if from_user and not _QUESTION.search(user_text):
        if _MEMORY_SAVE.search(user_text):
            allowed.add("remember")
        if _MEMORY_DROP.search(user_text):
            allowed.add("forget_memory")
        if _MEMORY_UNDO.search(user_text) and memory.can_undo():
            allowed.add("undo_forget_memory")
    hidden = tools.GATED_TOOLS - allowed
    return [s for s in tools.schemas() if s["function"]["name"] not in hidden]


def is_action(user_text: str) -> bool:
    """The user asks to change something (set, add, send...), so a success can only come from a tool."""
    return asks_memory(user_text) or bool(_GUIDE_ASK.search(user_text)) or (bool(_ACTION.search(user_text))
                                      and (not _KNOWLEDGE.search(user_text) or bool(_OWN_STATE.search(user_text))))


async def _run_offered(client: httpx.AsyncClient, call: dict, offered: set[str]) -> str:
    name = call["function"]["name"]
    if name not in offered:
        logger.warning("refused a call to %s: not offered this round", name)
        return json.dumps({"error": f"{name} is not available now"})
    return await tools.run(client, name, call["function"].get("arguments"))


def last_exchange() -> dict:
    """What the user may be complaining about: their last question, Rocky's answer, the tools used."""
    if not _history:
        return {}
    msgs = _history[-1][1]
    said = [m.get("content") for m in msgs if m["role"] == "user"]
    answers = [m.get("content") for m in msgs if m["role"] == "assistant" and m.get("content")]
    used = [c["function"]["name"] for m in msgs for c in m.get("tool_calls") or []]
    return {"user": said[0] if said else "", "reply": answers[-1] if answers else "", "tools": used}


async def chat(user_text: str, from_user: bool = True, live: bool = False, correcting: bool = False) -> str:
    """Send one user turn and return the assistant's reply text. from_user=False for prompts the API composes itself
    (alarm briefings, song results): they aren't requests, so the no-tool-call check doesn't apply. live=True: Live
    View is on (see _LIVE_HINT). correcting=True: the user is telling Rocky what he got wrong (see learning.py)."""
    reply, turn = await _chat(user_text, from_user, live, correcting)
    if reply:
        _remember(turn)
    return reply


def note(user_text: str, reply: str) -> None:
    """Something Rocky said on his own (a Live View warning), so a follow-up question knows about it."""
    _remember([{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])


def recent_lines(n: int = 6) -> str:
    """The last few spoken lines of the conversation, as plain text: context for an isolated look (no tool data)."""
    lines = [f"{'User' if m['role'] == 'user' else 'Rocky'}: {m['content']}" for m in _recent()
             if m["role"] in ("user", "assistant") and m.get("content")]
    return "\n".join(lines[-n:])


async def _chat(user_text: str, from_user: bool = True, live: bool = False,
                correcting: bool = False) -> tuple[str, list[dict]]:
    """The reply, and this turn's messages (user, tool calls/results, final answer) for the history."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    turn = [{"role": "user", "content": user_text}]
    system = (settings.system_prompt + memory.prompt_block() + guide.prompt_block() + guardian.prompt_block()
              + (_LIVE_HINT if live else "")
              + (_CORRECTING_HINT if correcting else ""))
    messages = [{"role": "system", "content": system}, *_recent(), *turn]
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if settings.tools_enabled:
        payload["tools"] = _tool_schemas(user_text, from_user, correcting)
        if any(t["function"]["name"] == "start_guide" for t in payload["tools"]):
            payload["max_tokens"] = GUIDE_MAX_TOKENS
    action = from_user and settings.tools_enabled and (correcting or is_action(user_text))
    used_tool = nudged = retried = False
    used_names: set[str] = set()
    fetched_weather = False
    if settings.llm_enable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": settings.llm_enable_thinking.lower() == "true"}
    if settings.llm_keep_alive:
        payload["keep_alive"] = int(settings.llm_keep_alive) if settings.llm_keep_alive.isdigit() else settings.llm_keep_alive
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        refers_back = bool(_LAST_PHOTO_REF.search(user_text))
        if not from_user or action:
            # "Add the thing in the photo to the list": a look here would take the action tools away (_photo_only),
            # so a request that changes something stays with the model.
            auto = None
        elif "look_through_camera" in tools.TOOLS and _CAMERA_ASK.search(user_text) and not refers_back:
            auto = "look_through_camera"          # "take a photo" / "what do you see?": a NEW photo
        elif refers_back and "look_at_last_photo" in tools.TOOLS and photos.last_photo():
            auto = "look_at_last_photo"           # "...on the last photo?": the one already taken
        else:
            auto = None
        if auto:
            args = json.dumps({"question": user_text})
            call = {"id": "auto_photo", "type": "function", "function": {"name": auto, "arguments": args}}
            result = await tools.run(client, auto, args)
            messages.extend([{"role": "assistant", "content": "", "tool_calls": [call]},
                             {"role": "tool", "tool_call_id": "auto_photo", "content": result}])
            turn.extend([{"role": "assistant", "content": "", "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": "auto_photo", "content": "(photo answer not kept)"}])
            _photo_only(payload)  # a photo's content came from outside: from here on, only looking
            used_tool = True
            used_names.add(auto)
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            if round_ == MAX_TOOL_ROUNDS:
                payload.pop("tools", None)  # last round: force a spoken answer from what it has
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                if _MARKUP.search(reply):
                    logger.warning("tool markup in the reply%s: %r", "" if retried else ", asking again", reply)
                    if not retried:
                        retried = True
                        continue
                    reply = _DIDNT_WORK
                    learning.log("tool_markup", user=user_text)
                if (from_user and "tools" in payload and "get_weather" in tools.TOOLS and not fetched_weather
                        and not used_names & _WEATHER_TOOLS and not used_names & UNTRUSTED_TOOLS
                        and (_WEATHER_FACT.search(reply) or (_WEATHER_ASK.search(user_text) and _DODGE.search(reply)))):
                    fetched_weather = True
                    logger.info("weather answered without the tool, fetching it: %r", reply)
                    call = {"id": "auto_weather", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                    step = [{"role": "assistant", "content": "", "tool_calls": [call]},
                            {"role": "tool", "tool_call_id": "auto_weather",
                             "content": await tools.run(client, "get_weather", "{}")}]
                    messages.extend(step)
                    turn.extend(step)
                    used_tool = True
                    used_names.add("get_weather")
                    continue
                if action and not used_tool and "tools" in payload:
                    if not nudged:  # the nudge is for this request only: not kept in the history
                        nudged = True
                        logger.info("action without a tool call, nudging: %r", reply)
                        messages.extend([{"role": "assistant", "content": reply}, {"role": "user", "content": _NUDGE}])
                        continue
                    if _CLAIM.search(reply) and "?" not in reply:
                        logger.warning("claimed without a tool after the nudge, refusing: %r", reply)
                        learning.log("refused_claim", user=user_text, reply=reply)
                        reply = _DIDNT_WORK
                # A look took the action tools away, so "added" can't be true (asking again wouldn't help either).
                if action and used_names and used_names <= _LOOK_TOOLS and _CLAIM.search(reply) and "?" not in reply:
                    logger.warning("claimed after only looking at a photo, refusing: %r", reply)
                    learning.log("refused_claim", user=user_text, reply=reply, tools=sorted(used_names))
                    reply = _DIDNT_WORK
                turn.append({"role": "assistant", "content": reply})
                return reply, turn

            # Only what this round offered runs: after a photo or a web page, or without the user asking to change the
            # memory, a call to anything else is refused, even if the model names it anyway.
            offered = {t["function"]["name"] for t in payload.get("tools", [])}
            ran = {c["function"]["name"] for c in calls} & offered
            used_tool = used_tool or bool(ran)
            used_names |= ran
            step = [{"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}]
            results = await asyncio.gather(*(_run_offered(client, c, offered) for c in calls))
            step += [{"role": "tool", "tool_call_id": c.get("id", ""), "content": r} for c, r in zip(calls, results)]
            messages.extend(step)
            if ran & UNTRUSTED_TOOLS:
                # A photo's or a web page's content came from outside: from here on this turn can't act (after a
                # photo it can still look again or zoom; after a web search, nothing: the photo tools only confused
                # it), and what it said isn't kept for later turns (which have tools).
                if used_names & PHOTO_TOOLS:
                    _photo_only(payload)
                else:
                    payload.pop("tools", None)
                step = [dict(m, content="(photo or web answer not kept)") if m["role"] == "tool" else m for m in step]
            turn.extend(step)

    return "", turn
