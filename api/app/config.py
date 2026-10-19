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
        " talk about Erid, Grace, space or the movie unless the user asks. If you cannot know something (live"
        " weather, news, the time) or cannot do it (timers, lights, music), say so simply. Never pretend or "
        "promise to do things you cannot do. Reply ONLY with the words to speak aloud: no stage directions, "
        "no emoji, no markdown, no lists. Keep it to 1-3 short sentences.",
    )
    llm_timeout_s: float = float(_get("LLM_TIMEOUT_S", "120"))
    # Ollama only: how long the model stays loaded after a reply ("0" = unload right away,
    # freeing its RAM before Rocky's voice runs). Empty = don't send it (server default).
    llm_keep_alive: str = _get("LLM_KEEP_ALIVE", "")

    # --- STT (faster-whisper) ---
    whisper_model: str = _get("WHISPER_MODEL", "base")
    whisper_device: str = _get("WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = _get("WHISPER_COMPUTE_TYPE", "int8")
    # Force a language (e.g. "en") or leave empty to auto-detect.
    whisper_language: str = _get("WHISPER_LANGUAGE", "")

    # --- TTS (Rocky's voice: Qwen3-TTS Base, zero-shot clone) ---
    # Hugging Face id or local path of the Qwen3-TTS Base model.
    tts_model: str = _get("TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
    tts_device: str = _get("TTS_DEVICE", "cpu")  # or: cuda (bf16)
    # The voice is cloned from this clip + its exact transcript (scripts/get_rocky_voice.sh).
    tts_ref_audio: str = _get("TTS_REF_AUDIO", "rocky/rocky_ref.wav")
    tts_ref_text: str = _get("TTS_REF_TEXT", "rocky/rocky_ref.txt")
    tts_temperature: float = float(_get("TTS_TEMPERATURE", "0.9"))
    # Language Qwen3-TTS speaks in (English, Spanish, ...). The voice was tuned in English.
    tts_language: str = _get("TTS_LANGUAGE", "English")

    # --- Output audio, shaped for the ESP32 I2S DAC (e.g. MAX98357A) ---
    # 16 kHz / 16-bit / mono is a safe default for small ESP32 speakers.
    output_sample_rate: int = int(_get("OUTPUT_SAMPLE_RATE", "16000"))


settings = Settings()
