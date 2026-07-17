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
        value = value.strip().strip('"').strip("'")
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
    system_prompt: str = _get(
        "SYSTEM_PROMPT",
        "You are a helpful voice assistant. Answer in a few short, natural "
        "sentences suitable for being read aloud. Do not use markdown, lists, "
        "or emoji.",
    )
    llm_timeout_s: float = float(_get("LLM_TIMEOUT_S", "120"))

    # --- STT (faster-whisper) ---
    whisper_model: str = _get("WHISPER_MODEL", "base")
    whisper_device: str = _get("WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = _get("WHISPER_COMPUTE_TYPE", "int8")
    # Force a language (e.g. "en") or leave empty to auto-detect.
    whisper_language: str = _get("WHISPER_LANGUAGE", "")

    # --- TTS (Piper standalone binary) ---
    # Path to the piper executable (from scripts/download_piper.sh) or "piper"
    # if it's on your PATH.
    piper_bin: str = _get("PIPER_BIN", "piper/piper")
    # Path to a Piper .onnx voice; its <name>.onnx.json config must sit beside it.
    piper_model: str = _get("PIPER_MODEL", "voices/en_US-lessac-medium.onnx")

    # --- Output audio, shaped for the ESP32 I2S DAC (e.g. MAX98357A) ---
    # 16 kHz / 16-bit / mono is a safe default for small ESP32 speakers.
    output_sample_rate: int = int(_get("OUTPUT_SAMPLE_RATE", "16000"))


settings = Settings()
