#!/usr/bin/env bash
# Copy Rocky's voice reference (clip + exact transcript) from the Rocky voice project into ./rocky/.
#   ./scripts/get_rocky_voice.sh [path/to/rocky/repo]     (default: ~/src/repos/rocky)
# The clip is cut from film audio, so it stays out of git (.gitignore) — personal use only.
# "golden" = the reference picked by ear: the favourite take first, then Rocky's film lines.
set -euo pipefail

ROCKY_REPO="${1:-$HOME/src/repos/rocky}"
SRC="${ROCKY_REPO}/voice"
DEST="$(cd "$(dirname "$0")/.." && pwd)/rocky"

for f in rocky_ref_golden.wav rocky_ref_golden.txt; do
  [[ -f "${SRC}/${f}" ]] || { echo "Missing ${SRC}/${f} — is ${ROCKY_REPO} the Rocky voice project?"; exit 1; }
done

mkdir -p "${DEST}"
cp "${SRC}/rocky_ref_golden.wav" "${DEST}/rocky_ref.wav"
cp "${SRC}/rocky_ref_golden.txt" "${DEST}/rocky_ref.txt"
echo "Rocky voice -> ${DEST}/rocky_ref.{wav,txt}"
