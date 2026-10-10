"""#job-board: Post a job modal, listing checks, mod routing, attempt/complete/confirm, private threads, flags,
expiry."""
from __future__ import annotations

import asyncio
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
from .vouch import VouchModal

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


def _mentions(ids) -> str:
    return " ".join(f"<@{i}>" for i in ids)


def job_embed(app: "App", job, attempters: list[int] | tuple = ()) -> discord.Embed:
    c = app.copy
    tier = app.cfg.job_tiers[job["tier"]]
    special = float(job["xp_multiplier"] or 1) > 1
    e = discord.Embed(title=job["title"], description=job["description"],
                      color=discord.Color.gold() if special else discord.Color.blurple())
    e.add_field(name=c.t("jobs.posted_by"), value=f"<@{job['poster_id']}>", inline=True)
    e.add_field(name=c.t("jobs.open_to"), value=tier.label, inline=True)
    helper = f"<@{job['helper_id']}>" if job["helper_id"] else ""
    status = {
        "open": c.t("jobs.status_open"),
        "accepted": c.t("jobs.status_attempting"),
        "awaiting_confirm": c.t("jobs.status_awaiting"),
        "needs_mod": c.t("jobs.status_needs_mod"),
        "completed": c.t("jobs.status_done", helper=helper),
        "cancelled": c.t("jobs.status_cancelled"),
        "expired": c.t("jobs.status_expired"),
        "removed": c.t("jobs.status_removed"),
        "closed": c.t("jobs.status_closed"),
    }.get(job["status"], job["status"])
    e.add_field(name=c.t("jobs.status"), value=status, inline=True)
    if special:
        e.add_field(name=c.t("jobs.special"), value=c.t("jobs.special_value"), inline=True)
    if attempters and job["status"] in JobService.JOINABLE:
        e.add_field(name=c.t("jobs.attempting"), value=_mentions(attempters)[:1024], inline=False)
    e.set_footer(text=f"Job #{job['id']}")
    return e


def job_view(app: "App", job) -> discord.ui.View | None:
    c = app.copy
    attempt = ("accept", c.t("jobs.btn_accept"), discord.ButtonStyle.primary)
    cancel = ("cancel", c.t("jobs.btn_cancel"), discord.ButtonStyle.secondary)
    flag = ("flag", c.t("jobs.btn_flag"), discord.ButtonStyle.secondary)
    buttons = {
        "open": [attempt, cancel, flag],
        "accepted": [attempt, ("complete", c.t("jobs.btn_complete"), discord.ButtonStyle.success), cancel, flag],
        # pending completion: the poster reviews who finished, in order; others can still finish and mark it too
        "awaiting_confirm": [("review", c.t("jobs.btn_review"), discord.ButtonStyle.success),
                             ("complete", c.t("jobs.btn_complete"), discord.ButtonStyle.success),
                             attempt, cancel, flag],
    }.get(job["status"])
    if not buttons:
        return None
    return _buttons(job["id"], buttons)


def _buttons(job_id: int, buttons, timeout: float | None = None) -> discord.ui.View:
    v = discord.ui.View(timeout=timeout)
    for action, label, style in buttons:
        v.add_item(JobButton(action, job_id, label=label, style=style))
    return v


