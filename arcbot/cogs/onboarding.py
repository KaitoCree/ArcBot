"""#apply: join handling, the three persistent buttons, Skip, pending reminders."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from ..db import iso, utcnow
from ..panels import ensure_panel

log = logging.getLogger("arcbot.onboarding")


class ApplyPanel(discord.ui.View):
    def __init__(self, cog: "Onboarding"):
        super().__init__(timeout=None)
        self.cog = cog
        c = cog.app.copy
        for cid, label, style, cb in (
            ("arcbot:apply:stats", c.t("apply.btn_show_stats"), discord.ButtonStyle.primary, self._stats),
            ("arcbot:apply:manual", c.t("apply.btn_manual"), discord.ButtonStyle.secondary, self._manual),
            ("arcbot:apply:skip", c.t("apply.btn_skip"), discord.ButtonStyle.secondary, self._skip),
        ):
            b = discord.ui.Button(label=label, style=style, custom_id=cid)
            b.callback = cb
            self.add_item(b)

    async def _stats(self, interaction: discord.Interaction) -> None:
        await self.cog.flow.open_upload(interaction, "onboarding")

    async def _manual(self, interaction: discord.Interaction) -> None:
        await self.cog.flow.open_manual(interaction, "onboarding")

    async def _skip(self, interaction: discord.Interaction) -> None:
        await self.cog.skip(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        from .intake_ui import _generic_error

        await _generic_error(interaction, error)


class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = bot.app  # type: ignore[attr-defined]
        self.flow = bot.intake_flow  # type: ignore[attr-defined]
        self.view = ApplyPanel(self)
        bot.add_view(self.view)

    async def cog_load(self) -> None:
        self.reminders.start()

    async def cog_unload(self) -> None:
        self.reminders.cancel()

    async def ensure_panels(self) -> None:
        if self.app.feature_missing("onboarding"):
            log.warning("onboarding disabled, missing: %s", self.app.feature_missing("onboarding"))
            return
        c = self.app.copy
        embed = discord.Embed(title=c.t("apply.prompt_title"), description=c.t("apply.prompt_body"),
                              color=discord.Color.dark_teal())
        await ensure_panel(self.app, "apply", "apply", embed, self.view)

    # ---------------------------------------------------------------- joins
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        app = self.app
        if member.bot or (app.guild_id and member.guild.id != app.guild_id):
            return
        user = app.engine.get_user(member.id)
        rank = user["rank_key"] if user else None
        await app.sync_rank_role(member, rank, "arcbot: rejoin restores rank" if rank else "arcbot: new member")
        log.info("join %s -> %s", member.id, rank or "Newcomer")
        minutes = float(app.cfg.raw["onboarding"].get("join_ping_minutes", 0) or 0)
        ch = app.channel("apply")
        if rank is None and minutes > 0 and ch is not None:
            # the mention badge shows a new member exactly where to start; it cleans itself up
            await app.gateway.send(ch, app.copy.t("apply.join_ping", mention=member.mention),
                                   allowed_mentions=discord.AllowedMentions(users=[member]),
                                   delete_after=minutes * 60)

    # ----------------------------------------------------------------- skip
    async def skip(self, interaction: discord.Interaction) -> None:
        app = self.app
        c = app.copy
        uid = interaction.user.id
        app.adopt_from_roles(interaction.user)
        user = app.engine.get_user(uid)
        if user and user["rank_key"]:
            await interaction.response.send_message(c.t("apply.already_placed"), ephemeral=True)
            return
        change = app.engine.place(uid, app.cfg.skip_rank)
        member = interaction.user if isinstance(interaction.user, discord.Member) else await app.member(uid)
        await interaction.response.send_message(c.t("apply.skip_done", rank=app.display_rank(app.cfg.skip_rank)),
                                                ephemeral=True)
        await app.apply_change(change, member, announce=False)
        log.info("skip %s -> %s", uid, app.cfg.skip_rank)

    # ------------------------------------------------------------ reminders
    @tasks.loop(minutes=30)
    async def reminders(self) -> None:
        app = self.app
        self.flow.sweep()
        ch = app.channel("mod_review")
        if ch is None:
            return
        for sub in app.intake.due_reminders():
            link = ""
            if sub["review_message_id"]:
                link = f" https://discord.com/channels/{ch.guild.id}/{sub['review_channel_id']}/{sub['review_message_id']}"
            mods = " ".join(r.mention for n in app.cfg.mod_roles if (r := app.resolved.roles.get(n)))
            await app.gateway.send(ch, f"{mods} Stats check #{sub['id']} is still waiting.{link}", mod_only=True,
                                   allowed_mentions=discord.AllowedMentions(roles=True))
            app.intake.mark_reminded(sub["id"])
        cutoff = iso(utcnow() - timedelta(hours=app.cfg.reminder_after_hours))
        for job in app.conn.execute(
                "SELECT id FROM jobs WHERE status='pending_review' AND created_at <= ?", (cutoff,)).fetchall():
            if app.alerts.should_send(f"job_reminder:{job['id']}"):
                await app.gateway.send(ch, f"Job #{job['id']} is still waiting for a look.", mod_only=True)

    @reminders.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
