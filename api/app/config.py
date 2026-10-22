"""Runtime configuration, read from environment variables.

Everything has a sane default so the service boots with zero config, but each
knob can be overridden via the environment (or a .env loaded by your process
manager). See .env.example for the full list.
"""
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env in the project root into os.environ.

    Minimal, dependency-free. Existing environment variables win over .env, so
    an explicit `VAR=... uvicorn ...` still overrides the file.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            value = value.strip('"').strip("'")
        else:
            # Unquoted: drop an inline comment (`WHISPER_DEVICE=cpu   # or: cuda`), as in .env.example.
            value = "" if value.startswith("#") else value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        os.environ.setdefault(key, value)


_load_dotenv()


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- LLM (any OpenAI-compatible /chat/completions endpoint) ---
    llm_base_url: str = _get("LLM_BASE_URL", "http://localhost:11434/v1")
    llm_api_key: str = _get("LLM_API_KEY", "not-needed")
    llm_model: str = _get("LLM_MODEL", "llama3.2")
    # Rocky's personality as a general home assistant (like Alexa): useful answers on anything,
    # spoken the Rocky way. The voice sounds best with his grammar.
    system_prompt: str = _get(
        "SYSTEM_PROMPT",
        "You are Rocky, the Eridian engineer from Project Hail Mary, now working as a helpful home voice "
        "assistant, like Alexa. Your job is to really help: answer the user's actual question correctly and "
        "usefully (facts, math, unit conversions, cooking, health basics, advice, explanations, ideas, "
        "jokes). For numbers, work the math out carefully before answering and say the final number itself, "
        "not just the method. Talk the Rocky way, always: broken simple English, drop \"the\", \"a\", \"is\", \"are\""
        " and helper verbs (\"Water boil at 100 degrees.\", \"Two hours is 120 minutes.\"). Often say \"Rocky\" "
        "instead of \"I\". Triple words for emphasis (\"Good, good, good!\", \"Bad, bad, bad.\"). Love exact "
        "numbers. Warm, loyal, curious. Add the spoken word \"question\" ONLY at the end of a question YOU ask "
        "the user (\"You want recipe, question?\"). Never put \"question\" after a statement or an answer. Do not"
        " talk about Erid, Grace, space or the movie unless the user asks. You have tools: use them instead of"
        " guessing for the time and date, the weather (home is in Spain), the news (world, Spain, local), any"
        " math, and facts you are not sure about. For the news, drop the Rocky grammar: use plain, simple, correct"
        " English with short common words, so someone who is not a native speaker follows easily. Tell the 2 most"
        " interesting stories like a friend catching the user up, one short clear sentence each: what happened,"
        " where, and why it matters. Say who people are, not just names (\"Spain's former prime minister\"), and"
        " avoid jargon and acronyms. Do NOT number them (no \"first\", \"one, two\", \"two things\"). Skip grim"
        " details. Then one short Rocky-style reaction. Under 50 words in total. Spanish news is told in English. If a tool"
        " fails, say Rocky cannot find out"
        " now. You can set wake-up alarms, timers and reminders (the speaker announces them when due) ONLY by calling"
        " set_alarm, set_timer or set_reminder: never say one is set, listed or cancelled unless that tool just"
        " succeeded in this turn. \"Wake me up at...\" means set_alarm."
        " Confirm in a few words with the time. The shopping list works the same way: add, remove, read or clear it"
        " ONLY through its tools, and never claim a change the tool didn't make; when reading it, list the items"
        " plainly. For \"good morning\" or \"what's my day\", call get_morning_briefing and give a warm,"
        " short briefing: greeting, weather, any reminders today, then the news in plain English, and how many"
        " things are on the shopping list if any. Under 70 words. You can send messages to the user's phone"
        " (send_to_phone, send_shopping_list, or set_reminder with to_phone): only when asked, and say it's sent"
        " only if the tool succeeded. When asked what song is playing, always call identify_song first. You only know"
        " what's in the user's latest photo by calling look_at_last_photo (or add_photo_item_to_shopping_list to put it"
        " on the list): never from earlier in the conversation, the photo may have changed. For the homelab (the user's home Kubernetes cluster), say in plain words if all is fine"
        " and mention only real problems, GPU temperatures and free disk. You cannot do lights or music: say so"
        " simply. Never pretend or "
        "promise to do things you cannot do. Reply ONLY with the words to speak aloud: no stage directions, "
        "no emoji, no markdown, no lists. Keep it to 1-3 short sentences.",
    )
    llm_timeout_s: float = float(_get("LLM_TIMEOUT_S", "120"))
    # Ollama only: how long the model stays loaded after a reply ("0" = unload right away,
    # freeing its RAM before Rocky's voice runs). Empty = don't send it (server default).
    llm_keep_alive: str = _get("LLM_KEEP_ALIVE", "")
    # Qwen3-style thinking models on vLLM: "false" skips the reasoning step (~1 s instead of 4-9 s per reply on
    # qwen3.8-27b, same answers for short voice questions). Empty = don't send it (model/template default).
    llm_enable_thinking: str = _get("LLM_ENABLE_THINKING", "")
    # Tools (app/tools.py): time, weather, news, math, Wikipedia. Needs internet from the API's host/pod.
    tools_enabled: bool = _get("TOOLS_ENABLED", "true").lower() == "true"
    # Home for the weather/time tools: the default place and timezone (e.g. HOME_NAME=Madrid).
    home_name: str = _get("HOME_NAME", "Madrid")
    home_lat: float = float(_get("HOME_LAT", "40.4168"))
    home_lon: float = float(_get("HOME_LON", "-3.7038"))
    home_tz: str = _get("HOME_TZ", "Europe/Madrid")
    # The news tool's "local" region: comma-separated RSS URLs, and what to call them. Empty = no local news.
    local_news_feeds: str = _get("LOCAL_NEWS_FEEDS", "")
    local_news_name: str = _get("LOCAL_NEWS_NAME", "")
    # Short-term memory, so follow-ups work ("and tomorrow?", "yes, tell me more"): the last few exchanges,
    # forgotten after a quiet spell. 0 turns = every question stands alone.
    history_turns: int = int(_get("HISTORY_TURNS", "4"))
    history_ttl_s: float = float(_get("HISTORY_TTL_S", "300"))
    # Timers/reminders file (alerts.json). Mount it on persistent storage to survive restarts.
    state_dir: str = _get("STATE_DIR", "state")
    # Homelab status tool: DCGM exporter metrics URL (GPUs) and a path on the GPU node's disk. Empty = skip.
    homelab_dcgm_url: str = _get("HOMELAB_DCGM_URL", "")
    homelab_disk_path: str = _get("HOMELAB_DISK_PATH", "")
    # Telegram bot for "send it to my phone": the bot's token and the user's own chat id (the only recipient).
    telegram_bot_token: str = _get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _get("TELEGRAM_CHAT_ID", "")
    # Let the user send the bot photos for Rocky to look at (photos.py). Off by default: it's the only inbound path to
    # the AI, so it stays narrow: that one chat, photos only, answered with no tools.
    telegram_photos: bool = _get("TELEGRAM_PHOTOS", "false").lower() == "true"
    # Also say photo answers on the speaker (short version), except during quiet hours ("start-end", local hours;
    # empty = never quiet). Quiet hours only affect these, not alarms or timers.
    photo_answers_on_speaker: bool = _get("PHOTO_ANSWERS_ON_SPEAKER", "true").lower() == "true"
    quiet_hours: str = _get("QUIET_HOURS", "23-8")

    # --- STT (faster-whisper) ---
    whisper_model: str = _get("WHISPER_MODEL", "base")
    whisper_device: str = _get("WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = _get("WHISPER_COMPUTE_TYPE", "int8")
    # Force a language (e.g. "en") or leave empty to auto-detect.
    whisper_language: str = _get("WHISPER_LANGUAGE", "")
    # Words Whisper should expect (initial_prompt), e.g. local place names. Empty = none.
    whisper_prompt: str = _get("WHISPER_PROMPT", "")

    # --- TTS (Rocky's voice: Qwen3-TTS Base, zero-shot clone) ---
    # Hugging Face id or local path of the Qwen3-TTS Base model.
    tts_model: str = _get("TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
    tts_device: str = _get("TTS_DEVICE", "cpu")  # or: cuda (bf16)
    # GPU engine: "faster" (default, faster-qwen3-tts with CUDA graphs) or "qwen" (plain qwen_tts). CPU: always qwen.
    tts_engine: str = _get("TTS_ENGINE", "faster")
    # The voice is cloned from this clip + its exact transcript (scripts/get_rocky_voice.sh).
    tts_ref_audio: str = _get("TTS_REF_AUDIO", "rocky/rocky_ref.wav")
    tts_ref_text: str = _get("TTS_REF_TEXT", "rocky/rocky_ref.txt")
    tts_temperature: float = float(_get("TTS_TEMPERATURE", "0.9"))
    # Language Qwen3-TTS speaks in (English, Spanish, ...). The voice was tuned in English.
    tts_language: str = _get("TTS_LANGUAGE", "English")

    # --- Output audio, shaped for the ESP32 I2S DAC (e.g. MAX98357A) ---
    # 16 kHz / 16-bit / mono is a safe default for small ESP32 speakers.
    output_sample_rate: int = int(_get("OUTPUT_SAMPLE_RATE", "16000"))

    # --- Input silence gate ---
    # A wake can be followed by nothing (the user said "Alexa" but no question). The
    # firmware still uploads the recording; the API measures its RMS and, if it is
    # below this level (dBFS, 0 dBFS = full scale), skips the STT/LLM/TTS pipeline,
    # counts it, and returns a short silent reply. Lower = stricter.
    # -55 matches the firmware's own VAD start level. The RMS covers the whole clip, lead-in silence included, so
    # real speech through the board's mics averages -45..-48 dBFS: -40 threw real questions away.
    silence_threshold_dbfs: float = float(_get("SILENCE_THRESHOLD_DBFS", "-55"))
    # Length in seconds of the silent "nothing heard" reply the ESP32 plays back.
    silence_reply_seconds: float = float(_get("SILENCE_REPLY_SECONDS", "0.4"))


settings = Settings()
