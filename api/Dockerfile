# ESP32 voice assistant API: Whisper STT -> LLM (external) -> Rocky's voice (Qwen3-TTS).
#
#   docker build -t esp32-homeai-api:cpu .                          # CPU (default)
#   docker build -t esp32-homeai-api:cuda --build-arg TORCH=cu128 . # NVIDIA GPU (RTX 50-series needs cu128)
#
# Not baked in, mounted at runtime (see docker-compose.yml / README "Docker"):
#   /app/rocky   Rocky's voice reference (private: cut from film audio)  -> read-only bind mount
#   /cache/hf    Hugging Face cache (Whisper + Qwen3-TTS, ~2.5 GB)       -> volume, filled on first start
FROM python:3.12-slim-bookworm

# cpu | cu128 (or another PyTorch wheel index name, e.g. cu126)
ARG TORCH=cpu

# sox: qwen-tts imports the `sox` module, which warns without the binary. soundfile/av wheels bundle their libs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sox \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

WORKDIR /app
COPY requirements.txt .
# The GPU build installs CUDA torch first; requirements.txt's torch==2.9.1 is then already satisfied.
RUN if [ "$TORCH" != "cpu" ]; then \
      uv pip install --system --no-cache --index-url "https://download.pytorch.org/whl/${TORCH}" \
        torch==2.9.1 torchaudio==2.9.1; \
    fi \
 && uv pip install --system --no-cache --index-strategy unsafe-best-match -r requirements.txt

COPY app/ app/
COPY scripts/download_models.py scripts/docker_entrypoint.sh scripts/

# faster-whisper (ctranslate2) on CUDA needs cuBLAS/cuDNN: reuse the ones CUDA torch installed as pip packages.
# Harmless on the CPU image (the directories just don't exist).
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib \
    HF_HOME=/cache/hf \
    MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 app && mkdir -p /cache/hf /app/rocky && chown -R app /cache /app
USER app

EXPOSE 8000
# Generous start period: the first start downloads ~2.5 GB of models, and loading the voice takes ~30 s on a CPU.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20m \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

ENTRYPOINT ["scripts/docker_entrypoint.sh"]
