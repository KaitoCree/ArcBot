"""Rank engine: one cumulative hidden points pool per user. Ranks only ever go up.

points = stats baseline + earned points. A stats placement lifts the baseline to the assessed rank's floor
(never lowers it); points earned from jobs, vouches, challenges and awards always stay on top.

No Discord here. Every method returns RankChange objects; the caller applies roles and announcements.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .db import iso, transaction, utcnow
from .ranks import VETERAN, RankTable

SEED_SOURCES = ("stat_seed", "import")  # point_events that set the stats baseline; everything else is earned
BACKFILL_SETTING = "points:additive_seed_backfill"


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

    def _baseline_and_earned(self, discord_id: int) -> tuple[int, int]:
        marks = ",".join("?" * len(SEED_SOURCES))
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(CASE WHEN source IN ({marks}) THEN delta END), 0),"
            f" COALESCE(SUM(CASE WHEN source NOT IN ({marks}) THEN delta END), 0)"
            " FROM point_events WHERE discord_id = ?", (*SEED_SOURCES, *SEED_SOURCES, discord_id)).fetchone()
        return int(row[0]), int(row[1])

    def seed_from_stats(self, discord_id: int, assessed: str, *, approved: bool, source: str = "stat_seed") -> None:
        """Lift the stats baseline to the floor of the assessed rank; earned points stay on top of it.

        Never lowers. Veteran needs approval."""
        self.ensure_user(discord_id)
        floor = self.ranks.seed_floor(assessed)
        baseline, _ = self._baseline_and_earned(discord_id)
        gap = max(0, floor - baseline)
        if gap:
            self.conn.execute(
                "INSERT INTO point_events(discord_id, delta, source, ref, created_at) VALUES(?, ?, ?, ?, ?)",
                (discord_id, gap, source, assessed, iso(utcnow())),
            )
            self.conn.execute("UPDATE users SET points = points + ? WHERE discord_id = ?", (gap, discord_id))
        if assessed == VETERAN and approved:
            self.conn.execute("UPDATE users SET veteran_granted = 1 WHERE discord_id = ?", (discord_id,))

    def backfill_additive_seeds(self) -> list[RankChange]:
        """One-time: before seeds were additive, a stats placement absorbed points earned earlier (it raised
        points to the floor instead of adding the floor on top). Credit those points back and re-rank."""
        from .db import get_setting, set_setting

        if get_setting(self.conn, BACKFILL_SETTING):
            return []
        changes: list[RankChange] = []
        with transaction(self.conn):
            marks = ",".join("?" * len(SEED_SOURCES))
            for u in self.conn.execute("SELECT * FROM users").fetchall():
                # every stats placement (a top-up that wrote nothing still left a decided submission) and import
                refs = [r[0] for r in self.conn.execute(
                    f"SELECT ref FROM point_events WHERE discord_id = ? AND source IN ({marks})"
                    " UNION SELECT decided_rank FROM stats_submissions WHERE discord_id = ?"
                    " AND status IN ('auto_placed', 'approved', 'set_rank') AND decided_rank IS NOT NULL",
                    (u["discord_id"], *SEED_SOURCES, u["discord_id"]))]
                floors = [self.ranks.seed_floor(r) for r in refs if r in self.ranks.keys]
                if not floors:
                    continue
                baseline, _ = self._baseline_and_earned(u["discord_id"])
                missing = max(floors) - baseline  # what the old top-up seed held back (earned points it absorbed)
                if missing > 0:
                    top = max((r for r in refs if r in self.ranks.keys), key=self.ranks.order)
                    self.conn.execute(
                        "INSERT INTO point_events(discord_id, delta, source, ref, reason, created_at)"
                        " VALUES(?, ?, 'stat_seed', ?, 'earned points kept on top of stats', ?)",
                        (u["discord_id"], missing, top, iso(utcnow())))
                    self.conn.execute("UPDATE users SET points = points + ? WHERE discord_id = ?",
                                      (missing, u["discord_id"]))
                change = self.recompute(u["discord_id"])
                if change is not None:
                    changes.append(change)
            set_setting(self.conn, BACKFILL_SETTING, iso(utcnow()))
        return changes

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
        - final rank = max(target, rank_from_points(points)); placing at Veteran needs grant_veteran
          (earning it from points does not).
        """
        if target == VETERAN and not grant_veteran and not provisional:
            u0 = self.get_user(discord_id)
            if u0 is None or self.ranks.rank_from_points(u0["points"]) != VETERAN:  # earned on points: fine
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
