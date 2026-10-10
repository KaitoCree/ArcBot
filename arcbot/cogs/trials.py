"""#trials: one pinned embed with this week's ARC Raiders trials and next week's.

MetaForge's GET /api/arc-raiders/weekly-trials (used by metaforge.app/arc-raiders/weekly-trials; not in their API
docs, so it may change without warning). Their terms ask callers to cache: the list is fetched again only after the
weekly reset or every `refresh_hours`. The message is only edited when its content changes; failures keep the last
post as it is.
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

log = logging.getLogger("arcbot.trials")

API = "https://metaforge.app/api/arc-raiders/weekly-trials"
LINK = "https://metaforge.app/arc-raiders/weekly-trials"
SETTING = "trials:message_id"


def valid_trials(payload: Any) -> dict[str, Any]:
    """{'active': [...], 'upcoming': [...], 'ends': unix s | None, 'next': unix s | None}."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("unexpected payload shape")
    out: dict[str, Any] = {"active": [], "upcoming": [], "ends": None, "next": None}
    for t in data:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        bucket = "active" if t.get("is_active") else "upcoming" if t.get("upcoming") else None
        if bucket is None:
            continue
        out[bucket].append({
            "name": str(t["name"]),
            "guide": t.get("guide_link") if isinstance(t.get("guide_link"), str) else None,
            "map_ids": [int(m) for m in (t.get("map_ids") or []) if str(m).lstrip("-").isdigit()],
            "start": int(t["window_start"]) if t.get("window_start") else None,
            "end": int(t["window_end"]) if t.get("window_end") else None,
            "order": int(t.get("sort_order") or 0),
        })
    for bucket in ("active", "upcoming"):
        out[bucket].sort(key=lambda t: t["order"])
    ends = payload.get("activeWindowEnd") or max((t["end"] or 0 for t in out["active"]), default=0)
    nxt = payload.get("nextWindowStart") or min((t["start"] or 0 for t in out["upcoming"]), default=0)
    out["ends"], out["next"] = (int(ends) or None), (int(nxt) or None)
    return out


def _line(t: dict[str, Any], maps: dict[int, str]) -> str:
    name = f"[{t['name']}]({t['guide']})" if t["guide"] else f"**{t['name']}**"
    where = [maps[m] for m in t["map_ids"] if m in maps]
    return f"• {name}" + (f" · {', '.join(where)}" if where else "")


def build_embed(trials: dict[str, Any], maps: dict[int, str], attribution: str) -> discord.Embed:
    embed = discord.Embed(title="🎯 ARC Raiders: Weekly Trials", url=LINK, color=0xE67E22)
    ends = f" · resets <t:{trials['ends']}:R>" if trials["ends"] else ""
    embed.add_field(name=f"This week{ends}", inline=False, value="\n".join(
        _line(t, maps) for t in trials["active"])[:1024] or "_No trials listed right now._")
    if trials["upcoming"]:
        starts = f" · <t:{trials['next']}:f>" if trials["next"] else ""
        embed.add_field(name=f"Next week{starts}", inline=False,
                        value="\n".join(_line(t, maps) for t in trials["upcoming"])[:1024])
    embed.set_footer(text=f"{attribution} · {LINK.removeprefix('https://')}")
    return embed


def embed_fingerprint(embed: discord.Embed) -> str:
    return hashlib.sha1(json.dumps([(f.name, f.value) for f in embed.fields]).encode()).hexdigest()


def needs_fetch(trials: dict[str, Any] | None, fetched_at: float, now: float, refresh_hours: float) -> bool:
    """Fetch when nothing is cached, the week has rolled over, or the cache is older than refresh_hours."""
    if trials is None:
        return True
    if trials["ends"] and now >= trials["ends"]:
        return True
    return now - fetched_at >= refresh_hours * 3600


class Trials(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = bot.app  # type: ignore[attr-defined]
        cfg = self.app.cfg.trials
        self.enabled = bool(cfg.get("enabled"))
        self.refresh_hours = float(cfg.get("refresh_hours", 6))
        self.retry_minutes = float(cfg.get("retry_minutes", 15))
        self.maps = {int(k): str(v) for k, v in (cfg.get("map_names") or {}).items()}
        self.attribution = self.app.cfg.timers.get("attribution_text", "Data: MetaForge")
        self.trials: dict[str, Any] | None = None
        self.fetched_at = 0.0
        self.failed_at = 0.0
        self.last_print: str | None = None
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        if self.enabled:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": "arcbot/1.0 (discord weekly trials)"})
            self.poll.start()
        else:
            log.info("trials disabled in config")

    async def cog_unload(self) -> None:
        self.poll.cancel()
        if self.session:
            await self.session.close()

    async def fetch(self) -> dict[str, Any] | None:
        assert self.session is not None
        try:
            async with self.session.get(API) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(resp.request_info, (), status=resp.status)
                return valid_trials(await resp.json(content_type=None))
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning("MetaForge trials fetch failed: %s", exc)
            return None

    @tasks.loop(minutes=5)
    async def poll(self) -> None:
        app = self.app
        ch = app.channel("trials")
        if ch is None:
            return
        now = time.time()
        if needs_fetch(self.trials, self.fetched_at, now, self.refresh_hours) \
                and now - self.failed_at >= self.retry_minutes * 60:
            trials = await self.fetch()
            if trials is not None:
                self.trials, self.fetched_at = trials, now
            else:
                self.failed_at = now
        if self.trials is None:
            return  # nothing cached yet: leave whatever is posted alone
        embed = build_embed(self.trials, self.maps, self.attribution)
        fp = embed_fingerprint(embed)
        if fp == self.last_print:
            return
        mid = get_setting(app.conn, SETTING)
        if mid:
            try:
                await app.gateway.edit(ch.get_partial_message(int(mid)), embed=embed, mod_only=True)
                self.last_print = fp
                return
            except discord.NotFound:
                log.info("trials message %s gone; posting a new one", mid)
            except discord.HTTPException as exc:
                log.warning("trials edit failed: %s", exc)
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
        log.exception("trials loop crashed; restarting", exc_info=error)
        self.poll.restart()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trials(bot))
