"""'What song is this?': the next recording is a music sample, identified with Shazam.

The identify_song tool arms this, Rocky says he's listening, and the speaker's follow-up window
records the music (it's loud and continuous, so it runs to the capture limit). /talk then sends
that clip to Shazam instead of Whisper. It uses shazamio, an unofficial client of Shazam's API (no
key): it can stop working whenever Shazam changes something, so failures are reported, not raised.
"""
import logging
import re
import time

logger = logging.getLogger("uvicorn.error")

ARM_FOR_S = 30  # the music has to arrive within this; a stale "armed" must not eat a later question

_armed_until = 0.0

# "What song is this?" and friends. Matched on the transcript so a request always arms recognition, even when the
# model answers "Rocky listening" without calling identify_song (seen in testing).
_REQUEST = re.compile(r"\b(what|which|name)\b.{0,25}\b(song|track|tune)\b|\bshazam\b|qu[eé] canci[oó]n", re.I)


def is_request(transcript: str) -> bool:
    return bool(_REQUEST.search(transcript or ""))


def arm() -> None:
    global _armed_until
    _armed_until = time.monotonic() + ARM_FOR_S


def take_armed() -> bool:
    """True once if a song identification is pending (and clears it)."""
    global _armed_until
    armed = time.monotonic() < _armed_until
    _armed_until = 0.0
    return armed


async def recognize(wav_bytes: bytes) -> dict:
    try:
        from shazamio import Shazam

        out = await Shazam().recognize(wav_bytes)
    except Exception as e:
        logger.warning("Shazam failed: %r", e)
        return {"error": f"{type(e).__name__}"}
    track = out.get("track")
    if not track:
        return {"match": False}
    meta = {m.get("title"): m.get("text") for s in track.get("sections", []) for m in s.get("metadata", []) or []}
    result = {"match": True, "title": track.get("title"), "artist": track.get("subtitle"),
              "album": meta.get("Album"), "year": meta.get("Released"), "genre": (track.get("genres") or {}).get("primary"),
              "url": track.get("url")}
    logger.info("Shazam: %s", result)
    return {k: v for k, v in result.items() if v}


def phone_message(result: dict) -> str:
    """What goes to Telegram for a match: the song, and links to look it up / play it."""
    from urllib.parse import quote_plus

    extra = ", ".join(str(result[k]) for k in ("album", "year") if result.get(k))
    lines = [f"🎵 {result['title']} — {result['artist']}" + (f" ({extra})" if extra else "")]
    if result.get("url"):
        lines.append(f"Shazam: {result['url']}")
    lines.append("YouTube: https://www.youtube.com/results?search_query=" + quote_plus(f"{result['title']} {result['artist']}"))
    return "\n".join(lines)


def as_prompt(result: dict, sent_to_phone: bool = False) -> str:
    """The turn handed to the LLM, so Rocky tells it his way and it lands in the conversation memory."""
    if result.get("match"):
        details = ", ".join(f"{k} {result[k]}" for k in ("album", "year", "genre") if result.get(k))
        return (f"[Song recognition: Rocky listened to the music and Shazam says it is \"{result['title']}\" by "
                f"{result['artist']}{' (' + details + ')' if details else ''}."
                f"{' It was also sent to the user phone.' if sent_to_phone else ''}] Tell the user the song and artist"
                f"{' (and that it is on their phone)' if sent_to_phone else ''}, Rocky style, in one or two short sentences.")
    if result.get("error"):
        return ("[Song recognition: the music service failed.] Tell the user Rocky cannot recognize songs right now.")
    return ("[Song recognition: no match. The music was probably too quiet, too far away, or not music.] Tell the "
            "user Rocky could not recognize it, and to try again with the music louder or closer.")
