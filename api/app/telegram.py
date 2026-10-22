"""Talk to the user's phone through a Telegram bot.

Sending: only ever to TELEGRAM_CHAT_ID (the user's own chat with the bot): the model picks the text, never the
recipient.

Receiving (photos.py, opt-in with TELEGRAM_PHOTOS=true): get_updates() long-polls Telegram (outgoing requests only,
no port opened) and is_trusted() is the single gate every incoming message must pass: a private chat that IS the
configured chat, from that same user, not a bot, not forwarded. Everything else is dropped by the caller.

The bot token is a secret that ends up in URLs, so errors are re-raised without the request URL, and httpx's request
logging is kept below INFO, so the token can't leak into logs or into what Rocky says.
"""
import logging

import httpx

from .config import settings

logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines print full URLs (with the token)

MAX_CHARS = 4000  # Telegram's limit is 4096
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
POLL_TIMEOUT_S = 30  # long poll: Telegram holds the request open until a message arrives or this passes


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def _call(method: str, http_timeout: float = 10, **params) -> dict:
    """POST a Bot API method; returns `result`. Never lets the token-bearing URL into an exception."""
    if not configured():
        raise TelegramError("Telegram is not set up (no bot token or chat id)")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(url, json=params)
    except httpx.HTTPError as e:
        raise TelegramError(f"Telegram unreachable: {type(e).__name__}") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not body.get("ok"):
        raise TelegramError(f"Telegram refused {method} ({resp.status_code} {body.get('description', '')})".strip())
    return body.get("result")


async def send(text: str) -> None:
    await _call("sendMessage", chat_id=settings.telegram_chat_id, text=text[:MAX_CHARS])


async def get_updates(offset: int | None, timeout: int = POLL_TIMEOUT_S) -> list[dict]:
    """Pending updates after `offset`. Only plain messages are requested: no edits, channel posts, buttons, inline
    queries or anything else Telegram can deliver."""
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return await _call("getUpdates", http_timeout=timeout + 10, **params) or []


def is_trusted(message: dict) -> bool:
    """The one gate for incoming messages: the user's own private chat, sent by the user themselves."""
    try:
        me = int(settings.telegram_chat_id)
    except ValueError:
        return False
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    return (chat.get("type") == "private" and chat.get("id") == me and sender.get("id") == me
            and not sender.get("is_bot") and "forward_origin" not in message and "forward_date" not in message)


async def download_file(file_id: str) -> bytes:
    """The bytes of a file the user sent, refusing anything over MAX_DOWNLOAD_BYTES."""
    info = await _call("getFile", file_id=file_id)
    if (info.get("file_size") or 0) > MAX_DOWNLOAD_BYTES or not info.get("file_path"):
        raise TelegramError("file too large or unavailable")
    url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{info['file_path']}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise TelegramError(f"Telegram refused the download ({resp.status_code})")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        raise TelegramError("file too large")
                return bytes(data)
    except httpx.HTTPError as e:
        raise TelegramError(f"Telegram unreachable: {type(e).__name__}") from None
