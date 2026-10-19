"""Download (or verify) the models the API loads, as configured in .env / the environment.

    .venv/bin/python scripts/download_models.py

Fetches the faster-whisper model (WHISPER_MODEL) and the Qwen3-TTS model (TTS_MODEL, which
includes its speech tokenizer) into the Hugging Face cache, so the first /talk doesn't stall on
a multi-GB download. With HF_HUB_OFFLINE=1 it only checks that both are already cached.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from faster_whisper.utils import download_model  # noqa: E402
from huggingface_hub import snapshot_download  # noqa: E402

from app.config import settings  # noqa: E402

print(f"Whisper STT : {settings.whisper_model}")
print(f"  -> {download_model(settings.whisper_model)}")

if os.path.isdir(settings.tts_model):
    print(f"Qwen3-TTS   : {settings.tts_model} (local directory, nothing to download)")
else:
    print(f"Qwen3-TTS   : {settings.tts_model}  (~2.4 GB for 0.6B)")
    print(f"  -> {snapshot_download(settings.tts_model)}")
print("Models ready.")
