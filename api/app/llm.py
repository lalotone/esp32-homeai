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

from . import tools
from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_TOOL_ROUNDS = 3  # a voice question needs one, rarely two (e.g. weather + time); stop loops early

MAX_TOKENS = 400  # a spoken reply or a tool call is far shorter; stops a runaway generation (seen: 2000+ tokens)

# Requests that change something. With the full prompt and ~20 tools the model sometimes answers "alarm set" /
# "added" without calling anything (3 of 5 alarm requests in testing; tool_choice="required" isn't enforced by
# llama.cpp with this template). So: an action request that ends with no tool call gets one corrective nudge, and a
# success claimed without any tool is replaced by an honest "didn't work".
_ACTION = re.compile(r"\b(wake me|alarm|timer|remind|add|put|remove|delete|clear|cancel|send|set|undo)\b", re.I)
_CLAIM = re.compile(r"\b(set|added|sent|cancel+ed|removed|cleared|done|ready|on (the|your) list|will wake)\b", re.I)
_NUDGE = ("[Check: you called no tool, so nothing was set, added, sent or cancelled. If this request needs a tool, "
          "call it now. If you really must ask the user something first, reply with only that question.]")
_DIDNT_WORK = "Hmm, Rocky could not do that. Say it again, question?"
PHOTO_TOOLS = {"look_at_last_photo", "add_photo_item_to_shopping_list"}  # their results come from a photo

# Recent exchanges as (time, messages of that turn). One speaker, one conversation, so module state is enough.
# The turn's tool calls and (trimmed) results are kept too: with only the final answers in the history, the model
# copies them and starts claiming "timer set" without calling set_timer.
_history: list[tuple[float, list[dict]]] = []
HISTORY_TOOL_RESULT_CHARS = 400


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


async def chat(user_text: str, from_user: bool = True) -> str:
    """Send one user turn and return the assistant's reply text. from_user=False for prompts the API composes itself
    (alarm briefings, song results): they aren't requests, so the no-tool-call check doesn't apply."""
    reply, turn = await _chat(user_text, from_user)
    if reply:
        _remember(turn)
    return reply


async def _chat(user_text: str, from_user: bool = True) -> tuple[str, list[dict]]:
    """The reply, and this turn's messages (user, tool calls/results, final answer) for the history."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    turn = [{"role": "user", "content": user_text}]
    messages = [{"role": "system", "content": settings.system_prompt}, *_recent(), *turn]
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if settings.tools_enabled:
        payload["tools"] = tools.schemas()
    action = from_user and settings.tools_enabled and bool(_ACTION.search(user_text))
    used_tool = nudged = False
    if settings.llm_enable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": settings.llm_enable_thinking.lower() == "true"}
    if settings.llm_keep_alive:
        payload["keep_alive"] = int(settings.llm_keep_alive) if settings.llm_keep_alive.isdigit() else settings.llm_keep_alive
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            if round_ == MAX_TOOL_ROUNDS:
                payload.pop("tools", None)  # last round: force a spoken answer from what it has
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                if action and not used_tool and "tools" in payload:
                    if not nudged:  # the nudge is for this request only: not kept in the history
                        nudged = True
                        logger.info("action without a tool call, nudging: %r", reply)
                        messages.extend([{"role": "assistant", "content": reply}, {"role": "user", "content": _NUDGE}])
                        continue
                    if _CLAIM.search(reply) and "?" not in reply:
                        logger.warning("claimed without a tool after the nudge, refusing: %r", reply)
                        reply = _DIDNT_WORK
                turn.append({"role": "assistant", "content": reply})
                return reply, turn

            used_tool = True
            step = [{"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}]
            results = await asyncio.gather(*(tools.run(client, c["function"]["name"], c["function"].get("arguments"))
                                             for c in calls))
            step += [{"role": "tool", "tool_call_id": c.get("id", ""), "content": r} for c, r in zip(calls, results)]
            messages.extend(step)
            if any(c["function"]["name"] in PHOTO_TOOLS for c in calls):
                # A photo's content came from outside: from here on this turn can only talk, not act, and what the
                # photo said isn't kept for later turns (which have tools).
                payload.pop("tools", None)
                step = [dict(m, content="(photo answer not kept)") if m["role"] == "tool" else m for m in step]
            turn.extend(step)

    return "", turn
