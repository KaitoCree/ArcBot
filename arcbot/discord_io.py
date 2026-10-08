"""Every Discord mutation goes through Gateway so ARCBOT_DRY_RUN=1 and the point-leak guard apply everywhere.

Ephemeral interaction replies are not routed here: they mutate nothing on the server and must still work
in dry-run so flows can be walked through.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

import discord

log = logging.getLogger("arcbot.io")

# "5 points", "points: 5", "12 pts". Player-facing bot text must never contain these.
_LEAK = re.compile(r"\d[\d,]*\s*(?:points?|pts)\b|\b(?:points?|pts)\s*[:=]\s*\d", re.IGNORECASE)

NO_MENTIONS = discord.AllowedMentions.none()


class PointLeakError(RuntimeError):
    pass


def leaks_points(text: str | None, user_texts: Iterable[str] = ()) -> bool:
    if not text:
        return False
    for ut in user_texts:
        if ut:
            text = text.replace(ut, "")
    return bool(_LEAK.search(text))


def _embed_text(embed: discord.Embed | None) -> str:
    if embed is None:
        return ""
    parts = [embed.title or "", embed.description or "", (embed.footer.text if embed.footer else None) or ""]
    for f in embed.fields:
        parts += [f.name or "", f.value or ""]
    return "\n".join(parts)


class Gateway:
    def __init__(self, dry_run: bool, alerts: "Alerts | None" = None):
        self.dry_run = dry_run
        self.alerts = alerts

    def _guard(self, content: str | None, embeds: list[discord.Embed], user_texts: Iterable[str]) -> None:
        ut = list(user_texts)
        if leaks_points(content, ut) or any(leaks_points(_embed_text(e), ut) for e in embeds):
            log.error("blocked a player-facing message that looked like it contained points: %r", content)
            raise PointLeakError("player-facing message contained a point value")

    async def send(
        self,
        target: discord.abc.Messageable,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        files: list[discord.File] | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
        mod_only: bool = False,
        user_texts: Iterable[str] = (),
        reference: discord.Message | None = None,
        delete_after: float | None = None,
    ) -> discord.Message | None:
        if not mod_only:
            self._guard(content, [embed] if embed else [], user_texts)
        if self.dry_run:
            log.info("[dry-run] send to %s: %s%s", getattr(target, "id", target), content or "",
                     f" [embed: {embed.title}]" if embed else "")
            return None
        kwargs: dict[str, Any] = {"allowed_mentions": allowed_mentions or NO_MENTIONS}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if files:
            kwargs["files"] = files
        if reference is not None:
            kwargs["reference"] = reference
            kwargs["mention_author"] = False
        if delete_after is not None:
            kwargs["delete_after"] = delete_after
        return await target.send(**kwargs)

    async def edit(
        self,
        message: discord.Message | discord.PartialMessage,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None | type[Ellipsis] = ...,
        mod_only: bool = False,
        user_texts: Iterable[str] = (),
        attachments: list[discord.File] | None = None,
    ) -> None:
        if not mod_only:
            self._guard(content, [embed] if embed else [], user_texts)
        if self.dry_run:
            log.info("[dry-run] edit message %s", message.id)
            return
        kwargs: dict[str, Any] = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not ...:
            kwargs["view"] = view
        if attachments is not None:
            kwargs["attachments"] = attachments
        await message.edit(**kwargs)

    async def pin(self, message: discord.Message) -> bool:
        if self.dry_run:
            log.info("[dry-run] pin message %s", message.id)
            return True
        try:
            await message.pin()
            return True
        except discord.HTTPException as exc:
            log.warning("could not pin message %s: %s", message.id, exc)
            return False

    async def react(self, message: discord.Message, emoji: str) -> None:
        if self.dry_run:
            log.info("[dry-run] react %s on %s", emoji, message.id)
            return
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as exc:
            log.warning("could not react on %s: %s", message.id, exc)

    async def dm(self, user: discord.abc.User, content: str) -> bool:
        self._guard(content, [], ())
        if self.dry_run:
            log.info("[dry-run] DM %s: %s", user.id, content)
            return True
        try:
            await user.send(content, allowed_mentions=NO_MENTIONS)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def create_thread(self, message: discord.Message, name: str) -> discord.Thread | None:
        if self.dry_run:
            log.info("[dry-run] thread '%s' on %s", name, message.id)
            return None
        try:
            return await message.create_thread(name=name[:100], auto_archive_duration=4320)
        except discord.HTTPException as exc:
            log.warning("could not create thread: %s", exc)
            return None

    async def set_roles(
        self,
        member: discord.Member,
        add: list[discord.Role],
        remove: list[discord.Role],
        reason: str,
    ) -> bool:
        add = [r for r in add if r not in member.roles]
        remove = [r for r in remove if r in member.roles]
        if not add and not remove:
            return True
        if self.dry_run:
            log.info("[dry-run] roles for %s: +%s -%s (%s)", member.id, [r.name for r in add],
                     [r.name for r in remove], reason)
            return True
        try:
            if remove:
                await member.remove_roles(*remove, reason=reason)
            if add:
                await member.add_roles(*add, reason=reason)
            log.info("roles for %s: +%s -%s (%s)", member.id, [r.name for r in add], [r.name for r in remove], reason)
            return True
        except discord.Forbidden:
            log.error("Forbidden changing roles for %s (role hierarchy?)", member.id)
            if self.alerts:
                await self.alerts.alert(
                    "role_forbidden",
                    "I couldn't change someone's rank role (Discord said Forbidden). Drag the arcbot role above "
                    "all rank roles and Newcomer, then run /admin setup-check.",
                )
            return False
        except discord.HTTPException as exc:
            log.error("role change failed for %s: %s", member.id, exc)
            return False


class Alerts:
    """Short, rate-limited alerts to #mod-review. Never raises."""

    def __init__(self, gateway: Gateway | None = None, min_interval_s: float = 3600):
        self.gateway = gateway
        self.channel: discord.abc.Messageable | None = None
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}

    def should_send(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._last.get(key)
        if last is not None and now - last < self.min_interval_s:
            return False
        self._last[key] = now
        return True

    async def alert(self, key: str, text: str) -> None:
        if not self.should_send(key):
            return
        log.warning("mod alert [%s]: %s", key, text)
        if self.channel is None or self.gateway is None:
            return
        try:
            await self.gateway.send(self.channel, f"⚠️ {text}", mod_only=True)
        except Exception:  # noqa: BLE001
            log.exception("could not post mod alert")
