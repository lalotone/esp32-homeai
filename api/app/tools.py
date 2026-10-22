"""Tools the LLM can call (OpenAI function calling): time, weather, news, math, Wikipedia,
homelab status, timers and reminders, the shopping list and a morning briefing.

All sources are free and keyless. Each tool returns a small JSON-able dict that the model turns into
1-3 spoken sentences; errors come back as {"error": ...} so the model can say it couldn't find out,
instead of the whole /talk failing. Network answers are cached briefly: a voice question is often
followed by a related one, and the feeds/forecast don't change by the minute.
"""
import ast
import asyncio
import html
import json
import logging
import math
import operator
import os
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

from . import alerts, shopping, telegram
from .config import settings

logger = logging.getLogger("uvicorn.error")

HTTP_TIMEOUT_S = 6.0
CACHE_TTL_S = 600
# Wikimedia rejects (403) user agents without a contact URL: https://w.wiki/4wJS
HEADERS = {"User-Agent": "esp32-homeai/1.0 (self-hosted home voice assistant; https://github.com/lalotone/esp32-homeai)"}

# RSS feeds per region. Several per region are merged newest-first, so one stale or broken feed
# doesn't empty the answer. Parsed leniently (regex): some of these aren't well-formed XML.
# "local" comes from settings (LOCAL_NEWS_FEEDS), since it depends on where the speaker lives.
NEWS_FEEDS = {
    "world": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
    "spain": ["https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "https://www.20minutos.es/rss/"],
}
if settings.local_news_feeds:
    NEWS_FEEDS["local"] = [u.strip() for u in settings.local_news_feeds.split(",") if u.strip()]

# WMO weather interpretation codes (Open-Meteo's weather_code), condensed for speech.
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}

_cache: dict[str, tuple[float, object]] = {}


async def _get(client: httpx.AsyncClient, url: str, as_json: bool = True, **params):
    key = url + json.dumps(params, sort_keys=True)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    resp = await client.get(url, params=params or None, headers=HEADERS, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json() if as_json else resp.text
    _cache[key] = (time.monotonic(), data)
    return data


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.home_tz)


# --- time ---

async def get_time(client: httpx.AsyncClient) -> dict:
    now = datetime.now(_tz())
    return {"local_time": now.strftime("%H:%M"), "date": now.strftime("%A %d %B %Y"),
            "timezone": settings.home_tz, "place": settings.home_name}


# --- weather ---

async def _locate(client: httpx.AsyncClient, location: str | None) -> tuple[str, float, float]:
    if not location or location.strip().lower() in (settings.home_name.lower(), "home", "here"):
        return settings.home_name, settings.home_lat, settings.home_lon
    data = await _get(client, "https://geocoding-api.open-meteo.com/v1/search", name=location, count=1, language="en")
    if not data.get("results"):
        raise LookupError(f"unknown place: {location}")
    r = data["results"][0]
    return f"{r['name']}, {r.get('country', '')}".strip(", "), r["latitude"], r["longitude"]


async def get_weather(client: httpx.AsyncClient, location: str | None = None, days_ahead: int = 0) -> dict:
    name, lat, lon = await _locate(client, location)
    days_ahead = max(0, min(int(days_ahead or 0), 6))
    d = await _get(client, "https://api.open-meteo.com/v1/forecast", latitude=lat, longitude=lon,
                   timezone=settings.home_tz, forecast_days=7,
                   current="temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
                   daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                         "precipitation_sum,sunrise,sunset")
    daily = d["daily"]
    i = days_ahead
    out = {"place": name, "units": "Celsius, km/h, mm",
           "day": daily["time"][i],
           "conditions": WMO.get(daily["weather_code"][i], "unknown"),
           "max_c": daily["temperature_2m_max"][i], "min_c": daily["temperature_2m_min"][i],
           "rain_chance_pct": daily["precipitation_probability_max"][i], "rain_mm": daily["precipitation_sum"][i],
           "sunrise": daily["sunrise"][i][-5:], "sunset": daily["sunset"][i][-5:]}
    if days_ahead == 0:
        c = d["current"]
        out["now"] = {"temp_c": c["temperature_2m"], "feels_like_c": c["apparent_temperature"],
                      "conditions": WMO.get(c["weather_code"], "unknown"), "wind_kmh": c["wind_speed_10m"],
                      "humidity_pct": c["relative_humidity_2m"]}
    return out


# --- news ---

def _strip(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"<[^>]+>", " ", html.unescape(s))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _parse_rss(text: str) -> list[dict]:
    items = []
    for raw in re.findall(r"<item\b.*?</item>", text, re.S):
        title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S)
        if not title:
            continue
        desc = re.search(r"<description[^>]*>(.*?)</description>", raw, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.S)
        try:
            ts = parsedate_to_datetime(date.group(1).strip()).timestamp() if date else 0.0
        except (TypeError, ValueError):
            ts = 0.0
        items.append({"title": _strip(title.group(1)), "summary": _strip(desc.group(1))[:220] if desc else "",
                      "ts": ts})
    return items


