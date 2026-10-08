"""Mod slash commands. All ephemeral and mod-gated; the only place point values are ever shown."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..app import OPTIONAL_PERMS, REQUIRED_PERMS
from ..db import transaction
from ..ocr import reader
from ..services.backup import run_backup

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("arcbot.mod")


def _app(interaction: discord.Interaction) -> "App":
    return interaction.client.app  # type: ignore[attr-defined]


def mod_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        app = _app(interaction)
        if not app.is_mod(interaction.user):
            await interaction.response.send_message(app.copy.t("errors.not_allowed"), ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


def gm_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        app = _app(interaction)
        if not app.is_guild_master(interaction.user):
            await interaction.response.send_message(app.copy.t("errors.not_allowed"), ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


def _rank_choices(app: "App") -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=app.display_rank(k), value=k) for k in app.ranks.keys]


def setup_report(app: "App", bot: commands.Bot) -> list[str]:
    """Plain-text lines for /admin setup-check (also used at startup)."""
    lines: list[str] = []
    guild = app.guild
    if guild is None:
        return ["❌ bot is not in the configured guild (check GUILD_ID)"]
    r = app.resolved
    lines.append("✅ all roles found" if not r.missing_roles else f"❌ missing roles: {', '.join(r.missing_roles)}")
    lines.append("✅ all channels found" if not r.missing_channels
                 else f"❌ missing channels: {', '.join('#' + c for c in r.missing_channels)}")
    me = guild.me
    top = me.top_role
    managed = [x for x in [app.newcomer_role(), *[app.rank_role(k) for k in app.ranks.keys]] if x is not None]
    too_high = [x.name for x in managed if x >= top]
    lines.append(f"✅ bot role '{top.name}' is above all rank roles" if not too_high
                 else f"❌ drag the bot's role above: {', '.join(too_high)}")
    perms = me.guild_permissions
    missing = [p for p in REQUIRED_PERMS if not getattr(perms, p)]
    lines.append("✅ required permissions present" if not missing
                 else f"❌ missing permissions: {', '.join(missing)}")
    opt = [p for p in OPTIONAL_PERMS if not getattr(perms, p)]
    if opt:
        lines.append(f"ℹ️ optional permissions not granted: {', '.join(opt)} (pins/threads may not work)")
    if perms.administrator:
        lines.append("⚠️ the bot has Administrator. It doesn't need it; consider removing it.")
    for key, ch in r.channels.items():
        cp = ch.permissions_for(me)
        need = ["view_channel", "send_messages", "read_message_history"]
        if key == "vouch":
            need.append("add_reactions")
        lacking = [p for p in need if not getattr(cp, p)]
        if lacking:
            lines.append(f"❌ #{ch.name}: bot lacks {', '.join(lacking)}")
    intents = bot.intents
    lines.append(("✅" if intents.members else "❌") + " Server Members intent requested")
    lines.append(("✅" if intents.message_content else "❌") + " Message Content intent requested")
    if guild.member_count and len(guild.members) < guild.member_count * 0.9:
        lines.append("⚠️ member list looks incomplete: is the Server Members intent ON in the Developer Portal?")
    lines.append("✅ Tesseract available" if reader.available() else "⚠️ Tesseract not found: screenshots fall back to manual entry")
    lines.append("ℹ️ dry run is ON: nothing is changed on the server" if app.dry_run else "ℹ️ dry run is off")
    return lines


class ImportConfirm(discord.ui.View):
    def __init__(self, cog: "ModTools", invoker: int, plan: list[tuple[discord.Member, str]]):
        super().__init__(timeout=300)
        self.cog, self.invoker, self.plan = cog, invoker, plan

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker

    @discord.ui.button(label="Import now", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        app = self.cog.app
        done = skipped = 0
        with transaction(app.conn):
            for member, rank in self.plan:
                u = app.engine.get_user(member.id)
                if u is not None and u["rank_key"]:
                    skipped += 1
                    continue
                app.engine.place(member.id, rank, grant_veteran=rank == "veteran", seed_source="import")
                done += 1
        log.info("import-existing by %s: %s imported, %s already had records", interaction.user.id, done, skipped)
        self.stop()
        await interaction.response.edit_message(
            content=f"Imported {done} members ({skipped} already had records). Nobody's roles were changed.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Import cancelled.", view=None)


class ModTools(commands.Cog):
    admin = app_commands.Group(name="admin", description="arcbot admin tools (mods only)", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app: App = bot.app  # type: ignore[attr-defined]

    # ----------------------------------------------------------- read-only
    @app_commands.command(name="leaderboard", description="Mods only: hidden points ranking")
    @app_commands.guild_only()
    @mod_only()
    async def leaderboard(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 50] = 15) -> None:
        rows = self.app.conn.execute(
            "SELECT discord_id, ingame_name, rank_key, points, last_activity_at FROM users WHERE rank_key IS NOT NULL"
            " ORDER BY points DESC LIMIT ?", (limit,)).fetchall()
        lines = []
        for i, r in enumerate(rows, 1):
            last = (r["last_activity_at"] or "never")[:10]
            lines.append(f"{i}. <@{r['discord_id']}> ({r['ingame_name'] or '?'}) · {self.app.display_rank(r['rank_key'])}"
                         f" · **{r['points']}** pts · last active {last}")
        await interaction.response.send_message("\n".join(lines) or "Nobody placed yet.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="profile", description="Mods only: a member's rank, points and history")
    @app_commands.guild_only()
    @mod_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member) -> None:
        app = self.app
        u = app.engine.get_user(member.id)
        if u is None:
            await interaction.response.send_message(f"{member.mention} has no record yet.", ephemeral=True)
            return
        subs = app.conn.execute("SELECT COUNT(*), MAX(created_at) FROM stats_submissions WHERE discord_id = ?",
                                (member.id,)).fetchone()
        events = app.conn.execute("SELECT id, delta, source, reason, created_at FROM point_events WHERE discord_id = ?"
                                  " ORDER BY id DESC LIMIT 10", (member.id,)).fetchall()
        e = discord.Embed(title=f"{member.display_name} ({u['ingame_name'] or 'no in-game name'})")
        rank = app.display_rank(u["rank_key"]) if u["rank_key"] else "unplaced"
        if u["provisional"]:
            rank += " (provisional)"
        e.add_field(name="Rank", value=rank)
        e.add_field(name="Points", value=str(u["points"]))
        e.add_field(name="Veteran granted", value="yes" if u["veteran_granted"] else "no")
        e.add_field(name="Stats submissions", value=f"{subs[0]} (last {(subs[1] or 'never')[:10]})")
        e.add_field(name="Last active", value=(u["last_activity_at"] or "never")[:10])
        e.add_field(name="Last 10 point events", inline=False, value="\n".join(
            f"#{ev['id']} {ev['delta']:+d} {ev['source']}{' – ' + ev['reason'] if ev['reason'] else ''} ({ev['created_at'][:10]})"
            for ev in events) or "none")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="queue", description="Mods only: everything waiting for a mod")
    @app_commands.guild_only()
    @mod_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        app = self.app
        gid = interaction.guild_id

        def link(ch, msg):
            return f" [open](https://discord.com/channels/{gid}/{ch}/{msg})" if ch and msg else ""

        lines = [f"Stats #{s['id']}: <@{s['discord_id']}> claims {app.display_rank(s['assessed_rank'])} "
                 f"({s['created_at'][:10]}){link(s['review_channel_id'], s['review_message_id'])}"
                 for s in app.conn.execute("SELECT * FROM stats_submissions WHERE status='pending' ORDER BY id")]
        lines += [f"Job #{j['id']}: {j['title']} ({j['created_at'][:10]}){link(j['review_channel_id'], j['review_message_id'])}"
                  for j in app.conn.execute("SELECT * FROM jobs WHERE status IN ('pending_review','needs_mod') ORDER BY id")]
        await interaction.response.send_message("\n".join(lines) or "Queue is empty. 🎉", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ----------------------------------------------------------- points
    @app_commands.command(name="award", description="Mods only: bonus points for exceptional help")
    @app_commands.guild_only()
    @mod_only()
    async def award(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str) -> None:
        app = self.app
        lo, hi = app.cfg.mod_award_min, app.cfg.mod_award_max
        if not lo <= amount <= hi:
            await interaction.response.send_message(f"Amount must be between {lo} and {hi}.", ephemeral=True)
            return
        if len(reason.strip()) < 3:
            await interaction.response.send_message("Please give a reason.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("You can't award yourself.", ephemeral=True)
            return
        change = app.engine.add_points(member.id, amount, "mod_award", actor_id=interaction.user.id,
                                       reason=reason.strip()[:200])
        app.engine.touch_activity(member.id)
        log.info("award %s -> %s: %s (%s)", interaction.user.id, member.id, amount, reason)
        await interaction.response.send_message(f"Awarded {amount} to {member.mention}.", ephemeral=True)
        await app.apply_change(change, member)

    @app_commands.command(name="revoke-event", description="Mods only: cancel a point event by id")
    @app_commands.guild_only()
    @mod_only()
    async def revoke_event(self, interaction: discord.Interaction, event_id: int) -> None:
        app = self.app
        ev = app.conn.execute("SELECT * FROM point_events WHERE id = ?", (event_id,)).fetchone()
        if ev is None:
            await interaction.response.send_message("No such event.", ephemeral=True)
            return
        if ev["source"] == "revoke":
            await interaction.response.send_message("That event is itself a revoke.", ephemeral=True)
            return
        if app.conn.execute("SELECT 1 FROM point_events WHERE source='revoke' AND ref = ?", (str(event_id),)).fetchone():
            await interaction.response.send_message("Already revoked.", ephemeral=True)
            return
        app.engine.add_points(ev["discord_id"], -ev["delta"], "revoke", ref=str(event_id),
                              actor_id=interaction.user.id, reason=f"revoke #{event_id}")
        log.info("revoke %s by %s", event_id, interaction.user.id)
        await interaction.response.send_message(
            f"Revoked event #{event_id} ({ev['delta']:+d} {ev['source']}) for <@{ev['discord_id']}>. Ranks never go "
            "down, so their rank stays.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="set-rank", description="Guild Master only: place a member at a rank (upward only)")
    @app_commands.guild_only()
    @gm_only()
    async def set_rank(self, interaction: discord.Interaction, member: discord.Member, rank: str) -> None:
        app = self.app
        if rank not in app.ranks.keys:
            await interaction.response.send_message("Unknown rank.", ephemeral=True)
            return
        before = app.engine.get_user(member.id)
        change = app.engine.set_rank_by_mod(member.id, rank, grant_veteran=True)
        log.info("set-rank %s -> %s by %s", member.id, rank, interaction.user.id)
        if change is None:
            cur = before["rank_key"] if before else None
            await interaction.response.send_message(
                f"No change: {member.mention} is already {app.display_rank(cur) if cur else 'unplaced'} or higher "
                "(ranks never go down).", ephemeral=True)
            return
        await interaction.response.send_message(f"{member.mention} is now {app.display_rank(change.new)}.", ephemeral=True)
        await app.apply_change(change, member, name=member.display_name)

    @set_rank.autocomplete("rank")
    async def _rank_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [c for c in _rank_choices(self.app) if current.lower() in c.name.lower()][:25]

    # ----------------------------------------------------------- admin
    @admin.command(name="setup-check", description="Check roles, channels, permissions and intents")
    @mod_only()
    async def setup_check(self, interaction: discord.Interaction) -> None:
        if interaction.guild is not None:
            self.app.resolve(interaction.guild)
        lines = setup_report(self.app, self.bot)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @admin.command(name="import-existing", description="Create records for members who already hold a rank role")
    @mod_only()
    async def import_existing(self, interaction: discord.Interaction) -> None:
        app = self.app
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not guild.chunked:
            await guild.chunk()
        plan: list[tuple[discord.Member, str]] = []
        no_rank: list[discord.Member] = []
        already = 0
        for m in guild.members:
            if m.bot:
                continue
            held = [k for k in app.ranks.keys if (r := app.rank_role(k)) is not None and r in m.roles]
            if not held:
                no_rank.append(m)
                continue
            top = max(held, key=app.ranks.order)
            u = app.engine.get_user(m.id)
            if u is not None and u["rank_key"]:
                already += 1
                continue
            plan.append((m, top))
        counts: dict[str, int] = {}
        for _, k in plan:
            counts[k] = counts.get(k, 0) + 1
        summary = ", ".join(f"{app.display_rank(k)}: {n}" for k, n in counts.items()) or "nobody"
        sample = "\n".join(f"• {m.display_name} → {app.display_rank(k)}" for m, k in plan[:20])
        more = f"\n…and {len(plan) - 20} more" if len(plan) > 20 else ""
        nr = ", ".join(m.display_name for m in no_rank[:30]) + ("…" if len(no_rank) > 30 else "")
        text = (f"**Dry run.** Would import {len(plan)} members ({summary}); {already} already have records.\n"
                f"Points are seeded to each rank's threshold (Veteran → granted). No roles are changed.\n"
                f"{sample}{more}\n\n**{len(no_rank)} members have no rank role** and are left alone: {nr or 'none'}")
        view = ImportConfirm(self, interaction.user.id, plan) if plan else None
        await interaction.followup.send(text[:1990], ephemeral=True, view=view or discord.utils.MISSING)

    @admin.command(name="export", description="CSV of every member record (mods only)")
    @mod_only()
    async def export(self, interaction: discord.Interaction) -> None:
        rows = self.app.conn.execute(
            "SELECT discord_id, ingame_name, rank_key, provisional, veteran_granted, points, placed_at,"
            " last_activity_at, last_stats_submission_at FROM users ORDER BY points DESC").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(rows[0].keys() if rows else ["discord_id"])
        for r in rows:
            w.writerow(list(r))
        file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename="arcbot-members.csv")
        await interaction.response.send_message(f"{len(rows)} records.", file=file, ephemeral=True)

    @admin.command(name="backup-now", description="Write a database backup right now")
    @mod_only()
    async def backup_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            path = await asyncio.get_running_loop().run_in_executor(None, run_backup, self.app.conn, self.app.cfg)
            await interaction.followup.send(f"Backup written: `{path.name}`", ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("backup-now failed")
            await interaction.followup.send(f"Backup failed: {exc}", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # predicate already replied
        log.exception("mod command error", exc_info=error)
        msg = self.app.copy.t("errors.generic")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModTools(bot))
