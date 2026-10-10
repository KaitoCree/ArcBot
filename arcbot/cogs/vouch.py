"""#vouch: message vouches, /vouch, the "Vouch for this member" right-click, and a near-miss hint.

Every well-formed vouch gets the same reaction whether or not it counts, so the counting rules stay hidden.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..vouching import VouchService, count_words, is_near_miss, parse_vouch

log = logging.getLogger("arcbot.vouch")
HINT_EVERY_S = 24 * 3600
HINT_VISIBLE_S = 15


class VouchModal(discord.ui.Modal):
    def __init__(self, cog: "Vouch", member: discord.Member):
        c = cog.app.copy
        super().__init__(title=c.t("vouch.modal_title"), timeout=10 * 60)
        self.cog, self.member = cog, member
        self.reason = discord.ui.TextInput(label=c.t("vouch.reason_label"), style=discord.TextStyle.paragraph,
                                           placeholder=c.t("vouch.reason_placeholder")[:100], max_length=300)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.guided_vouch(interaction, self.member, self.reason.value)


class Vouch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = bot.app  # type: ignore[attr-defined]
        self.service = VouchService(self.app.conn, self.app.cfg, self.app.engine)
        self._hinted: dict[int, float] = {}
        self.ctx_menu = app_commands.ContextMenu(name=self.app.copy.t("vouch.context_menu")[:32],
                                                 callback=self._ctx_vouch)
        self.ctx_menu.guild_only = True
        bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # -------------------------------------------------------- shared record
    async def _record(self, message_id: int, voucher_id: int, recipients: list[int], text: str) -> None:
        for uid in [voucher_id, *recipients]:
            self.app.adopt_from_roles(await self.app.member(uid))
        voucher = await self.app.member(voucher_id)
        days = None
        joined = getattr(voucher, "joined_at", None)
        if joined is not None:
            days = (datetime.now(timezone.utc) - joined).total_seconds() / 86400
        for o in self.service.record(message_id, voucher_id, recipients, text, voucher_days_in_guild=days):
            log.info("vouch %s -> %s counted=%s reason=%s", voucher_id, o.recipient_id, o.counted, o.reason)
            if o.change:
                await self.app.apply_change(o.change, await self.app.member(o.recipient_id))
        jobs = self.bot.get_cog("Jobs")
        if jobs is not None:  # a poster vouching for whoever finished their job lets its thread close
            await jobs.on_vouched(voucher_id, recipients)  # type: ignore[attr-defined]

    # -------------------------------------------------------- message vouch
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        app = self.app
        if message.author.bot or message.guild is None:
            return
        ch = app.channel("vouch")
        if ch is None or message.channel.id != ch.id:
            return
        mentioned = [(u.id, u.bot) for u in message.mentions]
        parsed = parse_vouch(message.content, mentioned, message.author.id, app.cfg)
        if not parsed.well_formed:
            await self._maybe_hint(message, mentioned)
            return
        if app.cfg.vouch_rules.get("always_react_when_well_formed", True):
            await app.gateway.react(message, app.copy.t("vouch.reaction"))
        await self._record(message.id, message.author.id, parsed.recipients, message.content)

    async def _maybe_hint(self, message: discord.Message, mentioned: list[tuple[int, bool]]) -> None:
        app = self.app
        if not app.cfg.vouch_rules.get("near_miss_hint", True):
            return
        if not is_near_miss(message.content, mentioned, message.author.id, app.cfg):
            return
        now = time.monotonic()
        last = self._hinted.get(message.author.id)
        if last is not None and now - last < HINT_EVERY_S:
            return
        self._hinted[message.author.id] = now
        await app.gateway.send(message.channel, app.copy.t("vouch.hint"), reference=message,
                               delete_after=HINT_VISIBLE_S)

    # -------------------------------------------------------- guided vouch
    @app_commands.command(name="vouch", description="Vouch for a raider who helped you out")
    @app_commands.guild_only()
    @app_commands.describe(member="Who helped you?", what="What did they do?")
    async def vouch(self, interaction: discord.Interaction, member: discord.Member, what: str) -> None:
        await self.guided_vouch(interaction, member, what)

    async def _ctx_vouch(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._precheck(interaction, member):
            return
        await interaction.response.send_modal(VouchModal(self, member))

    async def _precheck(self, interaction: discord.Interaction, member: discord.abc.User) -> bool:
        c = self.app.copy
        if member.id == interaction.user.id:
            await interaction.response.send_message(c.t("vouch.not_self"), ephemeral=True)
            return False
        if member.bot:
            await interaction.response.send_message(c.t("vouch.not_bot"), ephemeral=True)
            return False
        return True

    async def guided_vouch(self, interaction: discord.Interaction, member: discord.Member, what: str) -> None:
        app = self.app
        c = app.copy
        if not await self._precheck(interaction, member):
            return
        what = " ".join(what.split())[:300]
        min_words = int(app.cfg.vouch_rules["min_words"])
        if count_words(what) < min_words:
            await interaction.response.send_message(c.t("vouch.need_words", min_words=min_words), ephemeral=True)
            return
        ch = app.channel("vouch")
        if ch is None:
            await interaction.response.send_message(c.t("errors.generic"), ephemeral=True)
            return
        voucher = interaction.user
        text = c.t("vouch.posted", voucher=voucher.mention, member=member.mention, reason=what)
        msg = await app.gateway.send(ch, text, user_texts=[what])
        await interaction.response.send_message(c.t("vouch.done"), ephemeral=True)
        if msg is not None:
            await app.gateway.react(msg, c.t("vouch.reaction"))
        # dry run has no message; the interaction id is just as unique
        await self._record(msg.id if msg is not None else interaction.id, voucher.id, [member.id], what)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Vouch(bot))
