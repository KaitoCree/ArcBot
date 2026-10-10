"""Shared runtime state: config, DB, engine, resolved roles/channels, role sync and announcements."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field

import discord

from .config import Config
from .copytext import Copy
from .db import set_setting
from .discord_io import Alerts, Gateway
from .engine import RankChange, RankEngine
from .intake import IntakeService
from .naming import norm_channel
from .ranks import RankTable

log = logging.getLogger("arcbot.app")

# feature -> channel keys it needs (roles are checked separately)
FEATURE_CHANNELS = {
    "onboarding": ["apply", "mod_review"],
    "promotion": ["rank_promotion", "mod_review"],
    "vouch": ["vouch"],
    "jobs": ["job_board", "mod_review"],
    "timers": ["event_timers"],
}

# permissions the bot needs at guild level (docs/DISCORD_SETUP.md); pin/threads optional (job threads are private;
# Manage Threads lets the bot delete a finished job's thread)
REQUIRED_PERMS = ["view_channel", "send_messages", "send_messages_in_threads", "embed_links", "attach_files",
                  "read_message_history", "add_reactions", "manage_roles"]
OPTIONAL_PERMS = ["create_private_threads", "manage_threads", "manage_messages", "use_external_emojis"]


@dataclass
class Resolved:
    roles: dict[str, discord.Role] = field(default_factory=dict)  # by role name
    channels: dict[str, discord.TextChannel] = field(default_factory=dict)  # by config key
    missing_roles: list[str] = field(default_factory=list)
    missing_channels: list[str] = field(default_factory=list)


class App:
    def __init__(self, cfg: Config, copy: Copy, conn: sqlite3.Connection, *, dry_run: bool, guild_id: int | None):
        self.cfg = cfg
        self.copy = copy
        self.conn = conn
        self.dry_run = dry_run
        self.guild_id = guild_id
        self.engine = RankEngine(conn, cfg)
        self.ranks: RankTable = self.engine.ranks
        self.intake = IntakeService(conn, cfg, self.engine)
        self.alerts = Alerts()
        self.gateway = Gateway(dry_run, self.alerts)
        self.alerts.gateway = self.gateway
        self.resolved = Resolved()
        self.ocr_semaphore = asyncio.Semaphore(int(cfg.ocr.get("concurrency", 1)))
        self.guild: discord.Guild | None = None

    # ------------------------------------------------------------ resolving
    def resolve(self, guild: discord.Guild) -> Resolved:
        self.guild = guild
        r = Resolved()
        wanted_roles = [self.cfg.unplaced_role, *self.ranks.all_role_names(), *self.cfg.mod_roles]
        by_name = {role.name: role for role in guild.roles}
        for name in wanted_roles:
            if name in by_name:
                r.roles[name] = by_name[name]
            elif name not in self.cfg.mod_roles:
                r.missing_roles.append(name)
        if not any(m in by_name for m in self.cfg.mod_roles):
            r.missing_roles.append(" / ".join(self.cfg.mod_roles))
        text_by_name = {norm_channel(c.name): c for c in reversed(guild.text_channels)}
        for key, name in self.cfg.channels.items():
            ch = text_by_name.get(norm_channel(name))
            if ch is None:
                r.missing_channels.append(name)
            else:
                r.channels[key] = ch
                set_setting(self.conn, f"channel:{key}", ch.id)
        for name, role in r.roles.items():
            set_setting(self.conn, f"role:{name}", role.id)
        self.resolved = r
        self.alerts.channel = r.channels.get("mod_review")
        return r

    def channel(self, key: str) -> discord.TextChannel | None:
        return self.resolved.channels.get(key)

    def bot_channel(self, channel_id: int | None):
        """Any channel or thread by id (job threads live outside the config channel map)."""
        if self.guild is None or not channel_id:
            return None
        return self.guild.get_channel_or_thread(int(channel_id))

    def feature_missing(self, feature: str) -> list[str]:
        missing = [self.cfg.channels[k] for k in FEATURE_CHANNELS.get(feature, []) if k not in self.resolved.channels]
        if feature in ("onboarding", "promotion"):
            mods_label = " / ".join(self.cfg.mod_roles)
            missing += [n for n in self.resolved.missing_roles if n != mods_label]
        return missing

    def rank_role(self, key: str) -> discord.Role | None:
        return self.resolved.roles.get(self.ranks.role_name(key))

    def newcomer_role(self) -> discord.Role | None:
        return self.resolved.roles.get(self.cfg.unplaced_role)

    def display_rank(self, key: str) -> str:
        return self.ranks.role_name(key)

    # ------------------------------------------------------------ authority
    def is_mod(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild.owner_id == member.id:
            return True
        names = {r.name for r in member.roles}
        return any(m in names for m in self.cfg.mod_roles)

    def is_guild_master(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return member.guild.owner_id == member.id or any(r.name == self.cfg.guild_master_role for r in member.roles)

    # ------------------------------------------------------------ role sync
    async def sync_rank_role(self, member: discord.Member, rank_key: str | None, reason: str) -> bool:
        """Give exactly one rank role (or Newcomer when unplaced). Touch no other roles."""
        all_rank_roles = [r for k in self.ranks.keys if (r := self.rank_role(k)) is not None]
        newcomer = self.newcomer_role()
        if rank_key is None:
            add = [newcomer] if newcomer else []
            remove = all_rank_roles
        else:
            target = self.rank_role(rank_key)
            add = [target] if target else []
            remove = [r for r in all_rank_roles if r != target] + ([newcomer] if newcomer else [])
        return await self.gateway.set_roles(member, add, remove, reason)

    async def apply_change(self, change: RankChange | None, member: discord.Member | None, *,
                           announce: bool = True, name: str | None = None,
                           first_placement: bool | None = None) -> None:
        """Apply a RankChange to Discord roles and announce it (placement or rank-up, never with numbers)."""
        if change is None or member is None:
            return
        await self.sync_rank_role(member, change.new, f"arcbot: {change.old or 'unplaced'} -> {change.new}")
        if not announce or change.provisional:
            return
        channel = self.channel("rank_up_announcements")
        if channel is None:
            return
        rank = self.display_rank(change.new)
        first = change.first_placement if first_placement is None else first_placement
        if first:
            if not self.cfg.announce_placements:
                return
            text = self.copy.t("placement.placed", name=name or member.display_name, rank=rank)
        else:
            if not self.cfg.announce_rank_ups:
                return
            text = self.copy.t("placement.rank_up", name=member.display_name, rank=rank)
        await self.gateway.send(channel, text, user_texts=[name or "", member.display_name])

    def adopt_from_roles(self, member: discord.abc.User | None) -> bool:
        """A member who already holds a rank role but has no record (joined before arcbot, import not run,
        or ranked by hand) gets one on first contact, exactly like /admin import-existing: seeded to that
        rank's threshold, Veteran granted. No role changes, no announcement. Returns True if a record was made."""
        if not isinstance(member, discord.Member) or member.bot:
            return False
        u = self.engine.get_user(member.id)
        if u is not None and u["rank_key"]:
            return False
        held = [k for k in self.ranks.keys if (r := self.rank_role(k)) is not None and r in member.roles]
        if not held:
            return False
        top = max(held, key=self.ranks.order)
        self.engine.place(member.id, top, grant_veteran=top == "veteran", seed_source="import")
        log.info("adopted %s from existing role %s", member.id, top)
        return True

    async def member(self, user_id: int) -> discord.Member | None:
        if self.guild is None:
            return None
        m = self.guild.get_member(user_id)
        if m is not None:
            return m
        try:
            return await self.guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
