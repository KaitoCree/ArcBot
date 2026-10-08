"""Pinned button panels (#apply, #rank-promotion, #job-board). One message per panel, edited in place."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from .db import get_setting, set_setting

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("arcbot.panels")


async def ensure_panel(app: "App", key: str, channel_key: str, embed: discord.Embed, view: discord.ui.View) -> None:
    channel = app.channel(channel_key)
    if channel is None:
        log.warning("panel %s skipped: channel %s missing", key, app.cfg.channels.get(channel_key))
        return
    setting = f"panel:{key}:message_id"
    mid = get_setting(app.conn, setting)
    if mid:
        try:
            msg = await channel.fetch_message(int(mid))
            await app.gateway.edit(msg, embed=embed, view=view)
            return
        except discord.NotFound:
            log.info("panel %s message gone, posting a new one", key)
        except discord.HTTPException as exc:
            log.warning("panel %s fetch failed: %s", key, exc)
            return
    msg = await app.gateway.send(channel, embed=embed, view=view)
    if msg is not None:
        set_setting(app.conn, setting, msg.id)
        await app.gateway.pin(msg)
