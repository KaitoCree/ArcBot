"""Vouch parsing and rules. No Discord here.

A well-formed vouch = at least one non-bot, non-self user mention + `min_words` other words.
Every well-formed vouch gets the same reaction whether or not it counts, so the rules never leak.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config
from .db import iso, transaction, utcnow
from .engine import RankChange, RankEngine

_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")


@dataclass
class ParsedVouch:
    well_formed: bool
    recipients: list[int]
    words: int


@dataclass
class VouchOutcome:
    recipient_id: int
    counted: bool
    reason: str | None
    change: RankChange | None = None


def count_words(content: str) -> int:
    return len(_WORD.findall(_MENTION.sub(" ", content or "")))


def is_near_miss(content: str, mentioned: list[tuple[int, bool]], author_id: int, cfg: Config) -> bool:
    """Mentions a real member but too few words: worth a gentle format hint."""
    has_target = any(not is_bot and uid != author_id for uid, is_bot in mentioned)
    return has_target and count_words(content) < int(cfg.vouch_rules["min_words"])


def parse_vouch(content: str, mentioned: list[tuple[int, bool]], author_id: int, cfg: Config) -> ParsedVouch:
    """`mentioned` = [(user_id, is_bot), ...] in message order."""
    rules = cfg.vouch_rules
    seen: list[int] = []
    for uid, is_bot in mentioned:
        if is_bot or uid == author_id or uid in seen:
            continue
        seen.append(uid)
    words = count_words(content)
    ok = bool(seen) and words >= int(rules["min_words"])
    return ParsedVouch(ok, seen[: int(rules["max_recipients_per_message"])] if ok else [], words)


class VouchService:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, engine: RankEngine):
        self.conn, self.cfg, self.engine = conn, cfg, engine

    def record(self, message_id: int, voucher_id: int, recipients: list[int], text: str,
               voucher_days_in_guild: float | None = None,
               now: datetime | None = None) -> list[VouchOutcome]:
        rules = self.cfg.vouch_rules
        now = now or utcnow()
        out: list[VouchOutcome] = []
        with transaction(self.conn):
            if self.conn.execute("SELECT 1 FROM vouches WHERE message_id = ? LIMIT 1", (message_id,)).fetchone():
                return []  # idempotent: the same message is never counted twice
            voucher_placed = self.engine.is_placed(voucher_id)
            pair_since = iso(now - timedelta(hours=int(rules["pair_cooldown_hours"])))
            day_since = iso(now - timedelta(days=1))
            counted_today = self.conn.execute(
                "SELECT COUNT(*) FROM vouches WHERE voucher_id = ? AND counted = 1 AND created_at > ?",
                (voucher_id, day_since)).fetchone()[0]
            cap = int(rules["max_counted_vouches_per_voucher_per_day"])
            rcap = int(rules.get("max_counted_vouches_per_recipient_per_day", 10**9))
            for rid in recipients:
                reason = None
                min_days = float(rules.get("voucher_min_days_in_guild", 0) or 0)
                if rules.get("voucher_must_be_placed", True) and not voucher_placed:
                    reason = "voucher_unplaced"
                elif min_days and voucher_days_in_guild is not None and voucher_days_in_guild < min_days:
                    reason = "voucher_too_new"  # stops throwaway accounts (e.g. free weekends) farming vouches
                elif self.conn.execute(
                        "SELECT 1 FROM vouches WHERE voucher_id = ? AND recipient_id = ? AND counted = 1"
                        " AND created_at > ? LIMIT 1", (voucher_id, rid, pair_since)).fetchone():
                    reason = "pair_cooldown"
                elif counted_today >= cap:
                    reason = "daily_cap"
                elif self.conn.execute(
                        "SELECT COUNT(*) FROM vouches WHERE recipient_id = ? AND counted = 1 AND created_at > ?",
                        (rid, day_since)).fetchone()[0] >= rcap:
                    reason = "recipient_daily_cap"
                counted = reason is None
                self.conn.execute(
                    "INSERT INTO vouches(message_id, voucher_id, recipient_id, counted, reason, text, created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (message_id, voucher_id, rid, int(counted), reason, (text or "")[:300], iso(now)))
                change = None
                if counted:
                    counted_today += 1
                    change = self.engine.add_points(rid, self.cfg.vouch_points, "vouch", ref=str(message_id),
                                                    actor_id=voucher_id)
                    self.engine.touch_activity(rid, now)
                    self.engine.touch_activity(voucher_id, now)
                out.append(VouchOutcome(rid, counted, reason, change))
        return out
