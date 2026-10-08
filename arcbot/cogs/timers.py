"""#event-timers: one pinned embed with active and upcoming ARC Raiders events.

MetaForge's documented endpoint GET /api/arc-raiders/events-schedule
(https://metaforge.app/arc-raiders/api). `region` is not documented; if a request with it fails we retry without it. Cached; the message is only edited when its content changes; failures are silent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import aiohttp
import discord
from discord.ext import commands, tasks

from ..db import get_setting, set_setting

log = logging.getLogger("arcbot.timers")

API = "https://metaforge.app/api/arc-raiders/events-schedule"
LINK = "https://metaforge.app/arc-raiders"
MAX_UPCOMING = 10
SETTING = "timers:message_id"


def build_embed(events: list[dict[str, Any]], now_ms: int, attribution: str) -> discord.Embed:
    active = sorted((e for e in events if e["startTime"] <= now_ms <= e["endTime"]), key=lambda e: e["endTime"])
    upcoming = sorted((e for e in events if e["startTime"] > now_ms), key=lambda e: e["startTime"])[:MAX_UPCOMING]
    embed = discord.Embed(title="🛰️ ARC Raiders: Event Timers", url=LINK, color=0x2ECC71)
    embed.add_field(name="🟢 Active Now", inline=False, value="\n".join(
        f"**{e['name']}** · {e['map']} · ends <t:{e['endTime'] // 1000}:R>" for e in active) or "_Nothing active right now._")
    embed.add_field(name="🕒 Upcoming", inline=False, value="\n".join(
        f"**{e['name']}** · {e['map']} · starts <t:{e['startTime'] // 1000}:t> (<t:{e['startTime'] // 1000}:R>)"
        for e in upcoming) or "_No upcoming events found._")
    embed.set_footer(text=f"{attribution} · {LINK.removeprefix('https://')}")
    return embed


def embed_fingerprint(embed: discord.Embed) -> str:
    return hashlib.sha1(json.dumps([(f.name, f.value) for f in embed.fields]).encode()).hexdigest()


def valid_events(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("unexpected payload shape")
    out = []
    for e in data:
        if isinstance(e, dict) and all(k in e for k in ("name", "map", "startTime", "endTime")):
            out.append({"name": str(e["name"]), "map": str(e["map"]), "startTime": int(e["startTime"]),
                        "endTime": int(e["endTime"])})
    return out


class Timers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = bot.app  # type: ignore[attr-defined]
        cfg = self.app.cfg.timers
        self.enabled = bool(cfg.get("enabled"))
        self.region = cfg.get("region")
        self.attribution = cfg.get("attribution_text", "Data: MetaForge")
        self.events: list[dict[str, Any]] = []
        self.fetched_at = 0.0
        self.last_print: str | None = None
        self.session: aiohttp.ClientSession | None = None
        self.poll.change_interval(minutes=max(1, int(cfg.get("poll_minutes", 5))))

    async def cog_load(self) -> None:
        if self.enabled:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": "arcbot/1.0 (discord event timer)"})
            self.poll.start()
        else:
            log.info("timers disabled in config")

    async def cog_unload(self) -> None:
        self.poll.cancel()
        if self.session:
            await self.session.close()

    async def fetch(self) -> list[dict[str, Any]] | None:
        assert self.session is not None
        attempts = [{"region": self.region}] if self.region else []
        attempts.append({})
        for params in attempts:
            try:
                async with self.session.get(API, params=params) as resp:
                    if resp.status != 200:
                        raise aiohttp.ClientResponseError(resp.request_info, (), status=resp.status)
                    return valid_events(await resp.json(content_type=None))
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                log.warning("MetaForge fetch failed (%s): %s", params or "no params", exc)
        return None

    @tasks.loop(minutes=5)
    async def poll(self) -> None:
        app = self.app
        ch = app.channel("event_timers")
        if ch is None:
            return
        events = await self.fetch()
        if events is not None:
            self.events, self.fetched_at = events, time.time()
        elif not self.events:
            return  # nothing cached yet: leave whatever is posted alone
        embed = build_embed(self.events, int(time.time() * 1000), self.attribution)
        fp = embed_fingerprint(embed)
        if fp == self.last_print:
            return
        mid = get_setting(app.conn, SETTING) or app.cfg.timers.get("adopt_message_id")
        if mid:
            try:
                await app.gateway.edit(ch.get_partial_message(int(mid)), embed=embed, mod_only=True)
                set_setting(app.conn, SETTING, mid)
                self.last_print = fp
                return
            except discord.NotFound:
                log.info("timer message %s gone; posting a new one", mid)
            except discord.Forbidden:
                log.warning("cannot edit timer message %s (posted by another bot account?); posting a new one", mid)
            except discord.HTTPException as exc:
                log.warning("timer edit failed: %s", exc)
                return
        msg = await app.gateway.send(ch, embed=embed, mod_only=True)
        if msg is not None:
            set_setting(app.conn, SETTING, msg.id)
            await app.gateway.pin(msg)
        self.last_print = fp

    @poll.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()

    @poll.error
    async def _err(self, error: BaseException) -> None:
        log.exception("timer loop crashed; restarting", exc_info=error)
        self.poll.restart()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Timers(bot))
