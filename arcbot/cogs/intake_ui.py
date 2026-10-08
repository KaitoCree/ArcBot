"""Stats intake UI shared by #apply and #rank-promotion: upload modal, manual two-step modals,
read-back, partial-manual for unreadable fields, and the #mod-review pending flow."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from ..config import ROOT
from ..intake import DecisionResult, SubmitResult, delete_image
from ..ocr import parse as ocr_parse
from ..ocr import reader
from ..ranks import VETERAN
from ..scoring import (FIELD_ORDER, FLAG_LABELS, ParseError, Stats, format_hours, parse_hours, parse_int,
                       rating_breakdown)

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("arcbot.intake")

UPLOAD_DIR = ROOT / "data" / "uploads"
PAGE1 = ("hours", "knockouts", "squad_revives", "stranger_revives")
PAGE2 = ("quests", "containers", "expeditions")
SESSION_TTL_S = 30 * 60
ALLOWED_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp"}


@dataclass
class IntakeSession:
    user_id: int
    kind: str  # onboarding | promotion
    name: str = ""
    values: dict[str, float | int | None] = field(default_factory=lambda: {f: None for f in FIELD_ORDER})
    unsure: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=lambda: set(FIELD_ORDER))
    source: str = "manual"
    typed_after_ocr: bool = False
    image_path: str | None = None
    ocr_name: str | None = None
    ocr_name_conf: float = 0.0
    name_typed: bool = False  # typed by the member (vs read from the screenshot)
    raw: dict[str, str] = field(default_factory=dict)  # last typed strings, to refill modals after an error
    created: float = field(default_factory=time.monotonic)

    def needs_input(self) -> list[str]:
        """Fields that must be typed before confirming: unreadable ones only. Numbers the reader was unsure
        of are shown marked and the member may confirm them as they are."""
        return [f for f in FIELD_ORDER if f in self.missing]

    def name_missing(self) -> bool:
        return not self.name.strip()

    def blocked(self) -> bool:
        return bool(self.needs_input()) or self.name_missing()

    def stats(self) -> Stats:
        v = self.values
        return Stats(float(v["hours"] or 0), int(v["knockouts"] or 0), int(v["squad_revives"] or 0),
                     int(v["stranger_revives"] or 0), int(v["quests"] or 0), int(v["containers"] or 0),
                     int(v["expeditions"] or 0))


def _app(interaction: discord.Interaction) -> "App":
    return interaction.client.app  # type: ignore[attr-defined]


def _fmt(f: str, v: float | int | None) -> str:
    if v is None:
        return ""
    return format_hours(float(v)) if f == "hours" else f"{int(v):,}"


class IntakeFlow:
    """Owns in-memory sessions and every step of the intake UI."""

    def __init__(self, app: "App"):
        self.app = app
        self.sessions: dict[int, IntakeSession] = {}
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- sessions
    def session(self, user_id: int) -> IntakeSession | None:
        s = self.sessions.get(user_id)
        if s and time.monotonic() - s.created > SESSION_TTL_S:
            self.drop(user_id)
            return None
        return s

    def start(self, user_id: int, kind: str) -> IntakeSession:
        self.drop(user_id)
        s = IntakeSession(user_id, kind)
        self.sessions[user_id] = s
        return s

    def drop(self, user_id: int, *, keep_image: bool = False) -> None:
        s = self.sessions.pop(user_id, None)
        if s and s.image_path and not keep_image:
            delete_image(s.image_path)

    def sweep(self) -> None:
        now = time.monotonic()
        for uid in [u for u, s in self.sessions.items() if now - s.created > SESSION_TTL_S]:
            self.drop(uid)

    def cleanup_orphan_uploads(self) -> int:
        keep = {r[0] for r in self.app.conn.execute(
            "SELECT image_path FROM stats_submissions WHERE status='pending' AND image_path IS NOT NULL")}
        keep |= {s.image_path for s in self.sessions.values() if s.image_path}
        n = 0
        for p in UPLOAD_DIR.glob("*"):
            if str(p) not in keep:
                delete_image(str(p))
                n += 1
        return n

    # ------------------------------------------------------------ gatekeeping
    async def precheck(self, interaction: discord.Interaction, kind: str) -> bool:
        """Common checks before opening any intake modal. Replies and returns False when blocked."""
        app = self.app
        c = app.copy
        uid = interaction.user.id
        app.adopt_from_roles(interaction.user)
        user = app.engine.get_user(uid)
        if kind == "onboarding" and user and user["rank_key"] and not user["provisional"]:
            await interaction.response.send_message(c.t("apply.already_placed"), ephemeral=True)
            return False
        if kind == "promotion":
            if not user or not user["rank_key"]:
                await interaction.response.send_message(c.t("promotion.not_placed"), ephemeral=True)
                return False
            if user["rank_key"] == VETERAN:
                await interaction.response.send_message(c.t("promotion.veteran_note"), ephemeral=True)
                return False
        if app.intake.pending_for(uid):
            await interaction.response.send_message(c.t("apply.pending_wait"), ephemeral=True)
            return False
        if kind == "promotion":
            left = app.intake.cooldown_days_left(uid)
            if left:
                await interaction.response.send_message(c.t("promotion.cooldown", days_left=left), ephemeral=True)
                return False
        return True

    # ------------------------------------------------------------------ upload
    async def open_upload(self, interaction: discord.Interaction, kind: str) -> None:
        if not await self.precheck(interaction, kind):
            return
        await interaction.response.send_modal(UploadModal(self, kind))

    async def open_manual(self, interaction: discord.Interaction, kind: str) -> None:
        if not await self.precheck(interaction, kind):
            return
        s = self.start(interaction.user.id, kind)
        await interaction.response.send_modal(StatsModal(self, s, list(PAGE1), title_key="apply.manual_title_1",
                                                         include_name=True, then="page2"))

    async def handle_upload(self, interaction: discord.Interaction, kind: str, name: str,
                            files: list[discord.Attachment]) -> None:
        app = self.app
        c = app.copy
        max_mb = float(app.cfg.ocr.get("max_image_mb", 8))
        att = files[0] if files else None
        ctype = (att.content_type or "").split(";")[0].lower() if att else ""
        if att is None or att.size > max_mb * 1024 * 1024 or ctype not in ALLOWED_TYPES:
            await interaction.response.send_message(c.t("apply.bad_file", max_mb=int(max_mb)), ephemeral=True)
            return
        await interaction.response.send_message(c.t("apply.reading"), ephemeral=True)
        s = self.start(interaction.user.id, kind)
        s.name = name.strip()[:64]
        s.name_typed = bool(s.name)
        s.source = "ocr"
        try:
            data = await att.read()
        except discord.HTTPException:
            await interaction.edit_original_response(content=c.t("errors.generic"))
            return
        path = UPLOAD_DIR / f"{interaction.user.id}-{int(time.time())}.{ALLOWED_TYPES[ctype]}"
        path.write_bytes(data)
        s.image_path = str(path)
        loop = asyncio.get_running_loop()
        async with app.ocr_semaphore:
            result = await loop.run_in_executor(None, reader.read_stats, data, app.cfg.ocr)
        log.info("ocr for %s: missing=%s unsure=%s available=%s", interaction.user.id, sorted(result.missing),
                 sorted(result.unsure), result.available)
        for f in FIELD_ORDER:
            s.values[f] = result.values.get(f)
        s.missing = set(result.missing)
        s.unsure = set(result.unsure)
        s.ocr_name, s.ocr_name_conf = result.ingame_name, result.name_conf
        if not s.name and s.ocr_name and s.ocr_name_conf >= 0.6:
            s.name = s.ocr_name[:64]  # left blank: use the name from the screenshot, shown on the read-back
        content, view = self.readback(s)
        await interaction.edit_original_response(content=content, view=view)

    # ---------------------------------------------------------------- readback
    def readback(self, s: IntakeSession) -> tuple[str, discord.ui.View]:
        c = self.app.copy
        shown_name = s.name or f"_{c.t('apply.unreadable_value')}_"
        lines = [f"**{c.t('apply.readback_title')}**", f"**{c.t('apply.modal_name_label')}:** {shown_name}"]
        for f in FIELD_ORDER:
            label = c.t(f"apply.fields.{f}")
            if f in s.missing:
                lines.append(f"**{label}:** _{c.t('apply.unreadable_value')}_")
            else:
                suffix = c.t("apply.readback_unsure_suffix") if f in s.unsure else ""
                lines.append(f"**{label}:** {_fmt(f, s.values[f])}{suffix}")
        need = s.needs_input()
        labels = ([c.t("apply.modal_name_label").lower()] if s.name_missing() else []) +             [c.t(f"apply.fields.{f}").lower() for f in need]
        if labels:
            listed = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
            lines.append("")
            lines.append(c.t("apply.missing_fields", fields=listed, them="it" if len(labels) == 1 else "them"))
        elif s.unsure:
            lines.append("")
            lines.append(c.t("apply.unsure_note"))
        return "\n".join(lines), ReadbackView(self, s)

    async def show_readback(self, interaction: discord.Interaction, s: IntakeSession) -> None:
        content, view = self.readback(s)
        await interaction.response.send_message(content, view=view, ephemeral=True)

    # ---------------------------------------------------------------- finalize
    async def finalize(self, interaction: discord.Interaction, s: IntakeSession) -> None:
        app = self.app
        c = app.copy
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        # re-check gates: the user may have double-submitted from two windows
        if app.intake.pending_for(uid) or (s.kind == "promotion" and app.intake.cooldown_days_left(uid)):
            self.drop(uid)
            await interaction.followup.send(c.t("apply.pending_wait"), ephemeral=True)
            return
        if s.blocked():
            await interaction.followup.send(c.t("errors.generic"), ephemeral=True)
            return
        mismatch = bool(s.name_typed and s.ocr_name and s.ocr_name_conf >= 0.8
                        and not ocr_parse.names_match(s.name, s.ocr_name))
        notes = (["unsure_confirmed"] if s.unsure else []) +             (["name_from_screenshot"] if s.source != "manual" and not s.name_typed else [])
        source = s.source if not s.typed_after_ocr else "mixed"
        res = app.intake.submit(uid, kind=s.kind, source=source, ingame_name=s.name, stats=s.stats(),
                                name_mismatch=mismatch, image_path=s.image_path, notes=notes)
        self.drop(uid, keep_image=res.status == "pending")
        member = interaction.user if isinstance(interaction.user, discord.Member) else await app.member(uid)
        log.info("intake %s for %s: %s (assessed %s, flags %s)", s.kind, uid, res.status,
                 res.evaluation.assessed, res.evaluation.flags)

        if res.status == "auto_placed":
            await app.apply_change(res.change, member, name=s.name)
            rank = app.engine.get_user(uid)["rank_key"]
            msg = c.t("placement.you_are_placed", name=s.name, rank=app.display_rank(rank))
        elif res.status == "pending":
            await app.apply_change(res.change, member, announce=False)
            current = app.engine.get_user(uid)["rank_key"]
            msg = c.t("placement.pending", provisional_rank=app.display_rank(current))
            await self.post_review(res, s, member)
        else:
            msg = c.t("placement.already_at_or_above")
        await interaction.followup.send(msg, ephemeral=True)

    # ---------------------------------------------------------- #mod-review
    def review_embed(self, sub: Any, member: discord.abc.User | None, *, decided: str | None = None) -> discord.Embed:
        app = self.app
        flags = json.loads(sub["flags"] or "[]")
        color = discord.Color.orange() if decided is None else discord.Color.dark_grey()
        e = discord.Embed(title=f"Stats check #{sub['id']} ({sub['kind']})", color=color)
        who = member.mention if member else f"<@{sub['discord_id']}>"
        e.add_field(name="Member", value=f"{who}\nIn-game: **{sub['ingame_name']}**", inline=True)
        e.add_field(name="Assessed", value=f"**{app.display_rank(sub['assessed_rank'])}** (rating {sub['rating']:.1f})",
                    inline=True)
        e.add_field(name="Source", value=sub["source"], inline=True)
        nums = (f"Hours {format_hours(sub['hours'])} · Knockouts {sub['knockouts']:,} · Revives "
                f"{sub['squad_revives']:,} squad / {sub['stranger_revives']:,} stranger\n"
                f"Quests {sub['quests']:,} · Containers {sub['containers']:,} · Expeditions {sub['expeditions']:,}")
        e.add_field(name="Numbers", value=nums, inline=False)
        try:  # mod-only: how the rating was made up, to sanity-check odd profiles
            stats = Stats(sub["hours"], sub["knockouts"], sub["squad_revives"], sub["stranger_revives"],
                          sub["quests"], sub["containers"], sub["expeditions"])
            parts = rating_breakdown(stats, app.cfg)
            e.add_field(name="Rating breakdown", inline=False,
                        value=" · ".join(f"{k} {v:.1f}/{app.cfg.metrics[k]['max_points']}" for k, v in parts.items()))
        except (TypeError, KeyError):
            pass
        e.add_field(name="Flags", value=", ".join(FLAG_LABELS.get(f, f) for f in flags) or "none (rank above "
                    "auto-place limit)", inline=False)
        if sub["prev_rank_key"]:
            e.add_field(name="Rank before this", value=app.display_rank(sub["prev_rank_key"]), inline=True)
        if decided:
            e.add_field(name="Decision", value=decided, inline=False)
        return e

    async def post_review(self, res: SubmitResult, s: IntakeSession, member: discord.abc.User | None) -> None:
        app = self.app
        ch = app.channel("mod_review")
        sub = app.intake.get(res.submission_id)
        if ch is None or sub is None:
            await app.alerts.alert("no_mod_review", "A stats claim is pending but #mod-review is missing.")
            return
        files = []
        embed = self.review_embed(sub, member)
        if sub["image_path"] and Path(sub["image_path"]).exists():
            fname = "stats" + Path(sub["image_path"]).suffix
            files.append(discord.File(sub["image_path"], filename=fname))
            embed.set_image(url=f"attachment://{fname}")
        msg = await app.gateway.send(ch, embed=embed, view=review_view(sub["id"]), files=files, mod_only=True)
        if msg is not None:
            app.intake.set_review_message(sub["id"], ch.id, msg.id)

    async def apply_decision(self, actor: discord.abc.User, review_message: discord.Message | None, sub_id: int,
                             result: DecisionResult) -> None:
        app = self.app
        c = app.copy
        sub = app.intake.get(sub_id)
        member = await app.member(result.discord_id) if result.discord_id else None
        name = sub["ingame_name"] if sub else ""
        if result.status in ("approved", "set_rank"):
            # an onboarding claim held a provisional rank, so the engine sees old != None; it is still a welcome
            await app.apply_change(result.change, member, name=name,
                                   first_placement=True if result.kind == "onboarding" else None)
            text = c.t("placement.approved_dm", name=name, rank=app.display_rank(result.final_rank))
        else:
            await app.apply_change(result.change, member, announce=False)
            text = c.t("placement.denied_dm")
        if member is not None:
            sent = await app.gateway.dm(member, text)
            if not sent and (ch := app.channel("rank_promotion")) is not None:
                await app.gateway.send(ch, c.t("placement.dm_fallback", mention=member.mention, message=text),
                                       allowed_mentions=discord.AllowedMentions(users=[member]))
        label = {"approved": "Approved", "set_rank": "Rank set", "denied": "Denied"}[result.status]
        decided = f"{label} by {actor.mention}" + (
            f" → **{app.display_rank(result.final_rank)}**" if result.final_rank else "")
        if sub is not None and review_message is not None:
            await app.gateway.edit(review_message, embed=self.review_embed(sub, member, decided=decided),
                                   view=None, mod_only=True, attachments=[])


# ====================================================================== views
class UploadModal(discord.ui.Modal):
    def __init__(self, flow: IntakeFlow, kind: str):
        c = flow.app.copy
        super().__init__(title=c.t("apply.modal_title"), timeout=15 * 60)
        self.flow = flow
        self.kind = kind
        self.name_input = discord.ui.TextInput(custom_id="name", max_length=64, required=False)
        self.file_input = discord.ui.FileUpload(custom_id="shot", required=True, min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text=c.t("apply.modal_name_optional_label")[:45], component=self.name_input,
                                       description=c.t("apply.modal_name_optional_hint")[:100]))
        self.add_item(discord.ui.Label(text=c.t("apply.modal_file_label"), component=self.file_input,
                                       description=c.t("apply.modal_file_hint")[:100]))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.flow.handle_upload(interaction, self.kind, self.name_input.value, list(self.file_input.values))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _generic_error(interaction, error)


class StatsModal(discord.ui.Modal):
    """Up to five fields; `then` = 'page2' chains to the second manual page, 'readback' shows the read-back."""

    def __init__(self, flow: IntakeFlow, s: IntakeSession, fields_: list[str], *, title_key: str,
                 include_name: bool = False, then: str = "readback"):
        c = flow.app.copy
        super().__init__(title=c.t(title_key), timeout=15 * 60)
        self.flow, self.s, self.fields_, self.then = flow, s, fields_, then
        self.include_name = include_name
        self.inputs: dict[str, discord.ui.TextInput] = {}
        if include_name:
            ti = discord.ui.TextInput(label=c.t("apply.modal_name_label"), custom_id="name", min_length=2,
                                      max_length=64, default=s.raw.get("name", s.name) or None)
            self.inputs["name"] = ti
            self.add_item(ti)
        for f in fields_[: 5 - int(include_name)]:
            default = s.raw.get(f) or _fmt(f, s.values.get(f)) or None
            ti = discord.ui.TextInput(label=c.t(f"apply.fields.{f}"), custom_id=f, max_length=16, default=default,
                                      placeholder=c.t("apply.hours_hint") if f == "hours" else None)
            self.inputs[f] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        c = self.flow.app.copy
        s = self.s
        errors: list[str] = []
        parsed: dict[str, float | int] = {}
        for key, ti in self.inputs.items():
            s.raw[key] = ti.value
            if key == "name":
                continue
            try:
                parsed[key] = parse_hours(ti.value) if key == "hours" else parse_int(ti.value)
            except ParseError:
                errors.append(c.t("apply.bad_hours") if key == "hours" else
                              f"**{c.t(f'apply.fields.{key}')}:** {c.t('apply.bad_number')}")
        if errors:
            await interaction.response.send_message(
                "\n".join(errors), ephemeral=True,
                view=RetryView(self.flow, s, self.fields_, self.title_key_for_retry(), self.include_name, self.then))
            return
        if "name" in self.inputs:
            s.name = self.inputs["name"].value.strip()
            s.name_typed = True
        for f, v in parsed.items():
            s.values[f] = v
            s.missing.discard(f)
            s.unsure.discard(f)
            s.raw.pop(f, None)
        if s.source == "ocr":
            s.typed_after_ocr = True
        if self.then == "page2":
            await interaction.response.send_message(c.t("apply.manual_next"), ephemeral=True,
                                                    view=NextView(self.flow, s))
            return
        await self.flow.show_readback(interaction, s)

    def title_key_for_retry(self) -> str:
        return {"page2": "apply.manual_title_1"}.get(self.then, "apply.fix_title")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _generic_error(interaction, error)


class _SessionView(discord.ui.View):
    def __init__(self, flow: IntakeFlow, s: IntakeSession):
        super().__init__(timeout=15 * 60)
        self.flow, self.s = flow, s

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.s.user_id or self.flow.session(self.s.user_id) is not self.s:
            await interaction.response.send_message(self.flow.app.copy.t("apply.session_expired"), ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await _generic_error(interaction, error)


class NextView(_SessionView):
    def __init__(self, flow: IntakeFlow, s: IntakeSession):
        super().__init__(flow, s)
        btn = discord.ui.Button(label=flow.app.copy.t("apply.btn_next"), style=discord.ButtonStyle.primary)
        btn.callback = self._next
        self.add_item(btn)

    async def _next(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            StatsModal(self.flow, self.s, list(PAGE2), title_key="apply.manual_title_2"))


class RetryView(_SessionView):
    def __init__(self, flow, s, fields_, title_key, include_name, then):
        super().__init__(flow, s)
        self.args = (fields_, title_key, include_name, then)
        btn = discord.ui.Button(label=flow.app.copy.t("apply.btn_fix"), style=discord.ButtonStyle.primary)
        btn.callback = self._retry
        self.add_item(btn)

    async def _retry(self, interaction: discord.Interaction) -> None:
        fields_, title_key, include_name, then = self.args
        await interaction.response.send_modal(StatsModal(self.flow, self.s, fields_, title_key=title_key,
                                                         include_name=include_name, then=then))


class ReadbackView(_SessionView):
    def __init__(self, flow: IntakeFlow, s: IntakeSession):
        super().__init__(flow, s)
        c = flow.app.copy
        if s.blocked():
            b = discord.ui.Button(label=c.t("apply.btn_type_missing"), style=discord.ButtonStyle.primary)
            b.callback = self._type_missing
        else:
            b = discord.ui.Button(label=c.t("apply.btn_looks_right"), style=discord.ButtonStyle.success)
            b.callback = self._looks_right
        self.add_item(b)
        fix = discord.ui.Button(label=c.t("apply.btn_fix"), style=discord.ButtonStyle.secondary)
        fix.callback = self._fix
        self.add_item(fix)

    async def _looks_right(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.flow.finalize(interaction, self.s)

    async def _type_missing(self, interaction: discord.Interaction) -> None:
        need = self.s.needs_input()
        with_name = self.s.name_missing()
        if len(need) + int(with_name) <= 5:
            modal = StatsModal(self.flow, self.s, need, title_key="apply.missing_title", include_name=with_name)
        else:
            modal = StatsModal(self.flow, self.s, list(PAGE1), title_key="apply.manual_title_1", include_name=True,
                               then="page2")
        await interaction.response.send_modal(modal)

    async def _fix(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StatsModal(self.flow, self.s, list(PAGE1),
                                                         title_key="apply.manual_title_1", include_name=True,
                                                         then="page2"))


# ------------------------------------------------------- persistent review UI
class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"arcbot:rev:(?P<action>approve|setrank|deny):(?P<id>\d+)"):
    def __init__(self, action: str, sub_id: int):
        labels = {"approve": ("Approve", discord.ButtonStyle.success), "setrank": ("Set rank", discord.ButtonStyle.primary),
                  "deny": ("Deny", discord.ButtonStyle.danger)}
        label, style = labels[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"arcbot:rev:{action}:{sub_id}"))
        self.action, self.sub_id = action, sub_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # type: ignore[override]
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        app = _app(interaction)
        if not app.is_mod(interaction.user):
            await interaction.response.send_message(app.copy.t("errors.not_allowed"), ephemeral=True)
            return
        flow: IntakeFlow = interaction.client.intake_flow  # type: ignore[attr-defined]
        if self.action == "setrank":
            await interaction.response.send_message("Pick the rank to place them at:", ephemeral=True,
                                                    view=SetRankView(flow, self.sub_id, interaction.message))
            return
        await interaction.response.defer()
        result = app.intake.decide(self.sub_id, self.action, interaction.user.id,
                                   is_guild_master=app.is_guild_master(interaction.user))
        if not result.ok:
            await interaction.followup.send(f"Nothing to do: {result.reason.replace('_', ' ')}.", ephemeral=True)
            return
        await flow.apply_decision(interaction.user, interaction.message, self.sub_id, result)


def review_view(sub_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for action in ("approve", "setrank", "deny"):
        v.add_item(ReviewButton(action, sub_id))
    return v


class SetRankView(discord.ui.View):
    def __init__(self, flow: IntakeFlow, sub_id: int, review_message: discord.Message | None):
        super().__init__(timeout=5 * 60)
        self.flow, self.sub_id, self.review_message = flow, sub_id, review_message
        opts = [discord.SelectOption(label=flow.app.display_rank(k), value=k) for k in flow.app.ranks.keys]
        sel = discord.ui.Select(placeholder="Rank", options=opts)
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction) -> None:
        app = self.flow.app
        rank = self.sel.values[0]
        result = app.intake.decide(self.sub_id, "set_rank", interaction.user.id, rank=rank,
                                   is_guild_master=app.is_guild_master(interaction.user))
        if not result.ok:
            msg = ("Only the Guild Master can grant Veteran." if result.reason == "needs_gm"
                   else f"Nothing to do: {result.reason.replace('_', ' ')}.")
            await interaction.response.edit_message(content=msg, view=None)
            return
        await interaction.response.edit_message(content=f"Done: {app.display_rank(result.final_rank)}.", view=None)
        await self.flow.apply_decision(interaction.user, self.review_message, self.sub_id, result)


async def _generic_error(interaction: discord.Interaction, error: Exception) -> None:
    log.exception("intake UI error", exc_info=error)
    app = _app(interaction)
    await app.alerts.alert("intake_error", f"Intake error: {type(error).__name__}. Check the logs.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(app.copy.t("errors.generic"), ephemeral=True)
        else:
            await interaction.response.send_message(app.copy.t("errors.generic"), ephemeral=True)
    except discord.HTTPException:
        pass
