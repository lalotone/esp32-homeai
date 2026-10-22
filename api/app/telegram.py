"""Send messages to the user's phone through a Telegram bot.

Only ever to TELEGRAM_CHAT_ID (the user's own chat with the bot): the model picks the text, never the
recipient. The bot token is a secret that would end up in URLs, so errors are re-raised without the
request URL, and httpx's request logging is kept below INFO, so the token can't leak into logs or
into what Rocky says.
"""
import logging

import httpx

from .config import settings

logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines print full URLs (with the token)

MAX_CHARS = 4000  # Telegram's limit is 4096


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def send(text: str) -> None:
    if not configured():
        raise TelegramError("Telegram is not set up (no bot token or chat id)")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": text[:MAX_CHARS]})
    except httpx.HTTPError as e:
        raise TelegramError(f"Telegram unreachable: {type(e).__name__}") from None
    if resp.status_code != 200:
        try:
            detail = resp.json().get("description", "")
        except ValueError:
            detail = ""
        raise TelegramError(f"Telegram refused the message ({resp.status_code} {detail})".strip())
