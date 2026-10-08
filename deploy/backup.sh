#!/usr/bin/env bash
# Manual backup / restore wrappers. The bot already backs up nightly on its own.
#   bash deploy/backup.sh                       write a backup now
#   bash deploy/backup.sh restore FILE.db.gz    stop bot, restore FILE over the live DB, start bot
set -euo pipefail
APP=/opt/arcbot/app
PY=/opt/arcbot/venv/bin/python

if [[ "${1:-}" == "restore" ]]; then
  FILE="$(readlink -f "${2:?usage: backup.sh restore FILE.db.gz}")"
  sudo systemctl stop arcbot
  sudo -u arcbot bash -c "cd '$APP' && '$PY' -m arcbot.services.backup --restore '$FILE'"
  sudo systemctl start arcbot
  echo "restored $FILE and restarted arcbot"
else
  sudo -u arcbot bash -c "cd '$APP' && '$PY' -m arcbot.services.backup"
fi
