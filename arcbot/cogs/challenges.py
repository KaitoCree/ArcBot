"""Challenge jobs on #job-board: Form squad, invites, Submit my extract, Guild Master review, Hall of Clears.

Rules live in arcbot/challenges.py; the challenge post itself is a job row rendered by the Jobs cog.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..challenges import ChallengeService
from ..intake import delete_image
from .jobs import ALLOWED_TYPES, JOB_UPLOADS, _error, _mentions

if TYPE_CHECKING:
    from ..app import App
    from .jobs import Jobs

log = logging.getLogger("arcbot.challenges")


def _cog(interaction: discord.Interaction) -> "Challenges":
    return interaction.client.get_cog("Challenges")  # type: ignore[return-value]


def _days(member) -> float | None:
    joined = getattr(member, "joined_at", None)
    return (datetime.now(timezone.utc) - joined).total_seconds() / 86400 if joined else None


class ChallengeButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"arcbot:chal:(?P<action>form|close):(?P<id>\d+)"):
    """On the challenge post."""

    def __init__(self, action: str, job_id: int, *, label: str = "…", style=discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"arcbot:chal:{action}:{job_id}"))
        self.action, self.job_id = action, job_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["id"]), label=item.label, style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        await getattr(_cog(interaction), f"on_{self.action}")(interaction, self.job_id)


class SquadButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"arcbot:csq:(?P<action>accept|decline|invite|submit|disband):(?P<id>\d+)"):
    """In a squad's private thread."""

    def __init__(self, action: str, squad_id: int, *, label: str = "…", style=discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"arcbot:csq:{action}:{squad_id}"))
        self.action, self.squad_id = action, squad_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["id"]), label=item.label, style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        await getattr(_cog(interaction), f"squad_{self.action}")(interaction, self.squad_id)


class ChallengeReviewButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"arcbot:crev:(?P<action>approve|reject):(?P<id>\d+)"):
    """In #mod-review. Guild Master only (they may approve their own squad, by design)."""

    LABELS = {"approve": ("Approve clear", discord.ButtonStyle.success), "reject": ("Reject", discord.ButtonStyle.danger)}

    def __init__(self, action: str, squad_id: int):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"arcbot:crev:{action}:{squad_id}"))
        self.action, self.squad_id = action, squad_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if not cog.app.is_guild_master(interaction.user):
            await interaction.response.send_message(cog.app.copy.t("errors.not_allowed"), ephemeral=True)
            return
        await cog.decide(interaction, self.squad_id, approve=self.action == "approve")


def squad_controls(app: "App", squad_id: int) -> discord.ui.View:
    c = app.copy
    v = discord.ui.View(timeout=None)
    for action, key, style in (("submit", "challenge.btn_submit", discord.ButtonStyle.success),
                               ("invite", "challenge.btn_invite", discord.ButtonStyle.primary),
                               ("disband", "challenge.btn_disband", discord.ButtonStyle.secondary)):
        v.add_item(SquadButton(action, squad_id, label=c.t(key), style=style))
    return v


def invite_buttons(app: "App", squad_id: int) -> discord.ui.View:
    c = app.copy
    v = discord.ui.View(timeout=None)
    v.add_item(SquadButton("accept", squad_id, label=c.t("challenge.btn_accept"), style=discord.ButtonStyle.success))
    v.add_item(SquadButton("decline", squad_id, label=c.t("challenge.btn_decline"),
                           style=discord.ButtonStyle.secondary))
    return v


def review_view(squad_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ChallengeReviewButton("approve", squad_id))
    v.add_item(ChallengeReviewButton("reject", squad_id))
    return v


class InvitePicker(discord.ui.View):
    def __init__(self, cog: "Challenges", squad_id: int, room: int):
        super().__init__(timeout=10 * 60)
        self.cog, self.squad_id = cog, squad_id
        self.pick = discord.ui.UserSelect(placeholder=cog.app.copy.t("challenge.invite_pick"), min_values=1,
                                          max_values=max(1, room))
        self.pick.callback = self._picked
        self.add_item(self.pick)

    async def _picked(self, interaction: discord.Interaction) -> None:
        await self.cog.send_invites(interaction, self.squad_id, list(self.pick.values))


class ProofModal(discord.ui.Modal):
    def __init__(self, cog: "Challenges", squad_id: int):
        c = cog.app.copy
        super().__init__(title=c.t("challenge.proof_title"), timeout=15 * 60)
        self.cog, self.squad_id = cog, squad_id
        self.image_in = discord.ui.FileUpload(custom_id="proof", required=True, min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text=c.t("challenge.proof_label"), component=self.image_in))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_proof(interaction, self.squad_id, list(self.image_in.values))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _error(interaction, error)


