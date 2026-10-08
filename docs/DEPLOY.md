# Deploying

arcbot targets **Oracle Cloud Always Free** (VM.Standard.A1.Flex, Oracle Linux 9, aarch64) but runs on any
always-on Linux box with Python 3.12 and Tesseract.

## Install

```bash
sudo dnf -y install git
git clone <this repo> ~/arcbot-src
cd ~/arcbot-src && bash deploy/install.sh --no-start
sudo nano /opt/arcbot/app/.env        # DISCORD_TOKEN, GUILD_ID
sudo systemctl enable --now arcbot arcbot-keepalive
```

`deploy/install.sh` is idempotent (re-run it to update). It:
- installs `python3.12`, `tesseract`, `tesseract-langpack-eng`, `git` from `ol9_appstream`,
- creates a system user `arcbot`, copies the code to `/opt/arcbot/app` (keeping `.env` and `data/`),
  builds a venv in `/opt/arcbot/venv`,
- adds swap if there is none, installs the `arcbot` and `arcbot-keepalive` systemd units and journald limits,
- relabels `/opt/arcbot` for SELinux (it stays Enforcing) and runs `python -m arcbot --check`.

No inbound ports are needed; the bot only makes outbound HTTPS connections.

## Updating

```bash
cd ~/arcbot-src && git pull && bash deploy/install.sh
```

Database migrations run on startup. Button panels keep working across restarts.

## Health

- `systemctl status arcbot`, `journalctl -u arcbot -f`. The service restarts 5 s after any crash, and crashes
  post a short rate-limited alert in `#mod-review`.
- On startup the bot logs the same report as `/admin setup-check` (roles, channels, permissions, intents,
  Tesseract).

## Backups

- Nightly at `backups.time_local` (server time): SQLite online backup, integrity check, gzip, keep 14, in
  `/opt/arcbot/app/data/backups/`. Failures alert `#mod-review`.
- On demand: `/admin backup-now` or `bash deploy/backup.sh`.
- Optional off-box copy (`backups.git_push: true`): the newest backup is committed as `latest.db.gz` to a
  **private** git repo cloned at `data/backups/repo` with a read-write deploy key for the `arcbot` user.

Restore (same or fresh machine): install with `--no-start`, put `.env` back, then
`bash deploy/backup.sh restore /path/to/arcbot-YYYYMMDD-HHMMSS.db.gz`.

## Keepalive (Always Free idle reclamation)

Oracle may reclaim an Always Free instance that looks idle over 7 days: 95th-percentile CPU < 20%, network
< 20%, and (A1 shapes) memory < 20%, all at once. A quiet Discord bot is idle by all three measures.

`arcbot-keepalive` is a separate low-priority service that keeps the box above the memory line:
- holds real, resident memory so the whole machine sits near `keepalive.memory_hold_percent` (default 26%),
  never above `memory_ceiling_percent`, and gives memory back first if the machine runs short;
- optional hourly CPU burst on one core (`cpu_burst_enabled`, off by default) with weekly self-tuning;
- samples CPU and memory each minute and logs a weekly summary
  (`python -m arcbot.services.keepalive --report` from `/opt/arcbot/app` prints it on demand).

Check after a day: OCI Console → Compute → instance → Metrics → Memory Utilization ≈ 25%.
Alternatively, upgrading the tenancy to Pay As You Go exempts it from reclamation while staying free within the
Always Free limits; then set `keepalive.enabled: false`.
