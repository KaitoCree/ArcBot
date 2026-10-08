"""Rank table: ordering, thresholds and the points -> rank mapping. Pure, no I/O."""
from __future__ import annotations

from .config import Config

VETERAN = "veteran"


class RankTable:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._order = {r.key: i for i, r in enumerate(cfg.ranks)}

    @property
    def keys(self) -> list[str]:
        return list(self._order)

    def order(self, key: str | None) -> int:
        """-1 for unplaced (None)."""
        if key is None:
            return -1
        return self._order[key]

    def role_name(self, key: str) -> str:
        return self.cfg.rank(key).role

    def all_role_names(self) -> list[str]:
        return [r.role for r in self.cfg.ranks]

    def threshold(self, key: str) -> int:
        r = self.cfg.rank(key)
        if r.threshold is None:
            assert r.seed_threshold is not None
            return r.seed_threshold
        return r.threshold

    def seed_floor(self, key: str) -> int:
        """Points a user is seeded to when placed at `key` by stats."""
        return self.threshold(key)

    def rank_from_points(self, points: int) -> str:
        """Highest earnable rank whose threshold <= points. Never returns veteran."""
        best = self.cfg.ranks[0].key
        for r in self.cfg.ranks:
            if r.threshold is None:
                continue
            if r.threshold <= points:
                best = r.key
        return best

    def higher(self, a: str | None, b: str | None) -> str | None:
        return a if self.order(a) >= self.order(b) else b

    def at_least(self, key: str | None, minimum: str) -> bool:
        return key is not None and self.order(key) >= self.order(minimum)
