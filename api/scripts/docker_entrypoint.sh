#!/bin/sh
# Container entrypoint: check the voice reference, fetch/verify the models, then serve.
set -eu

if [ ! -f "${TTS_REF_AUDIO:-rocky/rocky_ref.wav}" ] || [ ! -f "${TTS_REF_TEXT:-rocky/rocky_ref.txt}" ]; then
  echo "Rocky's voice reference is missing. Mount a folder with rocky_ref.wav + rocky_ref.txt at /app/rocky," >&2
  echo "e.g.  -v \$PWD/rocky:/app/rocky:ro   (how to get them: api/rocky/README.md)" >&2
  exit 1
fi

# Idempotent: downloads into the /cache/hf volume on the first start, then only checks the cache.
# Set HF_HUB_OFFLINE=1 to skip the network check once the volume is filled.
python scripts/download_models.py

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
