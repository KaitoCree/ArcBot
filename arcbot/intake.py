"""Stats intake decisions shared by onboarding and promotion. DB only, no Discord."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .db import iso, parse_iso, transaction, utcnow
from .engine import RankChange, RankEngine
from .ranks import VETERAN
from .scoring import Stats, assessed_rank, plausibility_flags, rating


@dataclass
class Evaluation:
    rating: float
    assessed: str
    flags: list[str]
    needs_review: bool


@dataclass
class SubmitResult:
    submission_id: int
    status: str  # auto_placed | pending | no_change
    evaluation: Evaluation
    change: RankChange | None
    provisional_rank: str | None = None


@dataclass
class DecisionResult:
    ok: bool
    reason: str = ""  # already_decided | not_found | needs_gm
    discord_id: int | None = None
    status: str | None = None
    change: RankChange | None = None
    final_rank: str | None = None
    kind: str | None = None
    image_path: str | None = None
    extra: dict = field(default_factory=dict)


class IntakeService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, engine: RankEngine):
        self.conn = conn
        self.cfg = cfg
        self.engine = engine
        self.ranks = engine.ranks

    # ------------------------------------------------------------- checks
    def cooldown_days_left(self, discord_id: int, now: datetime | None = None) -> int:
        u = self.engine.get_user(discord_id)
        if u is None:
            return 0
        ready = parse_iso(u["stats_cooldown_until"])
        if ready is None:
            last = parse_iso(u["last_stats_submission_at"])
            if last is None:
                return 0  # first stats submission (e.g. after Skip) has no cooldown
            ready = last + timedelta(days=self.cfg.promotion_cooldown_days)
        now = now or utcnow()
        if now >= ready:
            return 0
        return max(1, (ready - now).days + (1 if (ready - now).seconds else 0))

    def pending_for(self, discord_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM stats_submissions WHERE discord_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (discord_id,),
        ).fetchone()

    def evaluate(self, stats: Stats, *, name_mismatch: bool = False) -> Evaluation:
        score = rating(stats, self.cfg)
        assessed = assessed_rank(score, self.cfg)
        flags = plausibility_flags(stats, self.cfg, name_mismatch=name_mismatch)
        above = self.ranks.order(assessed) > self.ranks.order(self.cfg.flag_assessed_rank_above)
        return Evaluation(score, assessed, flags, needs_review=above or bool(flags))

    # ------------------------------------------------------------- submit
    def submit(
        self,
        discord_id: int,
        *,
        kind: str,
        source: str,
        ingame_name: str,
        stats: Stats,
        name_mismatch: bool = False,
        image_path: str | None = None,
        notes: list[str] | None = None,
        now: datetime | None = None,
    ) -> SubmitResult:
        """`notes` are shown to mods on a pending claim (e.g. unsure OCR fields the member confirmed);
        they never force a review on their own."""
        now = now or utcnow()
        ev = self.evaluate(stats, name_mismatch=name_mismatch)
        with transaction(self.conn):
            user = self.engine.ensure_user(discord_id, ingame_name)
            prev_rank = None if user["provisional"] else user["rank_key"]
            prev_submission_at = user["last_stats_submission_at"]
            prev_cooldown_until = user["stats_cooldown_until"]
            current = user["rank_key"]

            change: RankChange | None = None
            provisional: str | None = None
            if current is not None and self.ranks.order(ev.assessed) <= self.ranks.order(current):
                status = "no_change"  # upward only; stored, nothing changes
            elif not ev.needs_review:
                status = "auto_placed"
                change = self.engine.place(discord_id, ev.assessed, ingame_name=ingame_name)
            else:
                status = "pending"
                # provisional = the highest rank that auto-places anyway, never above the claim, never below now
                prov = self.cfg.provisional_rank
                if self.ranks.order(ev.assessed) < self.ranks.order(prov):
                    prov = ev.assessed
                if self.ranks.order(prov) > self.ranks.order(current):
                    provisional = prov
                    change = self.engine.place(discord_id, prov, provisional=True, ingame_name=ingame_name)

            # a result that changed nothing (submitted too early) only earns the short wait
            wait = self.cfg.no_change_cooldown_days if status == "no_change" else self.cfg.promotion_cooldown_days
            self.conn.execute(
                "UPDATE users SET last_stats_submission_at = ?, stats_cooldown_until = ? WHERE discord_id = ?",
                (iso(now), iso(now + timedelta(days=wait)), discord_id),
            )
            shown_flags = ev.flags + (list(notes or []) if status == "pending" else [])
            cur = self.conn.execute(
                """INSERT INTO stats_submissions(discord_id, kind, source, ingame_name, hours, knockouts,
                       squad_revives, stranger_revives, quests, containers, expeditions, rating, assessed_rank,
                       flags, status, prev_rank_key, prev_submission_at, prev_cooldown_until, image_path,
                       created_at, decided_at, decided_rank)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (discord_id, kind, source, ingame_name, stats.hours, stats.knockouts, stats.squad_revives,
                 stats.stranger_revives, stats.quests, stats.containers, stats.expeditions, ev.rating,
                 ev.assessed, json.dumps(shown_flags), status, prev_rank, prev_submission_at, prev_cooldown_until,
                 image_path if status == "pending" else None, iso(now),
                 None if status == "pending" else iso(now),
                 ev.assessed if status == "auto_placed" else None),
            )
            sub_id = int(cur.lastrowid)
        if status != "pending":
            delete_image(image_path)
        return SubmitResult(sub_id, status, ev, change, provisional)

    def set_review_message(self, sub_id: int, channel_id: int, message_id: int) -> None:
        self.conn.execute(
            "UPDATE stats_submissions SET review_channel_id = ?, review_message_id = ? WHERE id = ?",
            (channel_id, message_id, sub_id),
        )

    def get(self, sub_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM stats_submissions WHERE id = ?", (sub_id,)).fetchone()

    # ------------------------------------------------------------ decide
    def decide(
        self,
        sub_id: int,
        action: str,
        mod_id: int,
        *,
        rank: str | None = None,
        is_guild_master: bool = False,
        now: datetime | None = None,
    ) -> DecisionResult:
        now = now or utcnow()
        with transaction(self.conn):
            sub = self.get(sub_id)
            if sub is None:
                return DecisionResult(False, "not_found")
            if sub["status"] != "pending":
                return DecisionResult(False, "already_decided", discord_id=sub["discord_id"], status=sub["status"])
            uid = sub["discord_id"]
            prev = sub["prev_rank_key"]
            user = self.engine.get_user(uid)
            change: RankChange | None = None
            final: str | None = None

            if action in ("approve", "set_rank"):
                target = sub["assessed_rank"] if action == "approve" else rank
                if target not in self.ranks.keys:
                    return DecisionResult(False, "bad_rank")
                if action == "set_rank" and target == VETERAN and not is_guild_master:
                    return DecisionResult(False, "needs_gm")
                # never below the real rank they had before this claim
                target = self.ranks.higher(target, prev) or target
                grant = target == VETERAN
                if user is not None and user["rank_key"] is not None and not user["provisional"] \
                        and self.ranks.order(target) <= self.ranks.order(user["rank_key"]):
                    change = None  # they climbed meanwhile; nothing to do
                else:
                    change = self.engine.place(uid, target, grant_veteran=grant, ingame_name=sub["ingame_name"])
                final = self.engine.get_user(uid)["rank_key"]
                status = "approved" if action == "approve" else "set_rank"
            elif action == "deny":
                if user is not None and user["provisional"]:
                    change = self.engine.revert_provisional(uid, prev, self.cfg.skip_rank)
                if self.cfg.deny_resets_cooldown:
                    self.conn.execute(
                        "UPDATE users SET last_stats_submission_at = ?, stats_cooldown_until = ? WHERE discord_id = ?",
                        (sub["prev_submission_at"], sub["prev_cooldown_until"], uid),
                    )
                final = self.engine.get_user(uid)["rank_key"]
                status = "denied"
            else:
                return DecisionResult(False, "bad_action")

            self.conn.execute(
                "UPDATE stats_submissions SET status = ?, decided_by = ?, decided_rank = ?, decided_at = ?,"
                " image_path = NULL WHERE id = ?",
                (status, mod_id, final, iso(now), sub_id),
            )
        delete_image(sub["image_path"])
        return DecisionResult(True, discord_id=uid, status=status, change=change, final_rank=final,
                              kind=sub["kind"], image_path=sub["image_path"])

    def due_reminders(self, now: datetime | None = None) -> list[sqlite3.Row]:
        now = now or utcnow()
        cutoff = iso(now - timedelta(hours=self.cfg.reminder_after_hours))
        return self.conn.execute(
            "SELECT * FROM stats_submissions WHERE status = 'pending' AND reminded = 0 AND created_at <= ?",
            (cutoff,),
        ).fetchall()

    def mark_reminded(self, sub_id: int) -> None:
        self.conn.execute("UPDATE stats_submissions SET reminded = 1 WHERE id = ?", (sub_id,))


def delete_image(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
