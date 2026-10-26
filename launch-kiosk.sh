#!/bin/bash
# Fullscreen now-playing display. Close it with Super+Q; the listener keeps running.
set -euo pipefail

omarchy-toggle-idle stay-awake >/dev/null 2>&1 || true
hyprctl keyword cursor:inactive_timeout 1 >/dev/null 2>&1 || true

for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8787/health >/dev/null && break
  sleep 0.5
done

exec chromium \
  --app=http://127.0.0.1:8787/ \
  --class=nowplaying \
  --kiosk \
  --start-fullscreen \
  --no-first-run \
  --no-default-browser-check \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --disable-features=Translate,HardwareMediaKeyHandling \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --user-data-dir="${HOME}/.local/share/nowplaying/chrome-profile"
