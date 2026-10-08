"""#job-board: Post a job modal, listing checks, mod routing, accept/complete/confirm, flags, expiry."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from ..config import ROOT
from ..intake import delete_image
from ..jobs import JobService
from ..panels import ensure_panel

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("arcbot.jobs")
JOB_UPLOADS = ROOT / "data" / "uploads"
ALLOWED_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def _app(interaction: discord.Interaction) -> "App":
    return interaction.client.app  # type: ignore[attr-defined]


def _svc(interaction: discord.Interaction) -> JobService:
    return interaction.client.get_cog("Jobs").service  # type: ignore[union-attr]


def _image_path(job_id: int) -> Path | None:
    for p in JOB_UPLOADS.glob(f"job-{job_id}.*"):
        return p
    return None


def job_embed(app: "App", job, *, poster_name: str | None = None, helper_name: str | None = None) -> discord.Embed:
    c = app.copy
    tier = app.cfg.job_tiers[job["tier"]]
    e = discord.Embed(title=job["title"], description=job["description"], color=discord.Color.blurple())
    e.add_field(name=c.t("jobs.posted_by"), value=f"<@{job['poster_id']}>", inline=True)
    e.add_field(name=c.t("jobs.open_to"), value=tier.label, inline=True)
    helper = f"<@{job['helper_id']}>" if job["helper_id"] else ""
    other = ""
    if job["status"] == "awaiting_confirm":
        other_id = job["helper_id"] if job["completion_requested_by"] == job["poster_id"] else job["poster_id"]
        other = f"<@{other_id}>"
    status = {
        "open": c.t("jobs.status_open"),
        "accepted": c.t("jobs.status_taken", helper=helper),
        "awaiting_confirm": c.t("jobs.status_awaiting", other=other),
        "needs_mod": c.t("jobs.status_needs_mod"),
        "completed": c.t("jobs.status_done"),
        "cancelled": c.t("jobs.status_cancelled"),
        "expired": c.t("jobs.status_expired"),
        "removed": c.t("jobs.status_removed"),
        "closed": c.t("jobs.status_closed"),
    }.get(job["status"], job["status"])
    e.add_field(name=c.t("jobs.status"), value=status, inline=True)
    e.set_footer(text=f"Job #{job['id']}")
    return e


def job_view(app: "App", job) -> discord.ui.View | None:
    c = app.copy
    buttons = {
        "open": [("accept", c.t("jobs.btn_accept"), discord.ButtonStyle.success),
                 ("cancel", c.t("jobs.btn_cancel"), discord.ButtonStyle.secondary),
                 ("flag", c.t("jobs.btn_flag"), discord.ButtonStyle.secondary)],
        "accepted": [("complete", c.t("jobs.btn_complete"), discord.ButtonStyle.success),
                     ("cancel", c.t("jobs.btn_cancel"), discord.ButtonStyle.secondary),
                     ("flag", c.t("jobs.btn_flag"), discord.ButtonStyle.secondary)],
        "awaiting_confirm": [("confirm", c.t("jobs.btn_confirm"), discord.ButtonStyle.success),
                             ("flag", c.t("jobs.btn_flag"), discord.ButtonStyle.secondary)],
    }.get(job["status"])
    if not buttons:
        return None
    v = discord.ui.View(timeout=None)
    for action, label, style in buttons:
        v.add_item(JobButton(action, job["id"], label=label, style=style))
    return v


def review_embed(app: "App", job, *, decided: str | None = None, heading: str = "needs a look") -> discord.Embed:
    tier = app.cfg.job_tiers[job["tier"]]
    e = discord.Embed(title=f"Job #{job['id']} {heading}: {job['title']}", description=job["description"],
                      color=discord.Color.orange() if decided is None else discord.Color.dark_grey())
    e.add_field(name="Poster", value=f"<@{job['poster_id']}>", inline=True)
    e.add_field(name="Tier", value=f"{tier.label} (hidden reward {tier.points})", inline=True)  # mod-only
    reasons = json.loads(job["flag_reasons"] or "[]")
    e.add_field(name="Why it's here", value="\n".join(f"• {r}" for r in reasons) or "flagged by a member",
                inline=False)
    if decided:
        e.add_field(name="Decision", value=decided, inline=False)
    return e


def review_view(job_id: int, kind: str = "hold") -> discord.ui.View:
    """hold: Approve / Reject / Change tier. notice: Take down. stalled: Award helper / Close."""
    actions = {"hold": ("approve", "reject", "tier"), "notice": ("remove",), "stalled": ("award", "close")}[kind]
    v = discord.ui.View(timeout=None)
    for a in actions:
        v.add_item(JobReviewButton(a, job_id))
    return v


# ===================================================================== modal
class PostJobModal(discord.ui.Modal):
    def __init__(self, app: "App"):
        c = app.copy
        super().__init__(title=c.t("jobs.modal_title"), timeout=15 * 60)
        self.app = app
        mx = int(app.cfg.job_checks["max_description_chars"])
        self.title_in = discord.ui.TextInput(custom_id="title", max_length=100, min_length=4)
        self.desc_in = discord.ui.TextInput(custom_id="desc", style=discord.TextStyle.paragraph, max_length=min(mx, 4000))
        self.tier_in = discord.ui.Select(custom_id="tier", options=[
            discord.SelectOption(label=t.label, value=k) for k, t in app.cfg.job_tiers.items()])
        self.image_in = discord.ui.FileUpload(custom_id="image", required=False, min_values=0, max_values=1)
        self.add_item(discord.ui.Label(text=c.t("jobs.title_label"), component=self.title_in))
        self.add_item(discord.ui.Label(text=c.t("jobs.description_label"), component=self.desc_in))
        self.add_item(discord.ui.Label(text=c.t("jobs.tier_label"), component=self.tier_in))
        self.add_item(discord.ui.Label(text=c.t("jobs.image_label"), component=self.image_in))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: Jobs = interaction.client.get_cog("Jobs")  # type: ignore[assignment]
        await cog.submit(interaction, self.title_in.value.strip(), self.desc_in.value.strip(), self.tier_in.values[0],
                         list(self.image_in.values))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _error(interaction, error)


# ===================================================================== buttons
class BoardPanel(discord.ui.View):
    def __init__(self, cog: "Jobs"):
        super().__init__(timeout=None)
        self.cog = cog
        b = discord.ui.Button(label=cog.app.copy.t("jobs.post_button"), style=discord.ButtonStyle.primary,
                              custom_id="arcbot:jobs:post")
        b.callback = self._post
        self.add_item(b)

    async def _post(self, interaction: discord.Interaction) -> None:
        app = self.cog.app
        app.adopt_from_roles(interaction.user)
        if not app.engine.is_placed(interaction.user.id):
            await interaction.response.send_message(app.copy.t("jobs.not_placed"), ephemeral=True)
            return
        await interaction.response.send_modal(PostJobModal(app))


class JobButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"arcbot:job:(?P<action>accept|cancel|complete|confirm|flag):(?P<id>\d+)"):
    def __init__(self, action: str, job_id: int, *, label: str = "…", style=discord.ButtonStyle.secondary):
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"arcbot:job:{action}:{job_id}"))
        self.action, self.job_id = action, job_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["id"]), label=item.label, style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Jobs = interaction.client.get_cog("Jobs")  # type: ignore[assignment]
        await getattr(cog, f"on_{self.action}")(interaction, self.job_id)


class JobReviewButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"arcbot:jrev:(?P<action>approve|reject|tier|remove|award|close):(?P<id>\d+)"):
    LABELS = {"approve": ("Approve & post", discord.ButtonStyle.success), "reject": ("Reject", discord.ButtonStyle.danger),
              "tier": ("Change tier", discord.ButtonStyle.primary), "remove": ("Take down", discord.ButtonStyle.danger),
              "award": ("It was done: award helper", discord.ButtonStyle.success),
              "close": ("Close without award", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, job_id: int):
        label, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"arcbot:jrev:{action}:{job_id}"))
        self.action, self.job_id = action, job_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app = _app(interaction)
        if not app.is_mod(interaction.user):
            await interaction.response.send_message(app.copy.t("errors.not_allowed"), ephemeral=True)
            return
        cog: Jobs = interaction.client.get_cog("Jobs")  # type: ignore[assignment]
        await getattr(cog, f"review_{self.action}")(interaction, self.job_id)


class TierSelectView(discord.ui.View):
    def __init__(self, cog: "Jobs", job_id: int, review_message: discord.Message | None):
        super().__init__(timeout=5 * 60)
        self.cog, self.job_id, self.review_message = cog, job_id, review_message
        sel = discord.ui.Select(placeholder="New tier", options=[
            discord.SelectOption(label=t.label, value=k) for k, t in cog.app.cfg.job_tiers.items()])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction) -> None:
        job = self.cog.service.change_tier(self.job_id, self.sel.values[0])
        if job is None:
            await interaction.response.edit_message(content="That job isn't waiting any more.", view=None)
            return
        await interaction.response.edit_message(content=f"Tier set to {self.cog.app.cfg.job_tiers[job['tier']].label}.",
                                                view=None)
        if self.review_message is not None:
            await self.cog.app.gateway.edit(self.review_message, embed=review_embed(self.cog.app, job),
                                            view=review_view(self.job_id), mod_only=True)


async def _error(interaction: discord.Interaction, error: Exception) -> None:
    log.exception("job UI error", exc_info=error)
    app = _app(interaction)
    await app.alerts.alert("jobs_error", f"Job board error: {type(error).__name__}. Check the logs.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(app.copy.t("errors.generic"), ephemeral=True)
        else:
            await interaction.response.send_message(app.copy.t("errors.generic"), ephemeral=True)
    except discord.HTTPException:
        pass


# ======================================================================= cog
class Jobs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app: App = bot.app  # type: ignore[attr-defined]
        self.service = JobService(self.app.conn, self.app.cfg, self.app.engine)
        self.panel = BoardPanel(self)
        bot.add_view(self.panel)
        JOB_UPLOADS.mkdir(parents=True, exist_ok=True)

    async def cog_load(self) -> None:
        self.expiry.start()

    async def cog_unload(self) -> None:
        self.expiry.cancel()

    async def ensure_panels(self) -> None:
        if self.app.feature_missing("jobs"):
            log.warning("job board disabled, missing: %s", self.app.feature_missing("jobs"))
            return
        c = self.app.copy
        embed = discord.Embed(title=c.t("jobs.board_title"), description=c.t("jobs.board_body"),
                              color=discord.Color.blurple())
        await ensure_panel(self.app, "jobs", "job_board", embed, self.panel)

    # ----------------------------------------------------------- posting
    async def submit(self, interaction: discord.Interaction, title: str, desc: str, tier: str,
                     images: list[discord.Attachment]) -> None:
        app = self.app
        c = app.copy
        await interaction.response.defer(ephemeral=True, thinking=True)
        image = images[0] if images else None
        if image and ((image.content_type or "").split(";")[0] not in ALLOWED_TYPES
                      or image.size > int(app.cfg.ocr.get("max_image_mb", 8)) * 1024 * 1024):
            image = None
        job_id, status, reasons = self.service.create(interaction.user.id, title, desc, tier, has_image=image is not None)
        if image is not None:
            ext = ALLOWED_TYPES[(image.content_type or "").split(";")[0]]
            (JOB_UPLOADS / f"job-{job_id}.{ext}").write_bytes(await image.read())
        log.info("job %s by %s tier=%s status=%s reasons=%s", job_id, interaction.user.id, tier, status, reasons)
        if status == "open":
            await self.publish(job_id)
            if reasons:  # soft flag-list words: posted, mods get a heads-up with a Take down button
                await self.send_to_review(job_id, kind="notice", heading="posted, heads-up")
            await interaction.followup.send(c.t("jobs.posted"), ephemeral=True)
        else:
            await self.send_to_review(job_id)
            await interaction.followup.send(c.t("jobs.held_for_mods"), ephemeral=True)

    async def publish(self, job_id: int) -> None:
        app = self.app
        job = self.service.get(job_id)
        ch = app.channel("job_board")
        if job is None or ch is None:
            return
        files, embed = [], job_embed(app, job)
        img = _image_path(job_id)
        if img is not None:
            files.append(discord.File(str(img), filename=f"job{img.suffix}"))
            embed.set_image(url=f"attachment://job{img.suffix}")
        msg = await app.gateway.send(ch, embed=embed, view=job_view(app, job), files=files,
                                     user_texts=[job["title"], job["description"]])
        delete_image(str(img) if img else None)
        if msg is not None:
            self.service.set_posted(job_id, ch.id, msg.id)

    async def send_to_review(self, job_id: int, *, kind: str = "hold", heading: str = "needs a look") -> None:
        app = self.app
        job = self.service.get(job_id)
        ch = app.channel("mod_review")
        if job is None or ch is None:
            return
        files, embed = [], review_embed(app, job, heading=heading)
        img = _image_path(job_id) if kind == "hold" else None
        if img is not None:
            files.append(discord.File(str(img), filename=f"job{img.suffix}"))
            embed.set_image(url=f"attachment://job{img.suffix}")
        if job["message_id"]:
            embed.add_field(name="Job post", inline=False,
                            value=f"https://discord.com/channels/{ch.guild.id}/{job['channel_id']}/{job['message_id']}")
        msg = await app.gateway.send(ch, embed=embed, view=review_view(job_id, kind), files=files, mod_only=True)
        if msg is not None:
            self.service.set_review(job_id, ch.id, msg.id)

    async def _refresh(self, job) -> None:
        app = self.app
        if not job["message_id"] or not job["channel_id"]:
            return
        ch = app.bot_channel(job["channel_id"])
        if ch is None:
            return
        try:
            await app.gateway.edit(ch.get_partial_message(job["message_id"]), embed=job_embed(app, job),
                                   view=job_view(app, job), user_texts=[job["title"], job["description"]])
        except discord.HTTPException as exc:
            log.warning("could not refresh job %s: %s", job["id"], exc)

    # ------------------------------------------------------------ review
    async def review_approve(self, interaction: discord.Interaction, job_id: int) -> None:
        await interaction.response.defer()
        job = self.service.approve(job_id)
        if job is None:
            await interaction.followup.send("That job isn't waiting any more.", ephemeral=True)
            return
        await self.publish(job_id)
        await self._close_review(interaction, job, f"Approved by {interaction.user.mention}")
        await self._tell_poster(job["poster_id"], self.app.copy.t("jobs.posted"))

    async def review_reject(self, interaction: discord.Interaction, job_id: int) -> None:
        await interaction.response.defer()
        job = self.service.reject(job_id)
        if job is None:
            await interaction.followup.send("That job isn't waiting any more.", ephemeral=True)
            return
        img = _image_path(job_id)
        delete_image(str(img) if img else None)
        await self._close_review(interaction, job, f"Rejected by {interaction.user.mention}")
        await self._tell_poster(job["poster_id"], self.app.copy.t("jobs.rejected_generic"))

    async def review_remove(self, interaction: discord.Interaction, job_id: int) -> None:
        await interaction.response.defer()
        job = self.service.remove(job_id)
        if job is None:
            await interaction.followup.send("That job is already closed.", ephemeral=True)
            return
        await self._refresh(job)
        await self._close_review(interaction, job, f"Taken down by {interaction.user.mention}")
        await self._tell_poster(job["poster_id"], self.app.copy.t("jobs.rejected_generic"))

    async def review_award(self, interaction: discord.Interaction, job_id: int) -> None:
        await self._resolve(interaction, job_id, award=True)

    async def review_close(self, interaction: discord.Interaction, job_id: int) -> None:
        await self._resolve(interaction, job_id, award=False)

    async def _resolve(self, interaction: discord.Interaction, job_id: int, *, award: bool) -> None:
        app = self.app
        await interaction.response.defer()
        job, result = self.service.mod_resolve(job_id, award=award)
        if job is None:
            await interaction.followup.send("That job is already wrapped up.", ephemeral=True)
            return
        log.info("job %s resolved by mod %s: award=%s", job_id, interaction.user.id, award)
        await self._refresh(job)
        label = "Helper awarded" if award else "Closed without award"
        await self._close_review(interaction, job, f"{label} by {interaction.user.mention}")
        for uid in (job["poster_id"], job["helper_id"]):
            await self._tell_poster(uid, app.copy.t("jobs.mod_resolved"))
        if result is not None and result.change is not None:
            await app.apply_change(result.change, await app.member(job["helper_id"]))

    async def review_tier(self, interaction: discord.Interaction, job_id: int) -> None:
        await interaction.response.send_message("Pick the tier:", ephemeral=True,
                                                view=TierSelectView(self, job_id, interaction.message))

    async def _close_review(self, interaction: discord.Interaction, job, decided: str) -> None:
        if interaction.message is not None:
            await self.app.gateway.edit(interaction.message, embed=review_embed(self.app, job, decided=decided),
                                        view=None, mod_only=True, attachments=[])

    async def _tell_poster(self, user_id: int, text: str) -> None:
        member = await self.app.member(user_id)
        if member is not None:
            await self.app.gateway.dm(member, text)

    # ------------------------------------------------------------ job buttons
    async def on_accept(self, interaction: discord.Interaction, job_id: int) -> None:
        app, c = self.app, self.app.copy
        uid = interaction.user.id
        app.adopt_from_roles(interaction.user)
        user = app.engine.get_user(uid)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        days = None
        if member is not None and member.joined_at is not None:
            days = (datetime.now(timezone.utc) - member.joined_at).total_seconds() / 86400
        job, reason = self.service.accept(job_id, uid, helper_rank=user["rank_key"] if user else None,
                                          days_in_guild=days)
        if reason:
            msg = {
                "taken": c.t("jobs.taken"),
                "own": c.t("jobs.cannot_accept_own"),
                "not_placed": c.t("jobs.not_placed"),
                "too_new": c.t("jobs.too_new"),
                "rank_too_low": c.t("jobs.ineligible", who=app.cfg.job_tiers[job["tier"]].who if job else ""),
            }[reason]
            await interaction.response.send_message(msg, ephemeral=True)
            return
        poster = await app.member(job["poster_id"])
        await interaction.response.send_message(
            c.t("jobs.accepted", poster=poster.display_name if poster else "the poster"), ephemeral=True)
        await self._refresh(job)
        if app.cfg.job_rules.get("create_thread_per_job") and interaction.message is not None:
            thread = await app.gateway.create_thread(interaction.message, job["title"])
            if thread is not None:
                self.service.set_thread(job_id, thread.id)
                await app.gateway.send(
                    thread, c.t("jobs.thread_intro", poster=f"<@{job['poster_id']}>", helper=f"<@{uid}>"),
                    allowed_mentions=discord.AllowedMentions(users=True))

    async def on_cancel(self, interaction: discord.Interaction, job_id: int) -> None:
        job = self.service.cancel(job_id, interaction.user.id)
        if job is None:
            await interaction.response.send_message(self.app.copy.t("jobs.not_yours"), ephemeral=True)
            return
        await interaction.response.send_message(self.app.copy.t("jobs.cancelled_note"), ephemeral=True)
        await self._refresh(job)

    async def on_complete(self, interaction: discord.Interaction, job_id: int) -> None:
        c = self.app.copy
        job = self.service.mark_complete(job_id, interaction.user.id)
        if job is None:
            await interaction.response.send_message(c.t("jobs.not_involved"), ephemeral=True)
            return
        await interaction.response.send_message(c.t("jobs.marked_complete"), ephemeral=True)
        await self._refresh(job)
        requester = interaction.user.id
        other = job["helper_id"] if requester == job["poster_id"] else job["poster_id"]
        target = self.app.bot_channel(job["thread_id"]) if job["thread_id"] else self.app.channel("job_board")
        if target is not None:
            await self.app.gateway.send(
                target, c.t("jobs.awaiting_confirm", other=f"<@{other}>", requester=f"<@{requester}>"),
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(other)]))

    async def on_confirm(self, interaction: discord.Interaction, job_id: int) -> None:
        app, c = self.app, self.app.copy
        job, award = self.service.confirm(job_id, interaction.user.id)
        if award is None:
            waiting = job is not None and job["status"] == "awaiting_confirm"
            if waiting and interaction.user.id == job["completion_requested_by"]:
                msg = c.t("jobs.only_other")
            elif waiting:
                msg = c.t("jobs.not_involved")
            else:
                msg = c.t("jobs.taken")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        log.info("job %s confirmed by %s: helper awarded %s (cap %s)", job_id, interaction.user.id, award.points,
                 award.capped)
        await interaction.response.send_message(c.t("jobs.completed"), ephemeral=True)
        await self._refresh(job)
        if award.change is not None:
            await app.apply_change(award.change, await app.member(job["helper_id"]))

    async def on_flag(self, interaction: discord.Interaction, job_id: int) -> None:
        app, c = self.app, self.app.copy
        if not self.service.flag(job_id, interaction.user.id):
            await interaction.response.send_message(c.t("jobs.not_yours"), ephemeral=True)
            return
        await interaction.response.send_message(c.t("jobs.flagged"), ephemeral=True)
        ch = app.channel("mod_review")
        job = self.service.get(job_id)
        if ch is not None and job is not None:
            link = ""
            if job["message_id"]:
                link = f" https://discord.com/channels/{ch.guild.id}/{job['channel_id']}/{job['message_id']}"
            await app.gateway.send(ch, f"🚩 Job #{job_id} \"{job['title']}\" was flagged by {interaction.user.mention}."
                                       f"{link}", mod_only=True)

    # ------------------------------------------------------------ nudges / expiry
    @tasks.loop(hours=1)
    async def expiry(self) -> None:
        await self.housekeeping()

    async def housekeeping(self) -> None:
        app, c = self.app, self.app.copy
        for job in self.service.due_checkins():
            target = app.bot_channel(job["thread_id"]) if job["thread_id"] else app.channel("job_board")
            if target is not None:
                ids = [job["poster_id"], job["helper_id"]]
                await app.gateway.send(
                    target, c.t("jobs.checkin", poster=f"<@{ids[0]}>", helper=f"<@{ids[1]}>"),
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(i) for i in ids]))
        for job in self.service.due_escalations():
            log.info("job %s confirmation stalled; sent to mods", job["id"])
            await self._refresh(job)
            await self.send_to_review(job["id"], kind="stalled", heading="was marked done but never confirmed")
        for job in self.service.expire_due():
            log.info("job %s expired", job["id"])
            await self._refresh(job)

    @expiry.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Jobs(bot))