def review_embed(app: "App", job, *, decided: str | None = None, heading: str = "needs a look") -> discord.Embed:
    tier = app.cfg.job_tiers[job["tier"]]
    e = discord.Embed(title=f"Job #{job['id']} {heading}: {job['title']}", description=job["description"],
                      color=discord.Color.orange() if decided is None else discord.Color.dark_grey())
    e.add_field(name="Poster", value=f"<@{job['poster_id']}>", inline=True)
    boost = float(job["xp_multiplier"] or 1)
    boost_txt = f" x{boost:g} Guild Master boost" if boost > 1 else ""
    e.add_field(name="Tier", value=f"{tier.label} (hidden reward {tier.points}{boost_txt})", inline=True)  # mod-only
    reasons = json.loads(job["flag_reasons"] or "[]")
    e.add_field(name="Why it's here", value="\n".join(f"• {r}" for r in reasons) or "flagged by a member",
                inline=False)
    if job["status"] in ("awaiting_confirm", "needs_mod") or decided:
        rows = JobService(app.conn, app.cfg, app.engine).completions(job["id"])
        if rows:
            e.add_field(name="Marked complete (fastest first)", inline=False, value="\n".join(
                f"{n}. <@{r['user_id']}>" + (" (turned down by poster)" if r["rejected_at"] else "")
                for n, r in enumerate(rows, 1))[:1024])
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
    def __init__(self, app: "App", *, can_boost: bool = False):
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
        # Guild Master only: everyone else never gets this field, so it can't be picked
        self.boost_in: discord.ui.Select | None = None
        if can_boost and len(app.cfg.job_xp_boosts) > 1:
            self.boost_in = discord.ui.Select(custom_id="boost", required=False, min_values=0, max_values=1, options=[
                discord.SelectOption(label=b.label[:100], value=str(i), default=i == 0)
                for i, b in enumerate(app.cfg.job_xp_boosts)])
            self.add_item(discord.ui.Label(text=c.t("jobs.boost_label"), component=self.boost_in))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: Jobs = interaction.client.get_cog("Jobs")  # type: ignore[assignment]
        boost = int(self.boost_in.values[0]) if self.boost_in is not None and self.boost_in.values else 0
        await cog.submit(interaction, self.title_in.value.strip(), self.desc_in.value.strip(), self.tier_in.values[0],
                         list(self.image_in.values), boost=boost)

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
        await interaction.response.send_modal(PostJobModal(app, can_boost=app.is_guild_master(interaction.user)))


class JobButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"arcbot:job:(?P<action>accept|join|cancel|complete|review|confirm|notdone|vouch|flag):(?P<id>\d+)"):
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


