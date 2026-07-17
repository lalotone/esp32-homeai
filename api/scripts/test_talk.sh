#!/usr/bin/env bash
# End-to-end smoke test without an ESP32.
#   ./scripts/test_talk.sh input.wav [http://localhost:8000]
# Posts a WAV to /talk and saves the spoken reply to reply.wav.
set -euo pipefail

IN="${1:-input.wav}"
HOST="${2:-http://localhost:8000}"

if [[ ! -f "$IN" ]]; then
  echo "No input file '$IN'."
  echo "Make one from your mic (needs ffmpeg):"
  echo "  ffmpeg -f alsa -i default -ac 1 -ar 16000 -sample_fmt s16 -t 4 input.wav"
  exit 1
fi

echo "POST $HOST/talk  <-  $IN"
curl -sS -D - -o reply.wav \
  -F "audio=@${IN};type=audio/wav" \
  "${HOST}/talk" | grep -iE '^x-(transcript|reply):' || true

echo "Saved reply.wav"
