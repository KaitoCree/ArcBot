"""Stats -> 0-100 Guild Rating -> assessed rank, plus plausibility flags. Pure functions."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields
from typing import Any

from .config import Config

FIELD_ORDER = ("hours", "knockouts", "squad_revives", "stranger_revives", "quests", "containers", "expeditions")


@dataclass
class Stats:
    hours: float
    knockouts: int
    squad_revives: int
    stranger_revives: int
    quests: int
    containers: int
    expeditions: int

    @property
    def revives(self) -> int:
        return self.squad_revives + self.stranger_revives

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ParseError(ValueError):
    pass


_HMS = re.compile(r"^\s*(\d[\d,. ]*)(?::(\d{1,2}))?(?::(\d{1,2}))?\s*$")


def parse_hours(text: str) -> float:
    """'268:02:47' -> 268.046..., '268' -> 268.0, '268:02' -> 268.033..."""
    m = _HMS.match(text or "")
    if not m:
        raise ParseError(text)
    h = parse_int(m.group(1))
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    if mins >= 60 or secs >= 60:
        raise ParseError(text)
    return h + mins / 60 + secs / 3600


def parse_int(text: str) -> int:
    """'14,099' / '14.099' / '14 099' / '350' -> int. Rejects negatives and decimals like '3.5'."""
    s = (text or "").strip().replace(" ", " ")
    if not s:
        raise ParseError(text)
    if re.fullmatch(r"\d{1,3}([,. ]\d{3})+", s):
        s = re.sub(r"[,. ]", "", s)
    if not s.isdigit():
        raise ParseError(text)
    return int(s)


def format_hours(hours: float) -> str:
    total = int(round(hours * 3600))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def metric_values(stats: Stats) -> dict[str, float]:
    return {
        "hours": stats.hours,
        "revives": float(stats.revives),
        "knockouts": float(stats.knockouts),
        "containers": float(stats.containers),
        "quests": float(stats.quests),
        "expeditions": float(stats.expeditions),
    }


def _curve(value: float, cap: float, curve: str) -> float:
    x = min(max(value, 0.0) / float(cap), 1.0)
    return math.sqrt(x) if curve == "sqrt" else x


def metric_fraction(name: str, m: dict[str, Any], values: dict[str, float]) -> float:
    """0..1 for one metric. A metric with `blend` mixes several stats so different playstyles score alike:
    best_share of the strongest stat plus the rest from the average of all of them."""
    if "blend" in m:
        parts = [_curve(values[stat], spec["cap"], m.get("curve", "linear")) for stat, spec in m["blend"].items()]
        best = float(m.get("best_share", 0.5))
        return best * max(parts) + (1 - best) * sum(parts) / len(parts)
    return _curve(values[name], m["cap"], m["curve"])


def rating_breakdown(stats: Stats, cfg: Config) -> dict[str, float]:
    values = metric_values(stats)
    return {name: float(m["max_points"]) * metric_fraction(name, m, values) for name, m in cfg.metrics.items()}


def rating(stats: Stats, cfg: Config) -> float:
    return round(sum(rating_breakdown(stats, cfg).values()), 2)


def assessed_rank(score: float, cfg: Config) -> str:
    best = cfg.ranks[0].key
    best_min = -1.0
    for key, minimum in cfg.bands.items():
        if score >= minimum and minimum >= best_min:
            best, best_min = key, minimum
    return best


def plausibility_flags(stats: Stats, cfg: Config, *, name_mismatch: bool = False) -> list[str]:
    p = cfg.plausibility
    flags: list[str] = []
    hours = max(stats.hours, 0.0)
    per_hour = hours if hours >= 1 else 1.0
    if hours > p["max_hours"]:
        flags.append("hours_over_max")
    if stats.knockouts / per_hour > p["max_knockouts_per_hour"]:
        flags.append("knockouts_per_hour")
    if stats.containers / per_hour > p["max_containers_per_hour"]:
        flags.append("containers_per_hour")
    if stats.revives / per_hour > p["max_revives_per_hour"]:
        flags.append("revives_per_hour")
    # "Quests Completed" only counts first-time completions, so it can't exceed the game's quest total
    if stats.quests > p["max_quests"]:
        flags.append("quests_over_max")
    if stats.expeditions > p["max_expeditions"]:
        flags.append("expeditions_over_max")
    tiny = p.get("tiny_hours_but_big_numbers") or {}
    if tiny and hours < tiny["hours_below"] and stats.containers > tiny["containers_above"]:
        flags.append("tiny_hours_big_numbers")
    zc = p.get("zero_combat_after_hours")
    if zc and hours >= float(zc) and stats.knockouts == 0 and stats.revives == 0:
        # a known Player Stats bug shows 0 knockouts/revives in the Overview strip while the real numbers exist
        flags.append("zero_combat")
    if name_mismatch and p.get("name_mismatch_is_flag", True):
        flags.append("name_mismatch")
    return flags


FLAG_LABELS = {
    "hours_over_max": "hours above plausible max",
    "knockouts_per_hour": "knockouts per hour very high",
    "containers_per_hour": "containers per hour very high",
    "revives_per_hour": "revives per hour very high",
    "quests_over_max": "quests above plausible max",
    "expeditions_over_max": "expeditions above plausible max",
    "tiny_hours_big_numbers": "tiny hours but big numbers",
    "name_mismatch": "in-game name in screenshot differs from typed name",
    "low_confidence": "some fields were typed in after OCR was unsure",
    "zero_combat": "0 knockouts and 0 revives despite many hours: maybe the Overview-strip stats bug; ask for the real numbers",
    "unsure_confirmed": "member confirmed numbers the reader wasn't sure of",
    "name_from_screenshot": "in-game name was read from the screenshot, not typed",
}


def stats_field_names() -> list[str]:
    return [f.name for f in fields(Stats)]
