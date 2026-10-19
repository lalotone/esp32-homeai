#!/usr/bin/env bash
# First-time setup for the API: venv + deps, .env, Rocky's voice reference, model downloads.
# Safe to re-run; every step skips what's already done.
#
#   ./scripts/first_run.sh                 # CPU (default)
#   TORCH=cu128 ./scripts/first_run.sh     # NVIDIA GPU (RTX 50-series needs cu128), then set TTS_DEVICE=cuda in .env
#   ROCKY_REPO=/path/to/rocky ./scripts/first_run.sh   # where to copy the voice reference from
#
# Downloads on a fresh machine: Python packages (~1 GB with CPU torch, several GB with CUDA torch),
# Qwen3-TTS (~2.4 GB) and the Whisper model (base ~150 MB). Offline check on a machine that already has them:
#   UV_OFFLINE=1 HF_HUB_OFFLINE=1 ./scripts/first_run.sh
set -euo pipefail

cd "$(dirname "$0")/.."
TORCH="${TORCH:-cpu}"
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

echo "1/5 Python environment"
command -v uv >/dev/null || { echo "uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[[ -x .venv/bin/python ]] || uv venv --python 3.12 .venv
if [[ "$TORCH" != "cpu" ]]; then
  # Install the GPU build first; requirements.txt's torch==2.9.1 is then already satisfied.
  uv pip install --python .venv --index-url "https://download.pytorch.org/whl/${TORCH}" torch==2.9.1 torchaudio==2.9.1
fi
uv pip install --python .venv --index-strategy unsafe-best-match -r requirements.txt
ok "dependencies installed (torch: ${TORCH})"

echo "2/5 Configuration"
if [[ -f .env ]]; then
  ok ".env exists (left untouched)"
else
  cp .env.example .env
  ok "created .env from .env.example. Edit LLM_BASE_URL / LLM_MODEL for your LLM"
fi
[[ "$TORCH" != "cpu" ]] && warn "GPU: set TTS_DEVICE=cuda, WHISPER_DEVICE=cuda, WHISPER_COMPUTE_TYPE=float16 in .env"

echo "3/5 Rocky's voice reference"
VOICE_OK=1
if [[ -f rocky/rocky_ref.wav && -f rocky/rocky_ref.txt ]]; then
  ok "rocky/rocky_ref.{wav,txt} present"
elif [[ -d "${ROCKY_REPO:-$HOME/src/repos/rocky}/voice" ]]; then
  ./scripts/get_rocky_voice.sh "${ROCKY_REPO:-$HOME/src/repos/rocky}"
  ok "copied from the Rocky voice project"
else
  VOICE_OK=0
  warn "missing rocky/rocky_ref.wav + rocky/rocky_ref.txt (not in git: film audio)."
  warn "See README → \"Voice reference (private, not in git)\" for how to provide them."
fi

echo "4/5 Models"
.venv/bin/python scripts/download_models.py

echo "5/5 LLM endpoint"
LLM_URL="$(.venv/bin/python -c 'import sys; sys.path.insert(0, "."); from app.config import settings; print(settings.llm_base_url.rstrip("/"))')"
if curl -sf -m 5 "${LLM_URL}/models" >/dev/null; then
  ok "${LLM_URL} answers"
else
  warn "${LLM_URL} not reachable yet. Start Ollama/vLLM or fix LLM_BASE_URL in .env"
fi

echo
if [[ $VOICE_OK -eq 1 ]]; then
  echo "Ready. Start it with:  MALLOC_ARENA_MAX=2 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
  echo "or as a service:       ./scripts/install_service.sh --enable"
else
  echo "Almost ready: add the voice reference (step 3), then start the API."
  exit 1
fi
