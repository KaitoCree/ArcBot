"""#rank-promotion: pinned Update my stats button (same intake as onboarding, 30-day cooldown)."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..panels import ensure_panel

log = logging.getLogger("arcbot.promotion")


class PromotionPanel(discord.ui.View):
    def __init__(self, cog: "Promotion"):
        super().__init__(timeout=None)
        self.cog = cog
        c = cog.app.copy
        up = discord.ui.Button(label=c.t("promotion.btn_update"), style=discord.ButtonStyle.primary,
                               custom_id="arcbot:promo:stats")
        up.callback = self._stats
        manual = discord.ui.Button(label=c.t("apply.btn_manual"), style=discord.ButtonStyle.secondary,
                                   custom_id="arcbot:promo:manual")
        manual.callback = self._manual
        self.add_item(up)
        self.add_item(manual)

    async def _stats(self, interaction: discord.Interaction) -> None:
        await self.cog.flow.open_upload(interaction, "promotion")

    async def _manual(self, interaction: discord.Interaction) -> None:
        await self.cog.flow.open_manual(interaction, "promotion")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        from .intake_ui import _generic_error

        await _generic_error(interaction, error)


class Promotion(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = bot.app  # type: ignore[attr-defined]
        self.flow = bot.intake_flow  # type: ignore[attr-defined]
        self.view = PromotionPanel(self)
        bot.add_view(self.view)

    async def ensure_panels(self) -> None:
        if self.app.feature_missing("promotion"):
            log.warning("promotion disabled, missing: %s", self.app.feature_missing("promotion"))
            return
        c = self.app.copy
        embed = discord.Embed(title=c.t("promotion.prompt_title"),
                              description=c.t("promotion.prompt_body", days=self.app.cfg.promotion_cooldown_days),
                              color=discord.Color.gold())
        await ensure_panel(self.app, "promotion", "rank_promotion", embed, self.view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Promotion(bot))
