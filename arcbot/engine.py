"""Rank engine: one cumulative hidden points pool per user. Ranks only ever go up.

No Discord here. Every method returns RankChange objects; the caller applies roles and announcements.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .db import iso, transaction, utcnow
from .ranks import VETERAN, RankTable


@dataclass(frozen=True)
class RankChange:
    discord_id: int
    old: str | None
    new: str
    first_placement: bool  # True when the user had no real rank before (unplaced or provisional-from-unplaced)
    provisional: bool = False


class RankEngine:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.ranks = RankTable(cfg)

    # ----------------------------------------------------------------- users
    def get_user(self, discord_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()

    def ensure_user(self, discord_id: int, ingame_name: str | None = None) -> sqlite3.Row:
        self.conn.execute(
            "INSERT INTO users(discord_id, ingame_name, created_at) VALUES(?, ?, ?) ON CONFLICT(discord_id) DO NOTHING",
            (discord_id, ingame_name, iso(utcnow())),
        )
        if ingame_name:
            self.conn.execute("UPDATE users SET ingame_name = ? WHERE discord_id = ?", (ingame_name, discord_id))
        row = self.get_user(discord_id)
        assert row is not None
        return row

    def touch_activity(self, discord_id: int, when: datetime | None = None) -> None:
        self.conn.execute(
            "UPDATE users SET last_activity_at = ? WHERE discord_id = ?", (iso(when or utcnow()), discord_id)
        )

    def is_placed(self, discord_id: int) -> bool:
        u = self.get_user(discord_id)
        return bool(u and u["rank_key"])

    # ---------------------------------------------------------------- points
    def add_points(
        self,
        discord_id: int,
        delta: int,
        source: str,
        ref: str | None = None,
        actor_id: int | None = None,
        reason: str | None = None,
    ) -> RankChange | None:
        with transaction(self.conn):
            self.ensure_user(discord_id)
            self.conn.execute(
                "INSERT INTO point_events(discord_id, delta, source, ref, actor_id, reason, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (discord_id, int(delta), source, ref, actor_id, reason, iso(utcnow())),
            )
            self.conn.execute("UPDATE users SET points = points + ? WHERE discord_id = ?", (int(delta), discord_id))
            return self.recompute(discord_id)

    def recompute(self, discord_id: int) -> RankChange | None:
        u = self.get_user(discord_id)
        if u is None or u["rank_key"] is None:
            return None  # unplaced users accumulate points but get no rank until placed
        new = VETERAN if u["veteran_granted"] else self.ranks.rank_from_points(u["points"])
        old = u["rank_key"]
        if self.ranks.order(new) > self.ranks.order(old):
            self.conn.execute("UPDATE users SET rank_key = ? WHERE discord_id = ?", (new, discord_id))
            return RankChange(discord_id, old, new, first_placement=False)
        return None  # never lower a rank, ever

    def seed_from_stats(self, discord_id: int, assessed: str, *, approved: bool, source: str = "stat_seed") -> None:
        """Raise points to the floor of the assessed rank. Never lowers. Veteran needs approval."""
        u = self.ensure_user(discord_id)
        floor = self.ranks.seed_floor(assessed)
        gap = max(0, floor - u["points"])
        if gap:
            self.conn.execute(
                "INSERT INTO point_events(discord_id, delta, source, ref, created_at) VALUES(?, ?, ?, ?, ?)",
                (discord_id, gap, source, assessed, iso(utcnow())),
            )
            self.conn.execute("UPDATE users SET points = points + ? WHERE discord_id = ?", (gap, discord_id))
        if assessed == VETERAN and approved:
            self.conn.execute("UPDATE users SET veteran_granted = 1 WHERE discord_id = ?", (discord_id,))

    # ------------------------------------------------------------- placement
    def place(
        self,
        discord_id: int,
        target: str,
        *,
        provisional: bool = False,
        grant_veteran: bool = False,
        seed: bool = True,
        ingame_name: str | None = None,
        seed_source: str = "stat_seed",
    ) -> RankChange | None:
        """Place (or promote) a user at `target`.

        - provisional=True: hold `target` as a temporary rank while mods look at a claim. No seeding.
        - a provisional rank may later be lowered by a final decision; a real rank never is.
        - final rank = max(target, rank_from_points(points)); Veteran only with grant_veteran.
        """
        if target == VETERAN and not grant_veteran and not provisional:
            raise ValueError("Veteran placement needs grant_veteran=True (mod approval or Guild Master)")
        with transaction(self.conn):
            u = self.ensure_user(discord_id, ingame_name)
            if not provisional and seed:
                self.seed_from_stats(discord_id, target, approved=grant_veteran, source=seed_source)
            if grant_veteran:
                self.conn.execute("UPDATE users SET veteran_granted = 1 WHERE discord_id = ?", (discord_id,))
            u2 = self.get_user(discord_id)
            assert u2 is not None
            if u2["veteran_granted"]:
                new = VETERAN
            elif provisional:
                new = target
            else:
                new = self.ranks.higher(target, self.ranks.rank_from_points(u2["points"]))  # type: ignore[assignment]
            old = u["rank_key"]
            was_provisional = bool(u["provisional"])
            now = iso(utcnow())
            assert new is not None

            if old is None:
                self.conn.execute(
                    "UPDATE users SET rank_key = ?, provisional = ?, placed_at = ? WHERE discord_id = ?",
                    (new, int(provisional), now, discord_id),
                )
                return RankChange(discord_id, None, new, first_placement=True, provisional=provisional)

            if self.ranks.order(new) > self.ranks.order(old) or (was_provisional and new != old):
                self.conn.execute(
                    "UPDATE users SET rank_key = ?, provisional = ? WHERE discord_id = ?",
                    (new, int(provisional), discord_id),
                )
                return RankChange(discord_id, old, new, first_placement=False, provisional=provisional)

            if was_provisional and not provisional:
                self.conn.execute("UPDATE users SET provisional = 0 WHERE discord_id = ?", (discord_id,))
            return None

    def revert_provisional(self, discord_id: int, prev_rank: str | None, fallback: str) -> RankChange | None:
        """Undo a provisional rank after Deny. Users who had no rank go to `fallback` (the Skip rank)."""
        with transaction(self.conn):
            u = self.get_user(discord_id)
            if u is None:
                return None
            target = prev_rank or fallback
            # activity points earned meanwhile still count
            if not u["veteran_granted"]:
                target = self.ranks.higher(target, self.ranks.rank_from_points(u["points"])) or target
            old = u["rank_key"]
            self.conn.execute(
                "UPDATE users SET rank_key = ?, provisional = 0, placed_at = COALESCE(placed_at, ?) WHERE discord_id = ?",
                (target, iso(utcnow()), discord_id),
            )
            if old == target:
                return None
            return RankChange(discord_id, old, target, first_placement=prev_rank is None)

    def set_rank_by_mod(self, discord_id: int, target: str, *, grant_veteran: bool) -> RankChange | None:
        """/set-rank: upward only for real ranks (ranks never go down); Veteran needs Guild Master."""
        return self.place(discord_id, target, grant_veteran=grant_veteran and target == VETERAN)
