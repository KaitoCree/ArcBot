#!/usr/bin/env bash
# arcbot installer for Oracle Linux 9 (aarch64 A1 or x86_64). Idempotent: safe to re-run for updates.
#
#   From a checkout of this repo on the server, as a sudo-capable user (opc):
#     bash deploy/install.sh            install/update, then (re)start the services
#     bash deploy/install.sh --no-start install/update only (first run: fill in .env, then start)
#
# Layout: /opt/arcbot/app (code + data/ + .env), /opt/arcbot/venv, system user "arcbot".
# Never disables SELinux; relabels /opt/arcbot instead. Opens no firewall ports (outbound HTTPS only).
set -euo pipefail

START=1
[[ "${1:-}" == "--no-start" ]] && START=0

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE=/opt/arcbot
APP="$BASE/app"
VENV="$BASE/venv"
PY=python3.12

say() { printf '\n==> %s\n' "$*"; }

say "Packages (python3.12, tesseract, git) from ol9_appstream"
sudo dnf -y -q install python3.12 python3.12-pip tesseract tesseract-langpack-eng git rsync

say "Service user"
if ! id arcbot &>/dev/null; then
  sudo useradd --system --home-dir "$BASE" --shell /sbin/nologin arcbot
fi
sudo mkdir -p "$APP/data/backups" "$APP/data/uploads"

say "Code -> $APP (keeps .env and data/)"
sudo rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude '.env' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' \
  "$SRC/" "$APP/"

say "Virtualenv + requirements"
if [[ ! -x "$VENV/bin/python" ]]; then
  sudo "$PY" -m venv "$VENV"
fi
sudo "$VENV/bin/python" -m pip install -q --upgrade pip
sudo "$VENV/bin/python" -m pip install -q -r "$APP/requirements.txt"
sudo "$VENV/bin/python" -c "import discord.ui as u; u.Label; u.FileUpload; import cv2, numpy, pytesseract" \
  || { echo "dependency check failed"; exit 1; }

say ".env"
if [[ ! -f "$APP/.env" ]]; then
  sudo cp "$APP/.env.example" "$APP/.env"
  echo "Created $APP/.env from the example. Fill in DISCORD_TOKEN and GUILD_ID:"
  echo "    sudo nano $APP/.env"
  START=0
fi
sudo chown -R arcbot:arcbot "$BASE"
sudo chmod 600 "$APP/.env"

say "Swap"
if [[ -z "$(swapon --show --noheadings)" ]]; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
  echo "swap already present: $(swapon --show --noheadings | awk '{print $1, $3}' | tr '\n' ' ')"
fi

say "systemd units + journald limits"
sudo install -m 644 "$APP/deploy/arcbot.service" /etc/systemd/system/arcbot.service
sudo install -m 644 "$APP/deploy/arcbot-keepalive.service" /etc/systemd/system/arcbot-keepalive.service
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\nMaxRetentionSec=30day\n' | sudo tee /etc/systemd/journald.conf.d/arcbot.conf >/dev/null
sudo systemctl restart systemd-journald
sudo systemctl daemon-reload

say "SELinux labels"
if command -v restorecon &>/dev/null; then
  sudo restorecon -R "$BASE" || true
fi

say "Self-check"
sudo -u arcbot bash -c "cd '$APP' && '$VENV/bin/python' -m arcbot --check" || true

if [[ "$START" == 1 ]]; then
  say "Starting services"
  sudo systemctl enable --now arcbot.service arcbot-keepalive.service
  sudo systemctl restart arcbot.service arcbot-keepalive.service
  sleep 3
  systemctl --no-pager --lines=5 status arcbot.service || true
else
  say "Not starting. When .env is ready:"
  echo "    sudo systemctl enable --now arcbot.service arcbot-keepalive.service"
fi
echo
echo "Logs:   journalctl -u arcbot -f        Keepalive: journalctl -u arcbot-keepalive -f"
