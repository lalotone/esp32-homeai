#!/usr/bin/env bash
# Download the default Piper voice (en_US-lessac-medium) into ./voices/.
# Override with: NAME and REL, e.g. NAME=en_US-amy-medium REL=en/en_US/amy/medium
set -euo pipefail

NAME="${NAME:-en_US-lessac-medium}"
REL="${REL:-en/en_US/lessac/medium}"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/${REL}"

mkdir -p voices
for ext in onnx onnx.json; do
  echo "Downloading ${NAME}.${ext} ..."
  curl -L --fail -o "voices/${NAME}.${ext}" "${BASE}/${NAME}.${ext}"
done
echo "Done -> voices/${NAME}.onnx"
