"""Load and validate config/arcbot.yaml. Fail fast with readable errors."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Rank:
    key: str
    role: str
    threshold: int | None
    seed_threshold: int | None


@dataclass(frozen=True)
class JobTier:
    key: str
    label: str
    min_rank: str
    points: int
    mod_preview: bool
    who: str = ""


@dataclass(frozen=True)
class XpBoost:
    label: str
    multiplier: float


class Config:
    """Thin validated wrapper around the YAML dict. `raw` stays available for rarely used values."""

    def __init__(self, raw: dict[str, Any], root: Path = ROOT):
        self.raw = raw
        self.root = root
        errors: list[str] = []
        try:
            self._load(errors)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"missing or malformed value: {exc!r}")
        if errors:
            raise ConfigError("config/arcbot.yaml has problems:\n  - " + "\n  - ".join(errors))

    # ------------------------------------------------------------------ load
    def _load(self, errors: list[str]) -> None:
        r = self.raw
        self.guild_name: str = r["guild"]["name"]

        roles = r["roles"]
        self.unplaced_role: str = roles["unplaced"]
        self.ranks: list[Rank] = [
            Rank(x["key"], x["role"], x.get("threshold"), x.get("seed_threshold")) for x in roles["ranks"]
        ]
        self.mod_roles: list[str] = list(roles["mods"])
        self.guild_master_role: str = roles["guild_master"]
        keys = [k.key for k in self.ranks]
        if len(set(keys)) != len(keys):
            errors.append("roles.ranks has duplicate keys")
        if not self.ranks or self.ranks[-1].key != "veteran":
            errors.append("roles.ranks must end with the 'veteran' rank")
        else:
            vet = self.ranks[-1]
            if vet.threshold is not None:
                errors.append("veteran threshold must be null (Veteran is never earned from points)")
            if vet.seed_threshold is None:
                errors.append("veteran needs seed_threshold")
        earnable = self.ranks[:-1]
        if not earnable or earnable[0].threshold != 0:
            errors.append("the lowest rank must have threshold 0")
        prev = -1
        for rk in earnable:
            if rk.threshold is None or rk.threshold <= prev:
                errors.append(f"rank thresholds must strictly increase (problem at {rk.key})")
            prev = rk.threshold if rk.threshold is not None else prev

        self.channels: dict[str, str] = dict(r["channels"])
        for need in ("apply", "rank_promotion", "mod_review", "vouch", "job_board", "event_timers",
                     "rank_up_announcements"):
            if need not in self.channels:
                errors.append(f"channels.{need} is missing")

        ob = r["onboarding"]
        self.skip_rank: str = ob["skip_rank"]
        self.announce_placements: bool = bool(ob["announce_placements"])
        self.announce_rank_ups: bool = bool(ob["announce_rank_ups"])

        rv = r["review"]
        self.flag_assessed_rank_above: str = rv["flag_assessed_rank_above"]
        self.provisional_rank: str = rv["provisional_rank_while_pending"]
        self.reminder_after_hours: float = float(rv["reminder_after_hours"])
        self.deny_resets_cooldown: bool = bool(rv["deny_resets_cooldown"])

        pr = r["promotion"]
        self.promotion_cooldown_days: int = int(pr["cooldown_days"])
        self.no_change_cooldown_days: int = int(pr.get("no_change_cooldown_days", pr["cooldown_days"]))

        sr = r["stats_rating"]
        self.metrics: dict[str, dict[str, Any]] = dict(sr["metrics"])
        known = {"hours", "revives", "knockouts", "containers", "quests", "expeditions"}
        for name, m in self.metrics.items():
            if m.get("curve") not in ("linear", "sqrt"):
                errors.append(f"stats_rating.metrics.{name}.curve must be linear or sqrt")
            if "blend" in m:
                if not m["blend"] or set(m["blend"]) - known:
                    errors.append(f"stats_rating.metrics.{name}.blend must list stats from {sorted(known)}")
                for stat, spec in (m["blend"] or {}).items():
                    if float((spec or {}).get("cap", 0)) <= 0:
                        errors.append(f"stats_rating.metrics.{name}.blend.{stat}.cap must be > 0")
                if not 0 <= float(m.get("best_share", 0.5)) <= 1:
                    errors.append(f"stats_rating.metrics.{name}.best_share must be 0..1")
            else:
                if name not in known:
                    errors.append(f"stats_rating.metrics.{name} is not a stat (use a blend for combined metrics)")
                if float(m.get("cap", 0)) <= 0:
                    errors.append(f"stats_rating.metrics.{name}.cap must be > 0")
        total = sum(float(m.get("max_points", 0)) for m in self.metrics.values())
        if abs(total - 100) > 0.01:
            errors.append(f"stats_rating max_points must add up to 100 (now {total:g})")
        self.bands: dict[str, float] = {k: float(v) for k, v in sr["bands"].items()}
        if set(self.bands) != set(keys):
            errors.append("stats_rating.bands must list exactly the rank keys")

        self.plausibility: dict[str, Any] = dict(r["plausibility"])

        pts = r["points"]
        self.vouch_points: int = int(pts["vouch"])
        self.vouch_rules: dict[str, Any] = dict(pts["vouch_rules"])
        if "pair_cooldown_hours" not in self.vouch_rules:
            self.vouch_rules["pair_cooldown_hours"] = int(self.vouch_rules.get("pair_cooldown_days", 14)) * 24
        self.mod_award_min: int = int(pts["mod_award"]["min"])
        self.mod_award_max: int = int(pts["mod_award"]["max"])
        self.job_tiers: dict[str, JobTier] = {
            k: JobTier(k, v["label"], v["min_rank"], int(v["points"]), bool(v["mod_preview_before_posting"]),
                       v.get("who") or v["label"])
            for k, v in pts["job_tiers"].items()
        }
        if len(self.job_tiers) > 25:
            errors.append("points.job_tiers can have at most 25 entries (Discord select limit)")
        self.job_rules: dict[str, Any] = dict(pts["job_rules"])
        self.job_xp_boosts: list[XpBoost] = [
            XpBoost(str(b["label"]), float(b["multiplier"])) for b in pts.get("job_xp_boosts") or []]
        if not self.job_xp_boosts:
            self.job_xp_boosts = [XpBoost("Standard reward", 1.0)]
        if self.job_xp_boosts[0].multiplier != 1:
            errors.append("points.job_xp_boosts must start with the multiplier 1 entry (the default)")
        if any(b.multiplier < 1 or b.multiplier > 10 for b in self.job_xp_boosts):
            errors.append("points.job_xp_boosts multipliers must be between 1 and 10")
        if len(self.job_xp_boosts) > 25:
            errors.append("points.job_xp_boosts can have at most 25 entries (Discord select limit)")
        self.squad_max_size: int = int((pts.get("job_squads") or {}).get("max_size", 3))
        if not 2 <= self.squad_max_size <= 26:
            errors.append("points.job_squads.max_size must be 2..26")
        if int(self.job_rules.get("max_attempters_per_job", 8)) < 1:
            errors.append("points.job_rules.max_attempters_per_job must be >= 1")

        jc = r["job_listing_checks"]
        self.job_checks: dict[str, Any] = dict(jc)
        flag_path = self.root / jc["flag_list_file"]
        try:
            self.flag_list: dict[str, Any] = json.loads(flag_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"cannot read flag list {flag_path}: {exc}")
            self.flag_list = {}

        self.timers: dict[str, Any] = dict(r["timers"])
        self.ocr: dict[str, Any] = dict(r["ocr"])
        self.backups: dict[str, Any] = dict(r["backups"])
        self.keepalive: dict[str, Any] = dict(r["keepalive"])

        # cross-references
        for label, key in (("onboarding.skip_rank", self.skip_rank),
                           ("review.flag_assessed_rank_above", self.flag_assessed_rank_above),
                           ("review.provisional_rank_while_pending", self.provisional_rank)):
            if key not in keys:
                errors.append(f"{label} '{key}' is not a rank key")
        for t in self.job_tiers.values():
            if t.min_rank not in keys:
                errors.append(f"job tier {t.key} min_rank '{t.min_rank}' is not a rank key")
        if self.mod_award_min < 1 or self.mod_award_max < self.mod_award_min:
            errors.append("points.mod_award min/max are invalid")
        if int(self.ocr.get("concurrency", 1)) < 1:
            errors.append("ocr.concurrency must be >= 1")

    # --------------------------------------------------------------- helpers
    @property
    def rank_keys(self) -> list[str]:
        return [r.key for r in self.ranks]

    def rank(self, key: str) -> Rank:
        for r in self.ranks:
            if r.key == key:
                return r
        raise KeyError(key)

    def rank_by_role(self, role_name: str) -> Rank | None:
        for r in self.ranks:
            if r.role == role_name:
                return r
        return None


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("ARCBOT_CONFIG") or ROOT / "config" / "arcbot.yaml")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must be a mapping at the top level")
    return Config(raw, root=p.resolve().parent.parent)