def completion_order_text(app: "App", rows) -> str:
    """The poster's view of who marked the job complete, fastest first."""
    c = app.copy
    lines = []
    for n, r in enumerate(rows, 1):
        ts = int(datetime.fromisoformat(r["claimed_at"]).timestamp())
        line = c.t("jobs.order_line", n=_ordinal(n), who=f"<@{r['user_id']}>", when=f"<t:{ts}:R>")
        lines.append(f"~~{line}~~ {c.t('jobs.order_turned_down')}" if r["rejected_at"] else line)
    return c.t("jobs.order_heading") + "\n" + "\n".join(lines)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class CompletionReview(discord.ui.View):
    """Ephemeral, poster only: pick who really completed it from the completion order, or turn one down."""

    def __init__(self, cog: "Jobs", job_id: int, pending: list[tuple[int, str]]):
        super().__init__(timeout=10 * 60)
        c = cog.app.copy
        self.cog, self.job_id = cog, job_id
        order = {uid: n for n, (uid, _) in enumerate(pending)}
        self.pick = discord.ui.Select(placeholder=c.t("jobs.review_pick"), options=[
            discord.SelectOption(label=f"{name}"[:100], value=str(uid), default=order[uid] == 0)
            for uid, name in pending])
        self.pick.callback = self._noop
        self.add_item(self.pick)
        ok = discord.ui.Button(label=c.t("jobs.btn_confirm"), style=discord.ButtonStyle.success)
        ok.callback = self._confirm
        no = discord.ui.Button(label=c.t("jobs.btn_notdone"), style=discord.ButtonStyle.danger)
        no.callback = self._reject
        self.add_item(ok)
        self.add_item(no)
        self.default = pending[0][0] if pending else None

    def chosen(self) -> int | None:
        return int(self.pick.values[0]) if self.pick.values else self.default

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.confirm_completion(interaction, self.job_id, self.chosen())

    async def _reject(self, interaction: discord.Interaction) -> None:
        await self.cog.reject_completion(interaction, self.job_id, self.chosen())


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
        self._thread_locks: dict[int, asyncio.Lock] = {}
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
                     images: list[discord.Attachment], *, boost: int = 0) -> None:
        app = self.app
        c = app.copy
        await interaction.response.defer(ephemeral=True, thinking=True)
        multiplier = 1.0
        if boost and 0 <= boost < len(app.cfg.job_xp_boosts) and app.is_guild_master(interaction.user):
            multiplier = app.cfg.job_xp_boosts[boost].multiplier
        image = images[0] if images else None
        if image and ((image.content_type or "").split(";")[0] not in ALLOWED_TYPES
                      or image.size > int(app.cfg.ocr.get("max_image_mb", 8)) * 1024 * 1024):
            image = None
        job_id, status, reasons = self.service.create(interaction.user.id, title, desc, tier, has_image=image is not None,
                                                      xp_multiplier=multiplier)
        if image is not None:
            ext = ALLOWED_TYPES[(image.content_type or "").split(";")[0]]
            (JOB_UPLOADS / f"job-{job_id}.{ext}").write_bytes(await image.read())
        log.info("job %s by %s tier=%s boost=x%g status=%s reasons=%s", job_id, interaction.user.id, tier, multiplier,
                 status, reasons)
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
            await app.gateway.edit(ch.get_partial_message(job["message_id"]),
                                   embed=job_embed(app, job, self.service.attempters(job["id"])),
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
        await self.close_threads()

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
        if award:
            await self._after_completion(job)
        await self.close_threads()

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

    # ------------------------------------------------------------ threads
    async def _thread(self, job) -> discord.Thread | None:
        if not job["thread_id"]:
            return None
        thread = self.app.bot_channel(job["thread_id"])
        if thread is None:
            try:  # archived threads are not cached
                thread = await self.bot.fetch_channel(job["thread_id"])
            except discord.NotFound:
                return None
            except discord.DiscordException as exc:
                log.warning("could not fetch thread %s: %s", job["thread_id"], exc)
                return None
        return thread  # type: ignore[return-value]

    async def _say(self, job, text: str, ping: list[int], view: discord.ui.View | None = None) -> None:
        """Post in the job's private thread, or on the board if there is none."""
        target = await self._thread(job) or self.app.channel("job_board")
        if target is not None:
            await self.app.gateway.send(target, text, view=view,
                                        allowed_mentions=discord.AllowedMentions(
                                            users=[discord.Object(i) for i in ping if i]))

    async def _join_thread(self, job, uid: int) -> None:
        """Create the private thread on the first attempt; add every later attempter to it."""
        app, c = self.app, self.app.copy
        if not app.cfg.job_rules.get("create_thread_per_job"):
            return
        lock = self._thread_locks.setdefault(job["id"], asyncio.Lock())
        async with lock:
            job = self.service.get(job["id"])
            thread = await self._thread(job)
            if thread is not None:
                await app.gateway.add_to_thread(thread, uid)
                await app.gateway.send(thread, c.t("jobs.thread_joined", helper=f"<@{uid}>"))
                return
            ch = app.bot_channel(job["channel_id"]) or app.channel("job_board")
            if ch is None:
                return
            thread = await app.gateway.create_private_thread(ch, job["title"])
            if thread is None:
                return
            self.service.set_thread(job["id"], thread.id)
            for member_id in (job["poster_id"], *self.service.attempters(job["id"])):
                await app.gateway.add_to_thread(thread, member_id)
            await app.gateway.send(
                thread, c.t("jobs.thread_intro", poster=f"<@{job['poster_id']}>", helper=f"<@{uid}>"),
                view=_buttons(job["id"], [("complete", c.t("jobs.btn_complete"), discord.ButtonStyle.success)]),
                allowed_mentions=discord.AllowedMentions(users=True))

    async def close_threads(self) -> None:
        """Delete (or archive) threads of finished jobs. A completed job's thread waits for the poster's vouch."""
        delete = bool(self.app.cfg.job_rules.get("delete_thread_when_done", True))
        for job in self.service.threads_to_close():
            thread = await self._thread(job)
            if thread is not None and not await self.app.gateway.close_thread(thread, delete=delete):
                continue  # try again next hour
            log.info("job %s thread closed (status=%s, vouched=%s)", job["id"], job["status"], bool(job["vouched_at"]))
            self.service.mark_thread_closed(job["id"])

    async def on_vouched(self, voucher_id: int, recipients: list[int]) -> None:
        """Called by the Vouch cog for every recorded vouch, counted or not."""
        if self.service.note_vouch(voucher_id, recipients):
            await self.close_threads()

    async def _after_completion(self, job) -> None:
        """Thank everyone in the thread and ask the poster to vouch; the thread closes after the vouch."""
        c = self.app.copy
        if not job["helper_id"]:
            return
        helper = await self.app.member(job["helper_id"])
        name = helper.display_name if helper is not None else "the helper"
        view = _buttons(job["id"], [("vouch", c.t("jobs.btn_vouch", name=name)[:80], discord.ButtonStyle.primary)])
        await self._say(job, c.t("jobs.done_thread", helper=f"<@{job['helper_id']}>", poster=f"<@{job['poster_id']}>"),
                        [job["poster_id"], job["helper_id"]], view=view)

    # ------------------------------------------------------------ job buttons
    async def on_accept(self, interaction: discord.Interaction, job_id: int, *, pending_ok: bool = False) -> None:
        app, c = self.app, self.app.copy
        uid = interaction.user.id
        app.adopt_from_roles(interaction.user)
        user = app.engine.get_user(uid)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        days = None
        if member is not None and member.joined_at is not None:
            days = (datetime.now(timezone.utc) - member.joined_at).total_seconds() / 86400
        job, reason = self.service.accept(job_id, uid, helper_rank=user["rank_key"] if user else None,
                                          days_in_guild=days, pending_ok=pending_ok)
        if reason == "pending":
            # someone may already have finished it: let them decide whether it's worth their time
            view = _buttons(job_id, [("join", c.t("jobs.btn_join"), discord.ButtonStyle.secondary)], timeout=5 * 60)
            await interaction.response.send_message(
                c.t("jobs.pending_warning", helper=f"<@{job['helper_id']}>"), view=view, ephemeral=True)
            return
        if reason:
            msg = {
                "taken": c.t("jobs.taken"),
                "own": c.t("jobs.cannot_accept_own"),
                "already": c.t("jobs.already_attempting"),
                "full": c.t("jobs.full"),
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
        await self._join_thread(job, uid)

    async def on_join(self, interaction: discord.Interaction, job_id: int) -> None:
        """"Attempt anyway" after the pending-completion heads-up."""
        await self.on_accept(interaction, job_id, pending_ok=True)

    async def on_cancel(self, interaction: discord.Interaction, job_id: int) -> None:
        job = self.service.cancel(job_id, interaction.user.id)
        if job is None:
            await interaction.response.send_message(self.app.copy.t("jobs.not_yours"), ephemeral=True)
            return
        await interaction.response.send_message(self.app.copy.t("jobs.cancelled_note"), ephemeral=True)
        await self._refresh(job)
        await self.close_threads()

    async def on_complete(self, interaction: discord.Interaction, job_id: int) -> None:
        c = self.app.copy
        uid = interaction.user.id
        job = self.service.mark_complete(job_id, uid)
        if job is None:
            job = self.service.get(job_id)
            if job is not None and uid in self.service.pending_completions(job_id):
                msg = c.t("jobs.already_marked")
            elif job is not None and job["poster_id"] == uid:
                msg = c.t("jobs.poster_waits")
            elif job is not None and job["status"] in JobService.JOINABLE:
                msg = c.t("jobs.not_attempting")
            else:
                msg = c.t("jobs.taken")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        log.info("job %s marked complete by %s", job_id, uid)
        await interaction.response.send_message(c.t("jobs.marked_complete"), ephemeral=True)
        await self._refresh(job)
        await self._say(job, c.t("jobs.awaiting_confirm", poster=f"<@{job['poster_id']}>", requester=f"<@{uid}>"),
                        [job["poster_id"]], view=_buttons(job_id, [
                            ("review", c.t("jobs.btn_review"), discord.ButtonStyle.success)]))

    async def on_review(self, interaction: discord.Interaction, job_id: int) -> None:
        """Poster only: the completion order (1st, 2nd, 3rd...) with confirm / not done for each."""
        app, c = self.app, self.app.copy
        job = self.service.get(job_id)
        if job is None or job["status"] != "awaiting_confirm":
            await interaction.response.send_message(c.t("jobs.taken"), ephemeral=True)
            return
        if interaction.user.id != job["poster_id"]:
            await interaction.response.send_message(c.t("jobs.only_poster"), ephemeral=True)
            return
        content, view = await self._review_panel(job_id)
        await interaction.response.send_message(content, view=view, ephemeral=True)

    # buttons on older messages
    on_confirm = on_review
    on_notdone = on_review

    async def _review_panel(self, job_id: int) -> tuple[str, CompletionReview | None]:
        app = self.app
        rows = self.service.completions(job_id)
        pending = []
        for n, r in enumerate(rows, 1):
            if r["rejected_at"] is None:
                m = await app.member(r["user_id"])
                pending.append((r["user_id"], f"{_ordinal(n)}: {m.display_name if m else r['user_id']}"))
        return completion_order_text(app, rows), (CompletionReview(self, job_id, pending) if pending else None)

    async def confirm_completion(self, interaction: discord.Interaction, job_id: int, user_id: int | None) -> None:
        app, c = self.app, self.app.copy
        job, award = self.service.confirm(job_id, interaction.user.id, user_id)
        if award is None:
            waiting = job is not None and job["status"] == "awaiting_confirm"
            msg = c.t("jobs.only_poster") if waiting and interaction.user.id != job["poster_id"] else c.t("jobs.taken")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        log.info("job %s confirmed: helper %s awarded %s (cap %s)", job_id, job["helper_id"], award.points, award.capped)
        await interaction.response.edit_message(content=c.t("jobs.completed", helper=f"<@{job['helper_id']}>"),
                                                view=None)
        await self._refresh(job)
        if award.change is not None:
            await app.apply_change(award.change, await app.member(job["helper_id"]))
        await self._after_completion(job)

    async def reject_completion(self, interaction: discord.Interaction, job_id: int, user_id: int | None) -> None:
        c = self.app.copy
        job, turned_down = self.service.reject_completion(job_id, interaction.user.id, user_id)
        if job is None:
            await interaction.response.send_message(c.t("jobs.taken"), ephemeral=True)
            return
        log.info("job %s: poster turned down %s's completion", job_id, turned_down)
        if job["status"] == "awaiting_confirm":  # someone else is next in line: show the updated order
            content, view = await self._review_panel(job_id)
            await interaction.response.edit_message(content=content, view=view)
        else:
            await interaction.response.edit_message(content=c.t("jobs.sent_back"), view=None)
        await self._refresh(job)
        await self._say(job, c.t("jobs.not_done_yet", helper=f"<@{turned_down}>", poster=f"<@{job['poster_id']}>"),
                        [turned_down])

    async def on_vouch(self, interaction: discord.Interaction, job_id: int) -> None:
        """The poster's "Vouch for <helper>" button after completion: opens the usual vouch form."""
        app, c = self.app, self.app.copy
        job = self.service.get(job_id)
        if job is None or job["status"] != "completed" or not job["helper_id"]:
            await interaction.response.send_message(c.t("jobs.taken"), ephemeral=True)
            return
        if interaction.user.id != job["poster_id"]:
            await interaction.response.send_message(c.t("jobs.vouch_only_poster"), ephemeral=True)
            return
        if job["vouched_at"]:
            await interaction.response.send_message(c.t("jobs.already_vouched"), ephemeral=True)
            return
        helper = await app.member(job["helper_id"])
        vouch = self.bot.get_cog("Vouch")
        if helper is None or vouch is None:
            await interaction.response.send_message(c.t("errors.generic"), ephemeral=True)
            return
        await interaction.response.send_modal(VouchModal(vouch, helper))  # type: ignore[arg-type]

    # ------------------------------------------------------------ nudges / expiry
    @tasks.loop(hours=1)
    async def expiry(self) -> None:
        await self.housekeeping()

    async def housekeeping(self) -> None:
        c = self.app.copy
        for job in self.service.due_checkins():
            helpers = self.service.attempters(job["id"])
            await self._say(job, c.t("jobs.checkin", poster=f"<@{job['poster_id']}>", helpers=_mentions(helpers)),
                            [job["poster_id"], *helpers])
        for job in self.service.due_escalations():
            log.info("job %s confirmation stalled; sent to mods", job["id"])
            await self._refresh(job)
            await self.send_to_review(job["id"], kind="stalled", heading="was marked done but never confirmed")
        for job in self.service.expire_due():
            log.info("job %s expired", job["id"])
            await self._refresh(job)
        await self.close_threads()

    @expiry.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Jobs(bot))
