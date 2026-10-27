#!/usr/bin/env bash
# Installs the marquee kiosk for the current user on Linux.
#
#   ./install.sh                      install or upgrade
#   ./install.sh --skip-packages      skip the system package step
#   ./install.sh --uninstall          remove the service and app (keeps config)
#   ./install.sh --uninstall --purge  also remove config, Chrome profile, and models
#
# Run as your normal user. System packages are installed with sudo.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${HOME:?HOME is not set}/.local/share/marquee"
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"
PORT=8787
APP_FILES=(server.py cinema.py voice.py index.html setup.html)

MODE=install
PURGE=0
SKIP_PACKAGES=0

for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE=uninstall ;;
    --purge) PURGE=1 ;;
    --skip-packages) SKIP_PACKAGES=1 ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[[ "$(uname -s)" == Linux ]] || die "this installer is for Linux only"
[[ $EUID -ne 0 ]] || die "run as your normal user, not root (sudo is used for packages)"
[[ $PURGE -eq 0 || $MODE == uninstall ]] || die "--purge only works with --uninstall"

uninstall() {
  say "removing marquee"
  if have systemctl; then
    systemctl --user disable --now marquee.service >/dev/null 2>&1 || true
  fi
  rm -f "$UNITS/marquee.service"
  if have systemctl; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  rm -f "$BIN/marquee-kiosk"
  for f in "${APP_FILES[@]}"; do rm -f "$DATA/$f"; done
  rm -rf "$DATA/fonts" "$DATA/venv"
  if [[ $PURGE -eq 1 ]]; then
    rm -rf "$DATA"
    say "removed $DATA"
  else
    say "kept $DATA (config, Chrome profile, voice model). Use --purge to delete it."
  fi
  say "done. System packages were left installed."
}

install_packages() {
  if have apt-get; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv ffmpeg alsa-utils pulseaudio-utils chromium curl
  elif have pacman; then
    sudo pacman -S --needed --noconfirm python ffmpeg alsa-utils libpulse chromium curl
  elif have dnf; then
    sudo dnf install -y python3 ffmpeg alsa-utils pulseaudio-utils chromium curl
  else
    die "no supported package manager (apt, pacman, dnf). Install python3 with venv, ffmpeg, alsa-utils, pactl, chromium, and curl, then rerun with --skip-packages"
  fi
}

check_tools() {
  have python3 || die "python3 is not installed"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is required"
  local missing=()
  for tool in ffmpeg arecord pactl chromium curl; do
    have "$tool" || missing+=("$tool")
  done
  if ((${#missing[@]})); then
    warn "missing commands: ${missing[*]}"
  fi
  if ! have songrec; then
    warn "songrec not found, so album recognition will not work yet. On Arch install songrec-bin from the AUR. Elsewhere run: cargo install songrec"
  fi
  if ! have hyprctl; then
    warn "hyprctl not found. The kiosk launcher expects Hyprland (Wayland) for fullscreen and Super+Q"
  fi
}

install_app() {
  say "copying app to $DATA"
  if [[ "$SRC" == "$DATA" ]]; then
    die "run install.sh from the repo, not from $DATA"
  fi
  mkdir -p "$DATA" "$BIN" "$UNITS"
  for f in "${APP_FILES[@]}"; do
    install -m 644 "$SRC/$f" "$DATA/$f"
  done
  rm -rf "$DATA/fonts"
  cp -R "$SRC/fonts" "$DATA/fonts"
  install -m 755 "$SRC/launch-kiosk.sh" "$BIN/marquee-kiosk"
  # Keep an existing config so reinstalling does not reset the theater.
  if [[ ! -f "$DATA/config.json" ]]; then
    install -m 644 "$SRC/config.example.json" "$DATA/config.json"
  fi
}

setup_venv() {
  say "setting up Python environment for voice commands"
  if [[ ! -x "$DATA/venv/bin/python" ]]; then
    python3 -m venv "$DATA/venv" \
      || die "could not create a virtualenv. On Debian/Ubuntu install python3-venv"
  fi
  if ! "$DATA/venv/bin/pip" install --quiet --disable-pip-version-check vosk; then
    warn "vosk did not install, so voice commands will be off"
  fi
}

install_service() {
  say "installing systemd user service"
  install -m 644 "$SRC/marquee.service" "$UNITS/marquee.service"
  if ! have systemctl || ! systemctl --user show-environment >/dev/null 2>&1; then
    warn "no systemd user session found. Start it later with: systemctl --user enable --now marquee.service"
    return
  fi
  systemctl --user daemon-reload
  systemctl --user enable marquee.service >/dev/null
  systemctl --user restart marquee.service
}

wait_for_server() {
  for _ in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      say "marquee is running at http://127.0.0.1:$PORT/"
      return
    fi
    sleep 0.5
  done
  warn "server did not answer on port $PORT. Check: journalctl --user -u marquee -e"
}

if [[ $MODE == uninstall ]]; then
  uninstall
  exit 0
fi

if [[ $SKIP_PACKAGES -eq 0 ]]; then
  say "installing system packages"
  install_packages
fi
check_tools
install_app
setup_venv
install_service
wait_for_server

cat <<EOF

Installed. The listener runs as a user service and starts at login.

  Pick a theater:  http://127.0.0.1:$PORT/setup
  Fullscreen:      $BIN/marquee-kiosk
                   Close it with Super+Q. The listener keeps running.

Start the display at login:
  Omarchy, in ~/.config/hypr/autostart.lua:
    o.launch_on_start("$BIN/marquee-kiosk")
    o.window({ class = "marquee" }, { fullscreen = true })
  Other Hyprland setups, in hyprland.conf:
    exec-once = $BIN/marquee-kiosk

The first run downloads a small speech model for voice commands.
EOF
