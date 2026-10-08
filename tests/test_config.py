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


def test_env_example_has_no_real_values():
    """The template is shared; real secrets belong only in .env (gitignored)."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("DISCORD_TOKEN=", "GUILD_ID=")):
            assert line.split("=", 1)[1].strip() == "", f"{line.split('=')[0]} must stay empty in .env.example"
