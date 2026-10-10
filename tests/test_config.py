import copy as stdcopy

import pytest

from arcbot.config import Config, ConfigError
from arcbot.db import connect, schema_version


def test_config_loads(cfg):
    assert cfg.rank_keys == ["green_horn", "scavenger", "pathfinder", "vanguard", "veteran"]
    assert cfg.job_tiers["veterans_only"].mod_preview is True
    assert cfg.flag_list["real_money_trading"]["terms"]


def test_bad_config_is_readable(cfg):
    raw = stdcopy.deepcopy(cfg.raw)
    raw["roles"]["ranks"][1]["threshold"] = 0
    raw["onboarding"]["skip_rank"] = "nope"
    with pytest.raises(ConfigError) as exc:
        Config(raw, root=cfg.root)
    msg = str(exc.value)
    assert "strictly increase" in msg and "skip_rank" in msg


def test_scoring_weights_validated(cfg):
    raw = stdcopy.deepcopy(cfg.raw)
    raw["stats_rating"]["metrics"]["hours"]["max_points"] = 50
    raw["stats_rating"]["metrics"]["playstyle"]["blend"]["kills"] = {"cap": 10}
    with pytest.raises(ConfigError) as exc:
        Config(raw, root=cfg.root)
    assert "add up to 100" in str(exc.value) and "blend" in str(exc.value)


def test_veteran_threshold_must_be_null(cfg):
    raw = stdcopy.deepcopy(cfg.raw)
    raw["roles"]["ranks"][-1]["threshold"] = 300
    with pytest.raises(ConfigError, match="never earned"):
        Config(raw, root=cfg.root)


def test_migrations_apply_and_are_idempotent(tmp_path):
    db = tmp_path / "x.db"
    c = connect(db)
    v = schema_version(c)
    assert v >= 1
    c.close()
    c = connect(db)
    assert schema_version(c) == v
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_job_attempts_migration_keeps_existing_helpers():
    import sqlite3

    from arcbot.db import MIGRATIONS_DIR, _split_sql, migrate

    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    for name in ("001_initial.sql", "002_cooldowns_and_job_completion.sql"):
        for stmt in _split_sql((MIGRATIONS_DIR / name).read_text(encoding="utf-8")):
            c.execute(stmt)
    c.execute("PRAGMA user_version = 2")
    ins = ("INSERT INTO jobs(id, poster_id, title, description, tier, status, helper_id, created_at, accepted_at,"
           " closed_at, thread_id) VALUES(?,1,'t','d','anyone',?,?,'2026-01-01','2026-01-02',?,?)")
    c.execute(ins, (1, "accepted", 2, None, 50))
    c.execute(ins, (2, "awaiting_confirm", 3, None, 51))
    c.execute(ins, (3, "completed", 4, "2026-01-03", 52))
    migrate(c)
    rows = {r["id"]: r for r in c.execute("SELECT * FROM jobs")}
    assert rows[1]["helper_id"] is None and rows[2]["helper_id"] == 3  # only a pending claim keeps its claimer
    assert [tuple(r) for r in c.execute("SELECT job_id, user_id FROM job_attempts ORDER BY job_id")] == [(1, 2), (2, 3)]
    assert rows[3]["thread_closed_at"] and rows[1]["thread_closed_at"] is None  # old finished threads are left alone
    assert rows[1]["xp_multiplier"] == 1


def test_xp_boosts_validated(cfg):
    raw = stdcopy.deepcopy(cfg.raw)
    raw["points"]["job_xp_boosts"] = [{"label": "Half", "multiplier": 0.5}]
    with pytest.raises(ConfigError, match="job_xp_boosts"):
        Config(raw, root=cfg.root)


def test_env_example_has_no_real_values():
    """The template is shared; real secrets belong only in .env (gitignored)."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("DISCORD_TOKEN=", "GUILD_ID=")):
            assert line.split("=", 1)[1].strip() == "", f"{line.split('=')[0]} must stay empty in .env.example"