async def get_news(client: httpx.AsyncClient, region: str = "world", count: int = 3) -> dict:
    region = region if region in NEWS_FEEDS else "world"
    count = max(1, min(int(count or 3), 5))
    results = await asyncio.gather(*(_get(client, u, as_json=False) for u in NEWS_FEEDS[region]),
                                   return_exceptions=True)
    items, seen = [], set()
    for res in results:
        if isinstance(res, Exception):
            logger.warning("news feed failed: %r", res)
            continue
        for it in _parse_rss(res):
            if it["title"].lower() not in seen:
                seen.add(it["title"].lower())
                items.append(it)
    if not items:
        raise RuntimeError("no news feed answered")
    items.sort(key=lambda it: it["ts"], reverse=True)
    tz = _tz()
    return {"region": region, "language": "en" if region == "world" else "es",
            "headlines": [{"title": it["title"], "summary": it["summary"],
                           "published": datetime.fromtimestamp(it["ts"], tz).strftime("%a %H:%M") if it["ts"] else ""}
                          for it in items[:count]]}


# --- math ---

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {n: getattr(math, n) for n in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil",
                                        "factorial", "radians", "degrees")}
_FUNCS.update(abs=abs, round=round, min=min, max=max)
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*(_eval(a) for a in node.args))
    raise ValueError("unsupported expression")


async def calculate(client: httpx.AsyncClient, expression: str) -> dict:
    value = _eval(ast.parse(expression.replace("^", "**"), mode="eval").body)
    if isinstance(value, float):
        value = round(value, 6)
    return {"expression": expression, "result": value}


# --- Wikipedia ---

async def lookup_wikipedia(client: httpx.AsyncClient, topic: str, language: str = "en") -> dict:
    lang = language if language in ("en", "es") else "en"
    found = await _get(client, f"https://{lang}.wikipedia.org/w/api.php", action="opensearch", search=topic,
                       limit=1, namespace=0, format="json")
    if not found[1]:
        raise LookupError(f"nothing on Wikipedia about {topic}")
    title = found[1][0]
    page = await _get(client, f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}")
    return {"title": page.get("title", title), "summary": page.get("extract", "")[:900]}


# --- homelab status ---

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


