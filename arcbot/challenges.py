"""Challenge jobs: squads, invites, post-raid proof, Guild Master review, equal squad rewards. No Discord.

A challenge is a job row with kind = 'challenge', posted by the Guild Master and open until they close it.
Squad lifecycle: forming (leader invites; invitees accept or decline) -> submitting (first screenshot locks the
roster; pending invites are cancelled) -> in_review (every accepted member uploaded) -> approved | rejected.
A forming/submitting squad can be disbanded by its leader.

Every member of an approved squad gets the same reward: the tier's points x the boost, plus first_clear_bonus for
the first approved squad. Each raider is rewarded at most once per challenge (job_rewards primary key); job caps
don't apply because the Guild Master checks every clear.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .db import iso, parse_iso, transaction, utcnow
from .engine import RankChange, RankEngine

ACTIVE = ("forming", "submitting", "in_review")


@dataclass
class ChallengeAward:
    user_id: int
    points: int
    change: RankChange | None
    already_rewarded: bool = False


class ChallengeService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, engine: RankEngine):
        self.conn, self.cfg, self.engine = conn, cfg, engine

    # ------------------------------------------------------------ reads
    def job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ? AND kind = 'challenge'", (job_id,)).fetchone()

    def squad(self, squad_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM challenge_squads WHERE id = ?", (squad_id,)).fetchone()

    def members(self, squad_id: int, statuses: tuple[str, ...] = ("accepted",)) -> list[int]:
        marks = ",".join("?" * len(statuses))
        return [r["user_id"] for r in self.conn.execute(
            f"SELECT user_id FROM challenge_members WHERE squad_id = ? AND status IN ({marks})"
            " ORDER BY invited_at, rowid", (squad_id, *statuses))]

    def invited(self, squad_id: int) -> list[int]:
        return self.members(squad_id, ("invited",))

    def active_squad_of(self, job_id: int, user_id: int) -> sqlite3.Row | None:
        """The squad user_id leads, is in or is invited to on this challenge (not yet decided)."""
        return self.conn.execute(
            "SELECT s.* FROM challenge_squads s JOIN challenge_members m ON m.squad_id = s.id"
            " WHERE s.job_id = ? AND m.user_id = ? AND m.status IN ('invited', 'accepted')"
            " AND s.status IN ('forming', 'submitting', 'in_review')", (job_id, user_id)).fetchone()

    def proofs(self, squad_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM challenge_proofs WHERE squad_id = ? ORDER BY submitted_at, rowid",
                                 (squad_id,)).fetchall()

    def in_progress(self, job_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM challenge_squads WHERE job_id = ? AND status IN"
                                 " ('forming', 'submitting', 'in_review')", (job_id,)).fetchone()[0]

    def hall(self, job_id: int) -> list[tuple[int, list[int]]]:
        """Approved squads in clear order: [(place, members), ...]."""
        rows = self.conn.execute("SELECT id, place FROM challenge_squads WHERE job_id = ? AND status = 'approved'"
                                 " ORDER BY place", (job_id,)).fetchall()
        return [(r["place"], self.members(r["id"])) for r in rows]

    def rewarded(self, job_id: int, user_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM job_rewards WHERE job_id = ? AND user_id = ?",
                                 (job_id, user_id)).fetchone() is not None

    # ------------------------------------------------------------ eligibility
    def block_reason(self, job: sqlite3.Row | None, user_id: int, *, rank: str | None,
                     days_in_guild: float | None) -> str | None:
        """Why user_id can't lead or join a squad on this challenge (the Guild Master may take part too)."""
        if job is None or job["status"] != "open":
            return "closed"
        if rank is None:
            return "not_placed"
        if not self.engine.ranks.at_least(rank, self.cfg.job_tiers[job["tier"]].min_rank):
            return "rank_too_low"
        if days_in_guild is not None and days_in_guild < float(self.cfg.job_rules["helper_min_days_in_guild"]):
            return "too_new"
        if self.active_squad_of(job["id"], user_id) is not None:
            return "in_squad"
        return None

    # ------------------------------------------------------------ squad roster
    def form(self, job_id: int, leader_id: int, *, rank: str | None, days_in_guild: float | None,
             now: datetime | None = None) -> tuple[sqlite3.Row | None, str | None]:
        now = now or utcnow()
        with transaction(self.conn):
            job = self.job(job_id)
            reason = self.block_reason(job, leader_id, rank=rank, days_in_guild=days_in_guild)
            if reason:
                return self.active_squad_of(job_id, leader_id) if reason == "in_squad" else None, reason
            cur = self.conn.execute("INSERT INTO challenge_squads(job_id, leader_id, status, created_at)"
                                    " VALUES(?,?,'forming',?)", (job_id, leader_id, iso(now)))
            sid = int(cur.lastrowid)
            self.conn.execute("INSERT INTO challenge_members(squad_id, user_id, status, invited_at, responded_at)"
                              " VALUES(?,?,'accepted',?,?)", (sid, leader_id, iso(now), iso(now)))
            return self.squad(sid), None

    def set_thread(self, squad_id: int, thread_id: int) -> None:
        self.conn.execute("UPDATE challenge_squads SET thread_id = ? WHERE id = ?", (thread_id, squad_id))

    def invite(self, squad_id: int, by: int, users: dict[int, tuple[str | None, float | None]],
               now: datetime | None = None) -> tuple[list[int], dict[int, str], str | None]:
        """Leader invites users = {id: (rank, days_in_guild)}. Returns (invited, {skipped: reason}, block)."""
        now = now or utcnow()
        with transaction(self.conn):
            sq = self.squad(squad_id)
            if sq is None or sq["status"] != "forming":
                return [], {}, "locked"
            if sq["leader_id"] != by:
                return [], {}, "not_leader"
            job = self.job(sq["job_id"])
            room = self.cfg.challenge_max_squad - len(self.members(squad_id, ("invited", "accepted")))
            done: list[int] = []
            skipped: dict[int, str] = {}
            for uid, (rank, days) in users.items():
                reason = "own" if uid == by else self.block_reason(job, uid, rank=rank, days_in_guild=days)
                if reason is None and room <= 0:
                    reason = "full"
                if reason:
                    skipped[uid] = reason
                    continue
                self.conn.execute(
                    "INSERT INTO challenge_members(squad_id, user_id, status, invited_at) VALUES(?,?,'invited',?)"
                    " ON CONFLICT(squad_id, user_id) DO UPDATE SET status = 'invited', invited_at = excluded.invited_at,"
                    " responded_at = NULL", (squad_id, uid, iso(now)))
                done.append(uid)
                room -= 1
            return done, skipped, None

    def respond(self, squad_id: int, user_id: int, accept: bool, now: datetime | None = None) -> str | None:
        """An invitee accepts or declines; an accepted member (not the leader) can leave while forming."""
        now = now or utcnow()
        with transaction(self.conn):
            sq = self.squad(squad_id)
            if sq is None or sq["status"] != "forming":
                return "locked"
            row = self.conn.execute("SELECT status FROM challenge_members WHERE squad_id = ? AND user_id = ?",
                                    (squad_id, user_id)).fetchone()
            if row is None or row["status"] not in ("invited", "accepted"):
                return "not_invited"
            if user_id == sq["leader_id"]:
                return "leader"
            if accept:
                if row["status"] == "accepted":
                    return None
                other = self.active_squad_of(sq["job_id"], user_id)
                if other is not None and other["id"] != squad_id:
                    return "in_squad"
            self.conn.execute("UPDATE challenge_members SET status = ?, responded_at = ? WHERE squad_id = ?"
                              " AND user_id = ?", ("accepted" if accept else "declined", iso(now), squad_id, user_id))
            return None

    def disband(self, squad_id: int, by: int, now: datetime | None = None) -> tuple[sqlite3.Row | None, list[str]]:
        """Leader only, before review. Returns (squad, screenshot paths to delete)."""
        with transaction(self.conn):
            sq = self.squad(squad_id)
            if sq is None or sq["leader_id"] != by or sq["status"] not in ("forming", "submitting"):
                return None, []
            paths = [p["image_path"] for p in self.proofs(squad_id) if p["image_path"]]
            self.conn.execute("UPDATE challenge_squads SET status = 'disbanded', decided_at = ? WHERE id = ?",
                              (iso(now or utcnow()), squad_id))
            self.conn.execute("UPDATE challenge_members SET status = 'cancelled' WHERE squad_id = ?"
                              " AND status = 'invited'", (squad_id,))
            return self.squad(squad_id), paths

    # ------------------------------------------------------------ proof
    def submit_proof(self, squad_id: int, user_id: int, image_path: str | None, sha256: str,
                     now: datetime | None = None) -> tuple[sqlite3.Row | None, str | None, str | None]:
        """Store a member's post-raid screenshot. The first one locks the roster.

        Returns (squad, replaced image path or None, block reason). Squad status 'in_review' = everyone is in."""
        now = now or utcnow()
        with transaction(self.conn):
            sq = self.squad(squad_id)
            if sq is None or sq["status"] not in ("forming", "submitting"):
                return sq, None, "locked"
            if user_id not in self.members(squad_id):
                return sq, None, "not_member"
            old = self.conn.execute("SELECT image_path FROM challenge_proofs WHERE squad_id = ? AND user_id = ?",
                                    (squad_id, user_id)).fetchone()
            self.conn.execute(
                "INSERT INTO challenge_proofs(squad_id, user_id, image_path, sha256, submitted_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(squad_id, user_id) DO UPDATE SET image_path = excluded.image_path,"
                " sha256 = excluded.sha256, submitted_at = excluded.submitted_at",
                (squad_id, user_id, image_path, sha256, iso(now)))
            if sq["status"] == "forming":
                self.conn.execute("UPDATE challenge_squads SET status = 'submitting' WHERE id = ?", (squad_id,))
                self.conn.execute("UPDATE challenge_members SET status = 'cancelled', responded_at = ?"
                                  " WHERE squad_id = ? AND status = 'invited'", (iso(now), squad_id))
            have = {p["user_id"] for p in self.proofs(squad_id)}
            if set(self.members(squad_id)) <= have:
                self.conn.execute("UPDATE challenge_squads SET status = 'in_review', submitted_at = ? WHERE id = ?",
                                  (iso(now), squad_id))
            replaced = old["image_path"] if old and old["image_path"] != image_path else None
            return self.squad(squad_id), replaced, None

    def flags(self, squad_id: int) -> list[str]:
        """Things the Guild Master should look at before approving (never auto-rejects)."""
        sq = self.squad(squad_id)
        proofs = self.proofs(squad_id)
        out: list[str] = []
        times = [parse_iso(p["submitted_at"]) for p in proofs]
        if len(times) > 1:
            spread = (max(times) - min(times)).total_seconds() / 60
            if spread > self.cfg.challenge_proof_window_min:
                out.append(f"screenshots arrived {spread:.0f} minutes apart "
                           f"(window {self.cfg.challenge_proof_window_min:g})")
        seen: dict[str, int] = {}
        for p in proofs:
            if p["sha256"] in seen:
                out.append(f"<@{p['user_id']}> uploaded the same image file as <@{seen[p['sha256']]}>")
            else:
                seen[p["sha256"]] = p["user_id"]
        if sq is not None:
            done = [u for u in self.members(squad_id) if self.rewarded(sq["job_id"], u)]
            if done:
                out.append("already rewarded for this challenge (helping only): "
                           + " ".join(f"<@{u}>" for u in done))
        return out

    def set_review(self, squad_id: int, channel_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE challenge_squads SET review_channel_id = ?, review_message_id = ? WHERE id = ?",
                          (channel_id, message_id, squad_id))

    # ------------------------------------------------------------ decision
    def reward_points(self, job: sqlite3.Row, first: bool) -> int:
        base = self.cfg.job_tiers[job["tier"]].points * float(job["xp_multiplier"] or 1)
        return round(base * (1 + (self.cfg.challenge_first_bonus if first else 0)))

    def decide(self, squad_id: int, approve: bool, by: int, now: datetime | None = None
               ) -> tuple[sqlite3.Row | None, list[ChallengeAward], list[str]]:
        """Guild Master approves (everyone rewarded equally, once per challenge) or rejects a squad in review.

        Returns (squad, awards, screenshot paths to delete); squad None if it wasn't waiting for review."""
        now = now or utcnow()
        with transaction(self.conn):
            sq = self.squad(squad_id)
            if sq is None or sq["status"] != "in_review":
                return None, [], []
            paths = [p["image_path"] for p in self.proofs(squad_id) if p["image_path"]]
            if not approve:
                self.conn.execute("UPDATE challenge_squads SET status = 'rejected', decided_at = ?, decided_by = ?"
                                  " WHERE id = ?", (iso(now), by, squad_id))
                return self.squad(squad_id), [], paths
            job = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (sq["job_id"],)).fetchone()
            place = 1 + self.conn.execute("SELECT COUNT(*) FROM challenge_squads WHERE job_id = ?"
                                          " AND status = 'approved'", (sq["job_id"],)).fetchone()[0]
            self.conn.execute("UPDATE challenge_squads SET status = 'approved', place = ?, decided_at = ?,"
                              " decided_by = ? WHERE id = ?", (place, iso(now), by, squad_id))
            points = self.reward_points(job, place == 1)
            awards: list[ChallengeAward] = []
            for uid in self.members(squad_id):
                if self.rewarded(job["id"], uid):
                    awards.append(ChallengeAward(uid, 0, None, already_rewarded=True))
                    continue
                self.conn.execute("INSERT INTO job_rewards(job_id, user_id, place, points, created_at)"
                                  " VALUES(?,?,?,?,?)", (job["id"], uid, place, points, iso(now)))
                change = self.engine.add_points(uid, points, "challenge", ref=f"{job['id']}:{squad_id}",
                                                actor_id=by)
                self.engine.touch_activity(uid, now)
                awards.append(ChallengeAward(uid, points, change))
            return self.squad(squad_id), awards, paths

    def close(self, job_id: int, by: int, now: datetime | None = None) -> sqlite3.Row | None:
        """The poster closes the challenge: no new squads or invites; squads already raiding can still submit."""
        with transaction(self.conn):
            job = self.job(job_id)
            if job is None or job["poster_id"] != by or job["status"] != "open":
                return None
            self.conn.execute("UPDATE jobs SET status = 'closed', closed_at = ? WHERE id = ?",
                              (iso(now or utcnow()), job_id))
            return self.job(job_id)