class Challenges(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app: App = bot.app  # type: ignore[attr-defined]
        self.service = ChallengeService(self.app.conn, self.app.cfg, self.app.engine)

    @property
    def jobs(self) -> "Jobs":
        return self.bot.get_cog("Jobs")  # type: ignore[return-value]

    def _who(self, member) -> tuple[str | None, float | None]:
        self.app.adopt_from_roles(member)
        row = self.app.engine.get_user(member.id)
        return (row["rank_key"] if row else None), _days(member)

    async def _refresh_post(self, job_id: int) -> None:
        job = self.jobs.service.get(job_id)
        if job is not None:
            await self.jobs._refresh(job)

    async def _thread(self, squad) -> discord.Thread | None:
        if not squad["thread_id"]:
            return None
        thread = self.app.bot_channel(squad["thread_id"])
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(squad["thread_id"])
            except discord.DiscordException:
                return None
        return thread  # type: ignore[return-value]

    async def _say(self, squad, text: str, ping: list[int], view: discord.ui.View | None = None) -> None:
        thread = await self._thread(squad)
        if thread is not None:
            await self.app.gateway.send(thread, text, view=view, allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(u) for u in ping]))

    # ------------------------------------------------------------ challenge post
    async def on_form(self, interaction: discord.Interaction, job_id: int) -> None:
        app, c = self.app, self.app.copy
        rank, days = self._who(interaction.user)
        squad, reason = self.service.form(job_id, interaction.user.id, rank=rank, days_in_guild=days)
        if reason:
            job = self.service.job(job_id)
            msg = {"closed": c.t("challenge.closed"), "not_placed": c.t("jobs.not_placed"),
                   "too_new": c.t("jobs.too_new"), "in_squad": c.t("challenge.in_squad"),
                   "rank_too_low": c.t("jobs.ineligible", who=app.cfg.job_tiers[job["tier"]].who if job else "")}[reason]
            await interaction.response.send_message(msg, ephemeral=True)
            return
        log.info("challenge %s: squad %s formed by %s", job_id, squad["id"], interaction.user.id)
        await interaction.response.send_message(c.t("challenge.formed"), ephemeral=True)
        job = self.service.job(job_id)
        ch = app.bot_channel(job["channel_id"]) or app.channel("job_board")
        if ch is not None:
            name = getattr(interaction.user, "display_name", str(interaction.user.id))
            thread = await app.gateway.create_private_thread(
                ch, f"{job['title'][:60]} · {c.t('challenge.thread_name', leader=name)}")
            if thread is not None:
                self.service.set_thread(squad["id"], thread.id)
                await app.gateway.add_to_thread(thread, interaction.user.id)
                await app.gateway.send(
                    thread, c.t("challenge.thread_intro", leader=interaction.user.mention, title=job["title"],
                                room=app.cfg.challenge_max_squad - 1),
                    view=squad_controls(app, squad["id"]), user_texts=[job["title"]],
                    allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        await self._refresh_post(job_id)

    async def on_close(self, interaction: discord.Interaction, job_id: int) -> None:
        c = self.app.copy
        job = self.service.close(job_id, interaction.user.id)
        if job is None:
            await interaction.response.send_message(c.t("challenge.close_only_poster"), ephemeral=True)
            return
        log.info("challenge %s closed by %s", job_id, interaction.user.id)
        await interaction.response.send_message(c.t("challenge.closed_note"), ephemeral=True)
        await self._refresh_post(job_id)

    # ------------------------------------------------------------ squad thread
    async def squad_invite(self, interaction: discord.Interaction, squad_id: int) -> None:
        c, cfg = self.app.copy, self.app.cfg
        squad = self.service.squad(squad_id)
        if squad is None or squad["leader_id"] != interaction.user.id:
            await interaction.response.send_message(c.t("challenge.only_leader"), ephemeral=True)
            return
        if squad["status"] != "forming":
            await interaction.response.send_message(c.t("challenge.locked"), ephemeral=True)
            return
        room = cfg.challenge_max_squad - len(self.service.members(squad_id, ("invited", "accepted")))
        if room <= 0:
            await interaction.response.send_message(c.t("challenge.skip_full"), ephemeral=True)
            return
        await interaction.response.send_message(c.t("challenge.invite_prompt", room=room),
                                                view=InvitePicker(self, squad_id, room), ephemeral=True)

    async def send_invites(self, interaction: discord.Interaction, squad_id: int, users: list) -> None:
        app, c = self.app, self.app.copy
        info = {u.id: self._who(u) for u in users if not getattr(u, "bot", False)}
        invited, skipped, block = self.service.invite(squad_id, interaction.user.id, info)
        if block:
            key = "challenge.locked" if block == "locked" else "challenge.only_leader"
            await interaction.response.edit_message(content=c.t(key), view=None)
            return
        why = {"own": "challenge.skip_own", "in_squad": "challenge.skip_in_squad", "full": "challenge.skip_full",
               "rank_too_low": "challenge.skip_rank", "too_new": "challenge.skip_new",
               "not_placed": "challenge.skip_unplaced", "closed": "challenge.closed"}
        lines = [c.t("challenge.invite_sent", invitees=_mentions(invited))] if invited else []
        lines += [c.t("challenge.invite_skipped", who=f"<@{u}>", why=c.t(why[r])) for u, r in skipped.items()]
        await interaction.response.edit_message(content="\n".join(lines) or c.t("challenge.skip_full"), view=None)
        if not invited:
            return
        log.info("squad %s: %s invited %s", squad_id, interaction.user.id, invited)
        squad = self.service.squad(squad_id)
        thread = await self._thread(squad)
        if thread is not None:
            for uid in invited:
                await app.gateway.add_to_thread(thread, uid)
        await self._say(squad, c.t("challenge.invited", invitees=_mentions(invited), leader=f"<@{squad['leader_id']}>"),
                        invited, view=invite_buttons(app, squad_id))

    async def squad_accept(self, interaction: discord.Interaction, squad_id: int) -> None:
        await self._respond(interaction, squad_id, accept=True)

    async def squad_decline(self, interaction: discord.Interaction, squad_id: int) -> None:
        await self._respond(interaction, squad_id, accept=False)

    async def _respond(self, interaction: discord.Interaction, squad_id: int, *, accept: bool) -> None:
        c = self.app.copy
        reason = self.service.respond(squad_id, interaction.user.id, accept)
        if reason:
            msg = {"locked": "challenge.locked", "not_invited": "challenge.not_invited",
                   "leader": "challenge.leader_stays", "in_squad": "challenge.in_squad"}[reason]
            await interaction.response.send_message(c.t(msg), ephemeral=True)
            return
        await interaction.response.send_message(c.t("challenge.accept_done" if accept else "challenge.decline_done"),
                                                ephemeral=True)
        squad = self.service.squad(squad_id)
        await self._say(squad, c.t("challenge.joined" if accept else "challenge.left", who=interaction.user.mention), [])

    async def squad_disband(self, interaction: discord.Interaction, squad_id: int) -> None:
        c = self.app.copy
        squad, paths = self.service.disband(squad_id, interaction.user.id)
        if squad is None:
            current = self.service.squad(squad_id)
            leader = current is not None and current["leader_id"] == interaction.user.id
            await interaction.response.send_message(c.t("challenge.locked" if leader else "challenge.only_leader"),
                                                    ephemeral=True)
            return
        for p in paths:
            delete_image(p)
        await interaction.response.send_message(c.t("challenge.disband_done"), ephemeral=True)
        await self._say(squad, c.t("challenge.disbanded", leader=interaction.user.mention), [])
        await self._close_thread(squad)
        await self._refresh_post(squad["job_id"])

    async def squad_submit(self, interaction: discord.Interaction, squad_id: int) -> None:
        c = self.app.copy
        squad = self.service.squad(squad_id)
        if squad is None or interaction.user.id not in self.service.members(squad_id):
            await interaction.response.send_message(c.t("challenge.not_member"), ephemeral=True)
            return
        if squad["status"] not in ("forming", "submitting"):
            await interaction.response.send_message(c.t("challenge.locked"), ephemeral=True)
            return
        await interaction.response.send_modal(ProofModal(self, squad_id))

    async def save_proof(self, interaction: discord.Interaction, squad_id: int,
                         images: list[discord.Attachment]) -> None:
        app, c = self.app, self.app.copy
        image = images[0] if images else None
        kind = (image.content_type or "").split(";")[0] if image else ""
        if image is None or kind not in ALLOWED_TYPES or image.size > int(app.cfg.ocr.get("max_image_mb", 8)) * 2**20:
            await interaction.response.send_message(c.t("challenge.proof_bad"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await image.read()
        path = JOB_UPLOADS / f"challenge-{squad_id}-{interaction.user.id}.{ALLOWED_TYPES[kind]}"
        path.write_bytes(data)
        squad, replaced, block = self.service.submit_proof(squad_id, interaction.user.id, str(path),
                                                           hashlib.sha256(data).hexdigest())
        if block:
            delete_image(str(path))
            await interaction.followup.send(c.t("challenge.locked" if block == "locked" else "challenge.not_member"),
                                            ephemeral=True)
            return
        if replaced:
            delete_image(replaced)
        log.info("squad %s: proof from %s (status %s)", squad_id, interaction.user.id, squad["status"])
        if squad["status"] != "in_review":
            have = {p["user_id"] for p in self.service.proofs(squad_id)}
            waiting = [u for u in self.service.members(squad_id) if u not in have]
            await interaction.followup.send(c.t("challenge.proof_saved", waiting=_mentions(waiting)), ephemeral=True)
            await self._say(squad, c.t("challenge.proof_saved", waiting=_mentions(waiting)), waiting)
            return
        await interaction.followup.send(c.t("challenge.proof_all_in"), ephemeral=True)
        await self._say(squad, c.t("challenge.proof_all_in"), [])
        await self.send_for_review(squad_id)

    # ------------------------------------------------------------ review
    async def send_for_review(self, squad_id: int) -> None:
        app = self.app
        squad = self.service.squad(squad_id)
        job = self.service.job(squad["job_id"]) or app.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (squad["job_id"],)).fetchone()
        ch = app.channel("mod_review")
        if ch is None:
            return
        members = self.service.members(squad_id)
        first = not any(place == 1 for place, _ in self.service.hall(job["id"]))
        flags = self.service.flags(squad_id)
        lines = [f"🏆 **Challenge clear to review**: {job['title']} (job #{job['id']}, squad #{squad_id})",
                 f"Squad: {_mentions(members)}",
                 f"Reward each: {self.service.reward_points(job, first)} points"  # mod-only
                 + (" (first clear bonus included)" if first else "")]
        for p in self.service.proofs(squad_id):
            ts = int(datetime.fromisoformat(p["submitted_at"]).timestamp())
            lines.append(f"• <@{p['user_id']}> screenshot <t:{ts}:T>")
        lines.append("⚠️ " + "\n⚠️ ".join(flags) if flags else "No automatic flags.")
        files = []
        for p in self.service.proofs(squad_id):
            if p["image_path"]:
                try:
                    files.append(discord.File(p["image_path"], filename=f"extract-{p['user_id']}"
                                              + p["image_path"][p["image_path"].rfind("."):]))
                except OSError:
                    lines.append(f"(screenshot from <@{p['user_id']}> is missing on disk)")
        msg = await app.gateway.send(ch, "\n".join(lines), view=review_view(squad_id), files=files, mod_only=True)
        if msg is not None:
            self.service.set_review(squad_id, ch.id, msg.id)

    async def decide(self, interaction: discord.Interaction, squad_id: int, *, approve: bool) -> None:
        app, c = self.app, self.app.copy
        await interaction.response.defer()
        squad, awards, paths = self.service.decide(squad_id, approve, interaction.user.id)
        if squad is None:
            await interaction.followup.send("That clear was already decided.", ephemeral=True)
            return
        for p in paths:
            delete_image(p)
        members = self.service.members(squad_id)
        given = [(a.user_id, a.points, a.already_rewarded) for a in awards]  # mod-only (log)
        log.info("squad %s %s by %s; awards %s", squad_id, squad["status"], interaction.user.id, given)
        if interaction.message is not None:
            verdict = (f"✅ Approved by {interaction.user.mention} (clear #{squad['place']})" if approve
                       else f"❌ Rejected by {interaction.user.mention}")
            await app.gateway.edit(interaction.message, content=f"{interaction.message.content}\n{verdict}",
                                   view=None, mod_only=True, attachments=[])
        if approve:
            key = "challenge.approved_first" if squad["place"] == 1 else "challenge.approved"
            await self._say(squad, c.t(key, squad=_mentions(members)), members)
            for a in awards:
                if a.change is not None:
                    await app.apply_change(a.change, await app.member(a.user_id))
        else:
            await self._say(squad, c.t("challenge.rejected", squad=_mentions(members)), members)
        await self._close_thread(squad)
        await self._refresh_post(squad["job_id"])

    async def _close_thread(self, squad) -> None:
        thread = await self._thread(squad)
        if thread is not None:  # archived, not deleted: the squad keeps its history
            await self.app.gateway.close_thread(thread, delete=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Challenges(bot))