async def _k8s(path: str) -> dict:
    """GET on the Kubernetes API with the pod's service account (read-only RBAC: nodes, pods, deployments)."""
    host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    async with httpx.AsyncClient(verify=f"{SA_DIR}/ca.crt", timeout=HTTP_TIMEOUT_S) as k:
        resp = await k.get(f"https://{host}:{port}{path}", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()


def _pod_problem(pod: dict) -> str | None:
    phase = pod["status"].get("phase")
    if phase == "Succeeded":
        return None
    for cs in pod["status"].get("containerStatuses", []):
        waiting = cs.get("state", {}).get("waiting")
        if waiting:
            return waiting.get("reason", "waiting")
        if not cs.get("ready") and phase == "Running":
            return "not ready"
    return None if phase == "Running" else (phase or "unknown")


async def get_homelab_status(client: httpx.AsyncClient) -> dict:
    out: dict = {}
    nodes, pods, deps = await asyncio.gather(_k8s("/api/v1/nodes"), _k8s("/api/v1/pods"),
                                             _k8s("/apis/apps/v1/deployments"), return_exceptions=True)
    if isinstance(nodes, Exception):
        out["kubernetes"] = f"unreachable: {type(nodes).__name__}"
    else:
        out["nodes"] = [{"name": n["metadata"]["name"],
                         "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]),
                         "pressure": [c["type"] for c in n["status"]["conditions"]
                                      if c["type"].endswith("Pressure") and c["status"] == "True"]}
                        for n in nodes["items"]]
    if not isinstance(pods, Exception):
        problems = [(p["metadata"]["namespace"], p["metadata"]["name"], _pod_problem(p)) for p in pods["items"]]
        problems = [p for p in problems if p[2]]
        out["pods"] = {"total": len(pods["items"]), "with_problems": len(problems),
                       "problems": [f"{ns}/{name}: {why}" for ns, name, why in problems[:5]]}
    if not isinstance(deps, Exception):
        out["deployments_down"] = [f"{d['metadata']['namespace']}/{d['metadata']['name']}" for d in deps["items"]
                                   if d["spec"].get("replicas", 1) > 0
                                   and d["status"].get("availableReplicas", 0) < d["spec"].get("replicas", 1)]
        out["scaled_to_zero"] = [d["metadata"]["name"] for d in deps["items"] if d["spec"].get("replicas", 1) == 0]
    if settings.homelab_dcgm_url:
        try:
            metrics = await _get(client, settings.homelab_dcgm_url, as_json=False)
            gpus: dict[str, dict] = {}
            names = {"DCGM_FI_DEV_GPU_TEMP": "temp_c", "DCGM_FI_DEV_GPU_UTIL": "busy_pct",
                     "DCGM_FI_DEV_FB_USED": "mem_used_mb", "DCGM_FI_DEV_FB_FREE": "mem_free_mb",
                     "DCGM_FI_DEV_POWER_USAGE": "power_w"}
            for line in metrics.splitlines():
                m = re.match(r'(DCGM_FI_DEV_\w+)\{gpu="(\d+)".*\}\s+([\d.]+)', line)
                if m and m.group(1) in names:
                    gpus.setdefault(m.group(2), {})[names[m.group(1)]] = round(float(m.group(3)))
            out["gpus"] = gpus
        except Exception as e:
            out["gpus"] = f"unavailable: {type(e).__name__}"
    if settings.homelab_disk_path:
        st = os.statvfs(settings.homelab_disk_path)
        out["gpu_node_disk"] = {"free_gb": round(st.f_bavail * st.f_frsize / 1e9),
                                "total_gb": round(st.f_blocks * st.f_frsize / 1e9),
                                "note": "kubelet starts evicting pods below 15% free"}
    return out


# --- timers and reminders (announced by the speaker via GET /pending, see alerts.py) ---

async def set_timer(client: httpx.AsyncClient, minutes: float = 0, seconds: int = 0, label: str = "") -> dict:
    total = float(minutes or 0) * 60 + int(seconds or 0)
    if total < 5 or total > 24 * 3600:
        raise ValueError("timer must be between 5 seconds and 24 hours")
    return {"set": alerts.add("timer", label or "timer", time.time() + total, _tz())}


async def set_reminder(client: httpx.AsyncClient, text: str, at: str = "", day_offset: int = 0,
                       in_minutes: float = 0, to_phone: bool = False) -> dict:
    if to_phone and not telegram.configured():
        raise RuntimeError("phone messages are not set up yet")
    if in_minutes:
        due = time.time() + float(in_minutes) * 60
    elif at:
        due = alerts.due_at(at, int(day_offset or 0), _tz())
    else:
        raise ValueError("need a time (at) or in_minutes")
    return {"set": alerts.add("reminder", text, due, _tz(), phone=bool(to_phone))}


async def list_alerts(client: httpx.AsyncClient) -> dict:
    return {"timers_and_reminders": alerts.listing(_tz())}


async def cancel_alert(client: httpx.AsyncClient, label: str = "") -> dict:
    gone = alerts.cancel(label, _tz())
    if not gone:
        raise LookupError(f"no timer or reminder matching '{label}'" if label else "no timers or reminders set")
    return {"cancelled": gone}


# --- shopping list (shopping.py) ---

async def add_to_shopping_list(client: httpx.AsyncClient, items: list[str]) -> dict:
    return shopping.add(items)


async def remove_from_shopping_list(client: httpx.AsyncClient, items: list[str]) -> dict:
    return shopping.remove(items)


async def get_shopping_list(client: httpx.AsyncClient) -> dict:
    return {"list": shopping.items()}


async def clear_shopping_list(client: httpx.AsyncClient) -> dict:
    return shopping.clear()


async def undo_clear_shopping_list(client: httpx.AsyncClient) -> dict:
    return shopping.undo_clear()


# --- phone (Telegram bot, telegram.py) ---

async def send_to_phone(client: httpx.AsyncClient, text: str) -> dict:
    await telegram.send(text)
    return {"sent": True}


async def send_shopping_list(client: httpx.AsyncClient) -> dict:
    items = shopping.items()
    if not items:
        raise LookupError("the shopping list is empty, nothing to send")
    await telegram.send("🛒 Shopping list\n" + "\n".join(f"• {i}" for i in items))
    return {"sent": True, "items": len(items)}


# --- good-morning briefing ---

async def get_morning_briefing(client: httpx.AsyncClient) -> dict:
    """Everything for "good morning" in one call (one LLM round instead of four)."""
    async def safe(coro):
        try:
            return await coro
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    now, weather, news = await asyncio.gather(safe(get_time(client)), safe(get_weather(client)),
                                              safe(get_news(client, "world", 2)))
    today = datetime.now(_tz()).strftime("%a")
    return {"now": now, "weather_today": weather, "world_news": news,
            "today_timers_and_reminders": [a for a in alerts.listing(_tz()) if a["at"].startswith(today)],
            "shopping_list_items": len(shopping.items())}


# --- registry ---

TOOLS = {
    "get_time": (get_time, "Current local time and date at home.", {}),
    "get_weather": (get_weather, "Weather now and forecast (up to 6 days ahead). Defaults to home.", {
        "location": {"type": "string", "description": "City or place; omit for home"},
        "days_ahead": {"type": "integer", "description": "0 = today, 1 = tomorrow, ... up to 6"}}),
    "get_news": (get_news, "Latest news. world = BBC World News (English); spain = Spanish national news (Spanish)"
                           + (f"; local = {settings.local_news_name or 'local news'}" if "local" in NEWS_FEEDS else "")
                           + ". One region per answer: world when the user doesn't say; several only if they ask "
                             "for several.", {
        "region": {"type": "string", "enum": list(NEWS_FEEDS)},
        "count": {"type": "integer", "description": "How many headlines, 1-5 (default 3)"}}),
    "calculate": (calculate, "Exact arithmetic: + - * / ** %, sqrt, log, sin, pi... Use for any math.", {
        "expression": {"type": "string", "description": "e.g. (3.5 * 60) or sqrt(2) * 10"}}),
    "lookup_wikipedia": (lookup_wikipedia, "Short encyclopedia summary of a person, place, thing or event.", {
        "topic": {"type": "string"},
        "language": {"type": "string", "enum": ["en", "es"], "description": "es for Spanish-only topics"}}),
    "get_homelab_status": (get_homelab_status, "Health of the user's home Kubernetes cluster: nodes, broken pods, "
                                               "deployments down, GPU temperature/memory/load, disk space.", {}),
    "set_timer": (set_timer, "Start a countdown timer; the speaker announces it when it ends.", {
        "minutes": {"type": "number"}, "seconds": {"type": "integer"},
        "label": {"type": "string", "description": "What it's for, e.g. pasta"}}),
    "set_reminder": (set_reminder, "Remind the user of something later; the speaker says it at that time.", {
        "text": {"type": "string", "description": "What to remind, e.g. call mum"},
        "at": {"type": "string", "description": "Local time HH:MM, 24h"},
        "day_offset": {"type": "integer", "description": "0 = today, 1 = tomorrow..."},
        "in_minutes": {"type": "number", "description": "Instead of at: minutes from now"},
        "to_phone": {"type": "boolean", "description": "Also send it to the user's phone (\"remind me on my phone\")"}}),
    "list_alerts": (list_alerts, "List the timers and reminders that are set, with time left.", {}),
    "cancel_alert": (cancel_alert, "Cancel timers/reminders whose label contains the text; empty = all.", {
        "label": {"type": "string"}}),
    "add_to_shopping_list": (add_to_shopping_list, "Add items to the shopping list (one entry per item).", {
        "items": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"milk\", \"2 onions\"]"}}),
    "remove_from_shopping_list": (remove_from_shopping_list, "Remove bought or unwanted items from the shopping list.", {
        "items": {"type": "array", "items": {"type": "string"}}}),
    "get_shopping_list": (get_shopping_list, "Read the shopping list.", {}),
    "clear_shopping_list": (clear_shopping_list, "Empty the whole shopping list (e.g. \"clear the list\").", {}),
    "undo_clear_shopping_list": (undo_clear_shopping_list, "Bring back the items from the last clear.", {}),
    "send_to_phone": (send_to_phone, "Send a text message to the user's phone (Telegram), e.g. a recipe, an "
                                     "address, the weather. Write the full text to send.", {
        "text": {"type": "string"}}),
    "send_shopping_list": (send_shopping_list, "Send the shopping list to the user's phone (Telegram).", {}),
    "get_morning_briefing": (get_morning_briefing, "Morning briefing for \"good morning\" or \"what's my day\": "
                                                   "time, today's weather, today's reminders, 2 world news, "
                                                   "shopping list size. Use it instead of the separate tools.", {}),
}
_REQUIRED = {"calculate": ["expression"], "lookup_wikipedia": ["topic"], "set_reminder": ["text"],
             "add_to_shopping_list": ["items"], "remove_from_shopping_list": ["items"], "send_to_phone": ["text"]}


def schemas() -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": _REQUIRED.get(name, [])}}}
        for name, (_fn, desc, props) in TOOLS.items()]


async def run(client: httpx.AsyncClient, name: str, arguments: str) -> str:
    """Run one tool call; always returns a JSON string (errors included) for the model."""
    t0 = time.monotonic()
    try:
        fn = TOOLS[name][0]
        args = json.loads(arguments or "{}")
        result = await asyncio.wait_for(fn(client, **args), timeout=HTTP_TIMEOUT_S + 2)
    except Exception as e:  # anything a tool raises becomes a message the model can speak around
        logger.warning("tool %s(%s) failed: %r", name, arguments, e)
        result = {"error": f"{type(e).__name__}: {e}"}
    logger.info("tool %s(%s) %.2fs", name, arguments, time.monotonic() - t0)
    return json.dumps(result, ensure_ascii=False)
