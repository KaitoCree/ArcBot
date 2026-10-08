"""The discord.py client: loads cogs, registers persistent views, syncs guild commands, runs the startup
self-check, nightly backups and the unhandled-error alert."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, time as dtime

import discord
from discord.ext import commands, tasks

from .app import App
from .config import ConfigError, load_config
from .copytext import load_copy
from .db import connect

log = logging.getLogger("arcbot")

COGS = ["onboarding", "promotion", "vouch", "jobs", "modtools", "timers"]


def _backup_time(cfg) -> dtime:
    hh, mm = str(cfg.backups.get("time_local", "03:30")).split(":")
    # discord.ext.tasks needs an aware time; use the host's local zone
    return dtime(int(hh), int(mm), tzinfo=datetime.now().astimezone().tzinfo)


class ArcBot(commands.Bot):
    def __init__(self, app: App):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # privileged: join handling, role sync
        intents.guild_messages = True
        intents.message_content = True  # privileged: only used to read #vouch
        intents.guild_reactions = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         allowed_mentions=discord.AllowedMentions.none())
        self.app = app
        from .cogs.intake_ui import IntakeFlow

        self.intake_flow = IntakeFlow(app)
        self._ready_once = False

    async def setup_hook(self) -> None:
        from .cogs.intake_ui import ReviewButton
        from .cogs.jobs import JobButton, JobReviewButton

        self.add_dynamic_items(ReviewButton, JobButton, JobReviewButton)
        for name in COGS:
            await self.load_extension(f"arcbot.cogs.{name}")
        if self.app.guild_id:
            guild = discord.Object(id=self.app.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d slash commands to guild %s", len(synced), self.app.guild_id)
        if self.app.cfg.backups.get("enabled", True):
            self.nightly_backup.change_interval(time=_backup_time(self.app.cfg))
            self.nightly_backup.start()

    async def on_ready(self) -> None:
        app = self.app
        log.info("logged in as %s (%s)%s", self.user, self.user.id if self.user else "?",
                 " [DRY RUN]" if app.dry_run else "")
        if self._ready_once:
            return  # reconnects fire on_ready again; panels and checks only once
        self._ready_once = True
        guild = self.get_guild(app.guild_id) if app.guild_id else None
        if guild is None:
            log.error("bot is not in guild %s (check GUILD_ID / invite)", app.guild_id)
            return
        app.resolve(guild)
        from .cogs.modtools import setup_report

        for line in setup_report(app, self):
            (log.warning if line.startswith(("❌", "⚠️")) else log.info)("setup: %s", line)
        removed = self.intake_flow.cleanup_orphan_uploads()
        if removed:
            log.info("deleted %d orphan uploads", removed)
        for cog_name in ("Onboarding", "Promotion", "Jobs"):
            cog = self.get_cog(cog_name)
            if cog is not None:
                try:
                    await cog.ensure_panels()  # type: ignore[attr-defined]
                except discord.HTTPException as exc:
                    log.error("panel for %s failed: %s", cog_name, exc)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        log.exception("unhandled error in %s", event_method)
        exc = sys.exc_info()[1]
        await self.app.alerts.alert(f"error:{event_method}",
                                    f"Unhandled error in {event_method}: {type(exc).__name__}. Details are in the logs.")

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("command error", exc_info=error)

    @tasks.loop(time=dtime(3, 30))
    async def nightly_backup(self) -> None:
        from .services.backup import run_backup

        try:
            path = await asyncio.get_running_loop().run_in_executor(None, run_backup, self.app.conn, self.app.cfg)
            log.info("nightly backup ok: %s", path.name)
        except Exception as exc:  # noqa: BLE001
            log.exception("nightly backup failed")
            await self.app.alerts.alert("backup_failed", f"Nightly backup failed: {exc}")


def run() -> int:
    try:
        cfg = load_config()
        copy = load_copy()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    token = os.environ.get("DISCORD_TOKEN")
    guild_id = os.environ.get("GUILD_ID")
    if not token or not guild_id:
        log.error("DISCORD_TOKEN and GUILD_ID must be set in .env")
        return 2
    dry = os.environ.get("ARCBOT_DRY_RUN") == "1"
    conn = connect()
    app = App(cfg, copy, conn, dry_run=dry, guild_id=int(guild_id))
    bot = ArcBot(app)
    try:
        bot.run(token, log_handler=None)
    finally:
        conn.close()
    return 0
