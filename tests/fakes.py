"""Minimal fakes of the discord.py objects the cogs touch, plus a recording Gateway.

Enough to drive real cog/flow code end to end without a Discord connection.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from arcbot.discord_io import Gateway, _embed_text

_ids = itertools.count(10_000)


class FakeMessage:
    def __init__(self, channel=None, content: str = "", author=None, mentions=None, guild=None):
        self.id = next(_ids)
        self.channel = channel
        self.content = content
        self.author = author
        self.mentions = mentions or []
        self.guild = guild

    async def pin(self):
        return None


class FakeChannel:
    def __init__(self, name: str, guild=None):
        self.id = next(_ids)
        self.name = name
        self.guild = guild

    def get_partial_message(self, mid: int) -> FakeMessage:
        m = FakeMessage(self)
        m.id = mid
        return m

    async def fetch_message(self, mid: int) -> FakeMessage:
        return self.get_partial_message(mid)


class FakeMember:
    def __init__(self, uid: int, name: str, *, days_in_guild: float = 30, bot: bool = False):
        self.id = uid
        self.display_name = name
        self.name = name
        self.mention = f"<@{uid}>"
        self.roles: list[Any] = []
        self.bot = bot
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=days_in_guild)
        self.guild = None


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.channels: dict[int, FakeChannel] = {}

    def get_channel_or_thread(self, cid: int):
        return self.channels.get(cid)

    def get_member(self, uid: int):
        return None


class RecordingGateway(Gateway):
    """Records everything instead of calling Discord; keeps the point-leak guard on player-facing output."""

    def __init__(self):
        super().__init__(dry_run=False)
        self.player_visible: list[str] = []
        self.mod_visible: list[str] = []
        self.role_changes: list[tuple[int, list[str], list[str]]] = []
        self.reactions: list[tuple[int, str]] = []

    def _rec(self, mod_only: bool, content: str | None, embed: discord.Embed | None, user_texts=()):
        text = (content or "") + "\n" + _embed_text(embed)
        if not mod_only:
            self._guard(content, [embed] if embed else [], user_texts)
            for ut in user_texts:
                if ut:
                    text = text.replace(ut, "")
        (self.mod_visible if mod_only else self.player_visible).append(text)

    async def send(self, target, content=None, *, embed=None, view=None, files=None, allowed_mentions=None,
                   mod_only=False, user_texts=(), reference=None, delete_after=None):
        self._rec(mod_only, content, embed, user_texts)
        return FakeMessage(target)

    async def edit(self, message, *, content=None, embed=None, view=..., mod_only=False, user_texts=(),
                   attachments=None):
        self._rec(mod_only, content, embed, user_texts)

    async def dm(self, user, content):
        self._rec(False, content, None)
        return True

    async def react(self, message, emoji):
        self.reactions.append((message.id, emoji))

    async def pin(self, message):
        return True

    async def create_thread(self, message, name):
        return None

    async def set_roles(self, member, add, remove, reason):
        self.role_changes.append((member.id, [r.name for r in add], [r.name for r in remove]))
        return True


class _Response:
    def __init__(self, sink: list[str]):
        self.sink = sink
        self._done = False
        self.modals: list[discord.ui.Modal] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, file=None,
                           allowed_mentions=None):
        self._done = True
        self.sink.append((content or "") + "\n" + _embed_text(embed))

    async def send_modal(self, modal):
        self._done = True
        self.modals.append(modal)

    async def defer(self, *, ephemeral=False, thinking=False):
        self._done = True

    async def edit_message(self, *, content=None, view=None, embed=None):
        self._done = True
        self.sink.append((content or "") + "\n" + _embed_text(embed))


class _Followup:
    def __init__(self, sink: list[str]):
        self.sink = sink

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False, file=None, allowed_mentions=None):
        self.sink.append((content or "") + "\n" + _embed_text(embed))


class FakeInteraction:
    def __init__(self, client, user: FakeMember, *, message: FakeMessage | None = None):
        self.client = client
        self.user = user
        self.replies: list[str] = []
        self.response = _Response(self.replies)
        self.followup = _Followup(self.replies)
        self.message = message
        self.guild_id = 1
        self.guild = None

    async def edit_original_response(self, *, content=None, view=None, embed=None):
        self.replies.append((content or "") + "\n" + _embed_text(embed))
