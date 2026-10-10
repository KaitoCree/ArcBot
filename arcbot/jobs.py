"""Job board rules: listing checks, routing, attempt gating, completion awards with caps, expiry. No Discord.

Lifecycle: open -> accepted (one or more raiders attempting; more can still join) -> awaiting_confirm (at least one
attempter tapped "Mark complete"; the board shows "Pending Completion" and others can still finish and mark it too)
-> completed (the poster picked who really did it from the completion order, and that raider is rewarded).
helper_id is the earliest completion the poster hasn't turned down. If the first raider to mark it hadn't really
finished, the poster turns that one down and the next in line becomes the fastest successful completion.
After completion the private thread stays up until the poster vouches for the helper (or vouch_wait_days pass).
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from better_profanity import profanity

from .config import Config, JobTier
from .db import iso, parse_iso, transaction, utcnow
from .engine import RankChange, RankEngine

profanity.load_censor_words()

_URL = re.compile(
    r"https?://|www\.|discord\.gg/|\b[a-z0-9-]+\.(?:com|net|org|gg|io|me|xyz|ly|co|app|shop|store|link|tk|ru)\b",
    re.IGNORECASE)
_LEET = str.maketrans({"4": "a", "@": "a", "3": "e", "0": "o", "1": "i", "!": "i", "$": "s", "5": "s", "7": "t"})


def _norm_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _compress(text: str) -> str:
    return re.sub(r"[^a-z]", "", _norm_text(text).translate(_LEET))


@dataclass
class Hit:
    category: str
    term: str
    action: str  # hold | notify
    disguised: bool = False

    def __str__(self) -> str:
        return f"{self.category}: {self.term}" + (" (disguised)" if self.disguised else "")


def flag_hits(text: str, flag_list: dict) -> list[Hit]:
    """Every flag-list hit. Word-boundary match, plus a squashed match (spaces/punctuation removed,
    leetspeak undone) for longer terms to catch 'p ay pal' / 'c4shapp'.

    Each category's "action" decides routing: "hold" (default) waits for a mod before posting,
    "notify" posts straight away and gives the mods a heads-up."""
    low = _norm_text(text)
    squashed = _compress(text)
    hits: list[Hit] = []
    for cat, body in flag_list.items():
        if not isinstance(body, dict):
            continue
        action = body.get("action", "hold")
        for term in body.get("terms", []):
            t = term.lower()
            pattern = r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])"
            if re.search(pattern, low):
                hits.append(Hit(cat, term, action))
                continue
            sq = _compress(t)
            if len(sq) >= 6 and sq in squashed:
                hits.append(Hit(cat, term, action, disguised=True))
    return hits


def is_emoji_only(text: str) -> bool:
    stripped = re.sub(r"<a?:\w+:\d+>", "", text or "")
    return not any(ch.isalnum() for ch in stripped)


@dataclass
class CheckResult:
    reasons: list[str]  # hold: a mod looks before it posts
    notices: list[str] = field(default_factory=list)  # notify: posts now, mods get a heads-up

    @property
    def ok(self) -> bool:
        return not self.reasons


@dataclass
class Award:
    points: int
    change: RankChange | None
    capped: str | None  # pair_cap | daily_cap | None


class JobService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, engine: RankEngine):
        self.conn, self.cfg, self.engine = conn, cfg, engine
        self.rules = cfg.job_rules
        self.checks = cfg.job_checks

    def tier(self, key: str) -> JobTier:
        return self.cfg.job_tiers[key]

    def get(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    # ------------------------------------------------------------ checks
    def check_listing(self, poster_id: int, title: str, description: str, now: datetime | None = None) -> CheckResult:
        now = now or utcnow()
        c = self.checks
        reasons: list[str] = []
        notices: list[str] = []
        text = f"{title}\n{description}"
        hits = flag_hits(text, self.cfg.flag_list)
        hold = [str(h) for h in hits if h.action != "notify"]
        soft = [str(h) for h in hits if h.action == "notify"]
        if hold:
            reasons.append("flag list: " + "; ".join(hold[:5]))
        if soft:
            notices.append("flag list (heads-up): " + "; ".join(soft[:5]))
        if profanity.contains_profanity(text):
            reasons.append("profanity filter")
        if c.get("flag_any_url", True) and _URL.search(text):
            reasons.append("contains a link")
        if len(description.strip()) < int(c["min_description_chars"]):
            reasons.append("description too short")
        if len(description) > int(c["max_description_chars"]):
            reasons.append("description too long")
        if is_emoji_only(title) or is_emoji_only(description):
            reasons.append("emoji only")
        window = iso(now - timedelta(minutes=int(c["duplicate_window_minutes"])))
        norm = (_norm_text(title).strip(), _norm_text(description).strip())
        for row in self.conn.execute("SELECT title, description FROM jobs WHERE poster_id = ? AND created_at > ?",
                                     (poster_id, window)):
            if (_norm_text(row["title"]).strip(), _norm_text(row["description"]).strip()) == norm:
                reasons.append("duplicate of a recent post")
                break
        open_count = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE poster_id = ? AND kind = 'job' AND status IN ('pending_review','open',"
            "'accepted','awaiting_confirm')", (poster_id,)).fetchone()[0]  # challenges stay open; not counted
        if open_count >= int(self.rules["max_open_jobs_per_poster"]):
            reasons.append(f"poster already has {open_count} open jobs")
        posted_today = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE poster_id = ? AND created_at > ?",
                                         (poster_id, iso(now - timedelta(days=1)))).fetchone()[0]
        if posted_today >= int(self.rules["max_posts_per_poster_per_day"]):
            reasons.append(f"posting rate: {posted_today} posts in 24h")
        return CheckResult(reasons, notices)

    # ------------------------------------------------------------ create/route
    def create(self, poster_id: int, title: str, description: str, tier: str, *, has_image: bool,
               xp_multiplier: float = 1.0, kind: str = "job", now: datetime | None = None
               ) -> tuple[int, str, list[str]]:
        """Returns (job_id, status, reasons). 'open' with reasons = posted now, mods get a heads-up.

        xp_multiplier and kind="challenge" are Guild Master options; the caller checks who is posting."""
        now = now or utcnow()
        t = self.tier(tier)
        if xp_multiplier not in {b.multiplier for b in self.cfg.job_xp_boosts}:
            xp_multiplier = 1.0
        if kind not in ("job", "challenge"):
            kind = "job"
        res = self.check_listing(poster_id, title, description, now)
        reasons = list(res.reasons)
        if t.mod_preview:
            reasons.insert(0, f"{t.label} jobs always get a mod preview")
        status = "pending_review" if reasons else "open"
        reasons += res.notices
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO jobs(poster_id, title, description, tier, status, flag_reasons, has_image, created_at,"
                " posted_at, xp_multiplier, kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (poster_id, title, description, tier, status, json.dumps(reasons), int(has_image), iso(now),
                 iso(now) if status == "open" else None, float(xp_multiplier), kind))
            self.engine.touch_activity(poster_id, now)
        return int(cur.lastrowid), status, reasons

    def set_posted(self, job_id: int, channel_id: int | None, message_id: int | None) -> None:
        self.conn.execute("UPDATE jobs SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, message_id, job_id))

    def set_review(self, job_id: int, channel_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE jobs SET review_channel_id = ?, review_message_id = ? WHERE id = ?",
                          (channel_id, message_id, job_id))

    def set_thread(self, job_id: int, thread_id: int) -> None:
        self.conn.execute("UPDATE jobs SET thread_id = ? WHERE id = ?", (thread_id, job_id))

    def _transition(self, job_id: int, from_states: tuple[str, ...], to: str, **cols: object) -> sqlite3.Row | None:
        with transaction(self.conn):
            job = self.get(job_id)
            if job is None or job["status"] not in from_states:
                return None
            sets = ", ".join([f"{k} = ?" for k in cols] + ["status = ?"])
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*cols.values(), to, job_id))
            return self.get(job_id)

    def approve(self, job_id: int, now: datetime | None = None) -> sqlite3.Row | None:
        return self._transition(job_id, ("pending_review",), "open", posted_at=iso(now or utcnow()))

    def reject(self, job_id: int, now: datetime | None = None) -> sqlite3.Row | None:
        return self._transition(job_id, ("pending_review",), "rejected", closed_at=iso(now or utcnow()))

    def change_tier(self, job_id: int, tier: str) -> sqlite3.Row | None:
        self.tier(tier)
        return self._transition(job_id, ("pending_review",), "pending_review", tier=tier)

    # ------------------------------------------------------------ attempt
    JOINABLE = ("open", "accepted", "awaiting_confirm")

    def attempters(self, job_id: int) -> list[int]:
        return [r["user_id"] for r in self.conn.execute(
            "SELECT user_id FROM job_attempts WHERE job_id = ? ORDER BY joined_at, rowid", (job_id,))]

    def is_attempting(self, job_id: int, user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM job_attempts WHERE job_id = ? AND user_id = ?",
                                 (job_id, user_id)).fetchone() is not None

    def accept_block_reason(self, job: sqlite3.Row, helper_id: int, *, helper_rank: str | None,
                            days_in_guild: float | None) -> str | None:
        ranks = self.engine.ranks
        if job["kind"] == "challenge":
            return "challenge"  # squads join challenges through Form squad
        if job["status"] not in self.JOINABLE:
            return "taken"
        if job["poster_id"] == helper_id:
            return "own"
        if self.is_attempting(job["id"], helper_id):
            return "already"
        if helper_rank is None:
            return "not_placed"
        if not ranks.at_least(helper_rank, self.tier(job["tier"]).min_rank):
            return "rank_too_low"
        if days_in_guild is not None and days_in_guild < float(self.rules["helper_min_days_in_guild"]):
            return "too_new"
        if len(self.attempters(job["id"])) >= int(self.rules.get("max_attempters_per_job", 8)):
            return "full"
        return None

    def accept(self, job_id: int, helper_id: int, *, helper_rank: str | None, days_in_guild: float | None,
               pending_ok: bool = False, now: datetime | None = None) -> tuple[sqlite3.Row | None, str | None]:
        """Add helper_id to the raiders attempting this job. Several can attempt at once.

        While someone's completion is pending, joining needs pending_ok (the player saw the heads-up)."""
        now = now or utcnow()
        with transaction(self.conn):
            job = self.get(job_id)
            if job is None:
                return None, "taken"
            reason = self.accept_block_reason(job, helper_id, helper_rank=helper_rank, days_in_guild=days_in_guild)
            if reason is None and job["status"] == "awaiting_confirm" and not pending_ok:
                reason = "pending"
            if reason:
                return job, reason
            self.conn.execute("INSERT INTO job_attempts(job_id, user_id, joined_at) VALUES(?,?,?)",
                              (job_id, helper_id, iso(now)))
            if job["status"] == "open":
                self.conn.execute("UPDATE jobs SET status = 'accepted', accepted_at = ? WHERE id = ?",
                                  (iso(now), job_id))
            return self.get(job_id), None

    def cancel(self, job_id: int, by: int, now: datetime | None = None) -> sqlite3.Row | None:
        job = self.get(job_id)
        if job is None or job["poster_id"] != by:
            return None
        return self._transition(job_id, ("open", "accepted", "awaiting_confirm", "pending_review"), "cancelled",
                                closed_at=iso(now or utcnow()))

    def completions(self, job_id: int) -> list[sqlite3.Row]:
        """Everyone who marked this job complete, fastest first (turned-down ones included, with rejected_at)."""
        return self.conn.execute("SELECT * FROM job_completions WHERE job_id = ? ORDER BY claimed_at, rowid",
                                 (job_id,)).fetchall()

    def pending_completions(self, job_id: int) -> list[int]:
        return [r["user_id"] for r in self.completions(job_id) if r["rejected_at"] is None]

    def mark_complete(self, job_id: int, by: int, now: datetime | None = None) -> sqlite3.Row | None:
        """An attempter says they finished it. Everyone who does is queued in order; the poster picks."""
        now = now or utcnow()
        with transaction(self.conn):
            job = self.get(job_id)
            if (job is None or job["status"] not in ("accepted", "awaiting_confirm")
                    or not self.is_attempting(job_id, by)):
                return None
            if by in self.pending_completions(job_id):
                return None  # already in line
            # a raider the poster turned down can mark it again once they've really finished: back of the line
            self.conn.execute("INSERT INTO job_completions(job_id, user_id, claimed_at) VALUES(?,?,?)"
                              " ON CONFLICT(job_id, user_id) DO UPDATE SET claimed_at = excluded.claimed_at,"
                              " rejected_at = NULL", (job_id, by, iso(now)))
            if job["status"] == "accepted":
                self._transition(job_id, ("accepted",), "awaiting_confirm", helper_id=by,
                                 completion_requested_by=by, completion_requested_at=iso(now))
            return self.get(job_id)

    def reject_completion(self, job_id: int, by: int, user_id: int | None = None,
                          now: datetime | None = None) -> tuple[sqlite3.Row | None, int | None]:
        """The poster says user_id (default: the earliest pending) didn't really finish it. The next one in line
        moves up; with nobody left it's back to attempting. Returns (job, the turned-down raider)."""
        now = now or utcnow()
        with transaction(self.conn):
            job = self.get(job_id)
            if job is None or job["poster_id"] != by or job["status"] != "awaiting_confirm":
                return None, None
            pending = self.pending_completions(job_id)
            target = user_id if user_id is not None else (pending[0] if pending else None)
            if target not in pending:
                return None, None
            self.conn.execute("UPDATE job_completions SET rejected_at = ? WHERE job_id = ? AND user_id = ?",
                              (iso(now), job_id, target))
            rest = [u for u in pending if u != target]
            if rest:
                self.conn.execute("UPDATE jobs SET helper_id = ? WHERE id = ?", (rest[0], job_id))
                return self.get(job_id), target
            row = self._transition(job_id, ("awaiting_confirm",), "accepted", helper_id=None,
                                   completion_requested_by=None, completion_requested_at=None)
            return row, target

    def confirm(self, job_id: int, by: int, user_id: int | None = None,
                now: datetime | None = None) -> tuple[sqlite3.Row | None, Award | None]:
        """The poster confirms who really completed it (default: the earliest pending); that raider is rewarded."""
        row, awards = self.confirm_many(job_id, by, user_id, now=now)
        return row, (awards[0][1] if awards else None)

    def confirm_many(self, job_id: int, by: int, user_id: int | None = None, *, now: datetime | None = None
                     ) -> tuple[sqlite3.Row | None, list[tuple[int, Award]]]:
        """Confirm who completed it (default: the earliest pending). Returns (job, [(raider, award)])."""
        now = now or utcnow()
        with transaction(self.conn):
            job = self.get(job_id)
            if job is None or job["status"] != "awaiting_confirm" or by != job["poster_id"]:
                return job, []
            pending = self.pending_completions(job_id)
            helper = user_id if user_id is not None else (pending[0] if pending else job["helper_id"])
            if pending and helper not in pending:
                return job, []
            self.conn.execute("UPDATE jobs SET helper_id = ?, reward_mode = 'single' WHERE id = ?", (helper, job_id))
            job = self.get(job_id)
            awards = [(helper, self._award(job, now))]
            self.conn.execute("UPDATE jobs SET status = 'completed', closed_at = ?, awarded_points = ? WHERE id = ?",
                              (iso(now), awards[0][1].points, job_id))
            return self.get(job_id), awards

    def winners(self, job_id: int) -> list[int]:
        """Rewarded raiders (one for a job; every approved squad member for a challenge), in clear order."""
        return [r["user_id"] for r in self.conn.execute(
            "SELECT user_id FROM job_rewards WHERE job_id = ? ORDER BY place, rowid", (job_id,))]

    def _award(self, job: sqlite3.Row, now: datetime, *, helper: int | None = None, place: int = 1) -> Award:
        helper = helper if helper is not None else job["helper_id"]
        poster = job["poster_id"]
        points = self.tier(job["tier"]).points
        # Guild Master boost: the extra rides on top of the daily cap (the pair cap still stops farming)
        bonus = round(points * float(job["xp_multiplier"] or 1)) - points
        award = self._capped(job, helper, poster, points, bonus, now)
        self.conn.execute("INSERT OR REPLACE INTO job_rewards(job_id, user_id, place, points, created_at)"
                          " VALUES(?,?,?,?,?)", (job["id"], helper, place, award.points, iso(now)))
        return award

    def _capped(self, job: sqlite3.Row, helper: int, poster: int, points: int, bonus: int, now: datetime) -> Award:
        pair = self.conn.execute(
            "SELECT COUNT(*) FROM job_rewards r JOIN jobs j ON j.id = r.job_id WHERE r.points > 0"
            " AND j.poster_id = ? AND r.user_id = ? AND r.created_at > ?",
            (poster, helper, iso(now - timedelta(days=30)))).fetchone()[0]
        if pair >= int(self.rules["pair_cap_per_30_days"]):
            return Award(0, None, "pair_cap")
        today = self.conn.execute(
            "SELECT COALESCE(SUM(points), 0) FROM job_rewards WHERE user_id = ? AND created_at > ?",
            (helper, iso(now - timedelta(days=1)))).fetchone()[0]
        room = int(self.rules["helper_daily_job_point_cap"]) - int(today)
        capped = None
        if room <= 0:
            points, capped = 0, "daily_cap"
        elif points > room:
            points, capped = room, "daily_cap"
        points += bonus
        if points <= 0:
            return Award(0, None, capped)
        change = self.engine.add_points(helper, points, "job", ref=str(job["id"]), actor_id=poster)
        self.engine.touch_activity(helper, now)
        self.engine.touch_activity(poster, now)
        return Award(points, change, capped)

    def flag(self, job_id: int, by: int, now: datetime | None = None) -> bool:
        job = self.get(job_id)
        if job is None or (by != job["poster_id"] and not self.is_attempting(job_id, by)):
            return False
        self.conn.execute("INSERT INTO job_flags(job_id, flagger_id, created_at) VALUES(?,?,?)",
                          (job_id, by, iso(now or utcnow())))
        return True

    # ------------------------------------------------------------ mods
    def remove(self, job_id: int, now: datetime | None = None) -> sqlite3.Row | None:
        """A mod takes down a job that was posted with a heads-up."""
        return self._transition(job_id, ("open", "accepted", "awaiting_confirm"), "removed",
                                closed_at=iso(now or utcnow()))

    def mod_resolve(self, job_id: int, *, award: bool, now: datetime | None = None
                    ) -> tuple[sqlite3.Row | None, Award | None]:
        """Finish a job whose confirmation stalled: award the helper (caps still apply) or close it."""
        now = now or utcnow()
        with transaction(self.conn):
            job = self.get(job_id)
            if job is None or job["status"] not in ("needs_mod", "awaiting_confirm"):
                return None, None
            if not award:
                self.conn.execute("UPDATE jobs SET status = 'closed', closed_at = ?, awarded_points = 0 WHERE id = ?",
                                  (iso(now), job_id))
                return self.get(job_id), Award(0, None, None)
            result = self._award(job, now)
            self.conn.execute("UPDATE jobs SET status = 'completed', closed_at = ?, awarded_points = ? WHERE id = ?",
                              (iso(now), result.points, job_id))
            return self.get(job_id), result

    # ------------------------------------------------------------ vouch / thread cleanup
    def note_vouch(self, voucher_id: int, recipients: list[int], now: datetime | None = None) -> list[sqlite3.Row]:
        """The poster vouched for a raider rewarded for their job: that job's thread can go."""
        if not recipients:
            return []
        marks = ",".join("?" * len(recipients))
        with transaction(self.conn):
            rows = self.conn.execute(
                f"SELECT DISTINCT j.id FROM jobs j JOIN job_rewards r ON r.job_id = j.id WHERE j.status = 'completed'"
                f" AND j.vouched_at IS NULL AND j.poster_id = ? AND r.user_id IN ({marks})",
                (voucher_id, *recipients)).fetchall()
            for r in rows:
                self.conn.execute("UPDATE jobs SET vouched_at = ? WHERE id = ?", (iso(now or utcnow()), r["id"]))
            return [self.get(r["id"]) for r in rows]

    def threads_to_close(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """Threads whose job is over. Completed jobs keep theirs until the poster vouched, or vouch_wait_days."""
        now = now or utcnow()
        cut = iso(now - timedelta(days=float(self.rules.get("vouch_wait_days", 3))))
        return self.conn.execute(
            "SELECT * FROM jobs WHERE thread_id IS NOT NULL AND thread_closed_at IS NULL AND ("
            " status IN ('cancelled', 'expired', 'removed', 'closed', 'rejected')"
            " OR (status = 'completed' AND (vouched_at IS NOT NULL OR closed_at <= ?)))", (cut,)).fetchall()

    def mark_thread_closed(self, job_id: int, now: datetime | None = None) -> None:
        self.conn.execute("UPDATE jobs SET thread_closed_at = ? WHERE id = ?", (iso(now or utcnow()), job_id))

    # ------------------------------------------------------------ nudges / expiry
    def due_checkins(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """Accepted jobs that reached the next check-in day ("did this get done?")."""
        now = now or utcnow()
        days = [int(d) for d in self.rules.get("checkin_after_days", [])]
        out = []
        for job in self.conn.execute("SELECT * FROM jobs WHERE status = 'accepted'").fetchall():
            sent = int(job["reminders_sent"])
            since = parse_iso(job["accepted_at"])
            if since is None or sent >= len(days) or now < since + timedelta(days=days[sent]):
                continue
            # if several are overdue (bot was down), send only the latest one
            while sent + 1 < len(days) and now >= since + timedelta(days=days[sent + 1]):
                sent += 1
            self.conn.execute("UPDATE jobs SET reminders_sent = ? WHERE id = ?", (sent + 1, job["id"]))
            out.append(self.get(job["id"]))
        return out

    def due_escalations(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """An attempter said done, the poster stayed silent: hand it to the mods instead of letting it expire."""
        now = now or utcnow()
        cut = iso(now - timedelta(days=int(self.rules.get("confirm_wait_days", 7))))
        out = []
        for job in self.conn.execute("SELECT id FROM jobs WHERE status = 'awaiting_confirm'"
                                     " AND completion_requested_at <= ?", (cut,)).fetchall():
            row = self._transition(job["id"], ("awaiting_confirm",), "needs_mod")
            if row is not None:
                out.append(row)
        return out

    def expire_due(self, now: datetime | None = None) -> list[sqlite3.Row]:
        now = now or utcnow()
        open_cut = now - timedelta(days=int(self.rules["open_job_expires_days"]))
        acc_cut = now - timedelta(days=int(self.rules["accepted_job_expires_days"]))
        out = []
        # awaiting_confirm never silently expires: due_escalations hands it to the mods instead
        for job in self.conn.execute("SELECT * FROM jobs WHERE status IN ('open','accepted') AND kind = 'job'"
                                     ).fetchall():  # challenges stay open until the Guild Master closes them
            if job["status"] == "open":
                since = parse_iso(job["posted_at"] or job["created_at"])
                due = since is not None and since <= open_cut
            else:
                since = parse_iso(job["accepted_at"])
                due = since is not None and since <= acc_cut
            if due:
                row = self._transition(job["id"], (job["status"],), "expired", closed_at=iso(now))
                if row is not None:
                    out.append(row)
        return out
