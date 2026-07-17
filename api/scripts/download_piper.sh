#!/usr/bin/env bash
# Download the standalone Piper binary into ./piper/ (Linux x86_64).
# Bundles the phonemizer + espeak-ng data, so no piper-phonemize pip wheel needed.
set -euo pipefail

VERSION="${VERSION:-2023.11.14-2}"
URL="https://github.com/rhasspy/piper/releases/download/${VERSION}/piper_linux_x86_64.tar.gz"

echo "Downloading Piper ${VERSION} ..."
curl -L --fail -o /tmp/piper.tar.gz "$URL"
tar -xzf /tmp/piper.tar.gz          # extracts a ./piper/ directory
rm -f /tmp/piper.tar.gz
echo "Done -> ./piper/piper"
./piper/piper --help >/dev/null 2>&1 && echo "Binary runs OK." || echo "Warning: binary did not run; check glibc/arch."
