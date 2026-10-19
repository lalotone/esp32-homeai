#!/usr/bin/env bash
# Install the API as a systemd *user* service (no root): starts at login, restarts if it crashes.
#   ./scripts/install_service.sh            # write the unit only
#   ./scripts/install_service.sh --enable   # write it and start it now + at every login
# Logs:   journalctl --user -u esp32-homeai-api -f
# Remove: systemctl --user disable --now esp32-homeai-api && rm ~/.config/systemd/user/esp32-homeai-api.service
#
# To keep it running while logged out (e.g. a headless box): sudo loginctl enable-linger "$USER"
set -euo pipefail

API_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="${UNIT_DIR}/esp32-homeai-api.service"
PORT="${PORT:-8000}"

[[ -x "${API_DIR}/.venv/bin/uvicorn" ]] || { echo "No .venv yet: run ./scripts/first_run.sh first"; exit 1; }

mkdir -p "${UNIT_DIR}"
cat > "${UNIT}" <<EOF
[Unit]
Description=ESP32 voice assistant API (Whisper -> LLM -> Rocky's voice)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=${API_DIR}
# glibc per-thread memory pools grow a lot with torch + a threadpool; cap them.
Environment=MALLOC_ARENA_MAX=2
ExecStart=${API_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=10
# Loading Rocky's voice takes ~30 s on a CPU.
TimeoutStartSec=300

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
echo "Wrote ${UNIT}"

if [[ "${1:-}" == "--enable" ]]; then
  systemctl --user enable --now esp32-homeai-api
  echo "Started. Status: systemctl --user status esp32-homeai-api"
else
  echo "Enable with: systemctl --user enable --now esp32-homeai-api"
fi
