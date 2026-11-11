#!/usr/bin/env bash
# Install and start the assistant as a systemd service. Run on the Pi:  ./deploy/install-service.sh [--no-start]
# --no-start enables it for the next boot without starting it now. Needs sudo.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$DIR/.." && pwd)"
[ -x "$APP/venv/bin/python" ] || { echo "No venv at $APP/venv"; exit 1; }
[ -f "$APP/.env" ] || { echo "No $APP/.env (needs GEMINI_API_KEY)"; exit 1; }

sed -e "s|__USER__|$(id -un)|g" -e "s|__APP__|$APP|g" "$DIR/gemini-assistant.service" \
  | sudo tee /etc/systemd/system/gemini-assistant.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable gemini-assistant
if [ "${1:-}" != "--no-start" ]; then
  sudo systemctl restart gemini-assistant
fi
echo
echo "Installed. Useful commands:"
echo "  systemctl status gemini-assistant          # is it running?"
echo "  sudo journalctl -u gemini-assistant -f     # live log"
echo "  sudo systemctl restart gemini-assistant    # after changing code or .env"
echo "  sudo systemctl disable --now gemini-assistant   # turn autostart off"
