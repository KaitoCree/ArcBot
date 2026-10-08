"""Members who already hold a rank role get a record on first contact (no dead end to #apply)."""
from unittest.mock import MagicMock

import discord

from arcbot.app import App
from arcbot.db import connect


def _app(cfg, copy):
    app = App(cfg, copy, connect(":memory:"), dry_run=True, guild_id=None)
    for rk in cfg.ranks:
        app.resolved.roles[rk.role] = MagicMock(name=rk.role)
    return app


def _member(uid, roles, bot=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.roles, m.bot = uid, roles, bot
    return m


def test_rank_role_without_record_is_adopted(cfg, copy):
    app = _app(cfg, copy)
    pf = app.rank_role("pathfinder")
    assert app.adopt_from_roles(_member(1, [pf])) is True
    u = app.engine.get_user(1)
    assert u["rank_key"] == "pathfinder" and u["points"] == 50
    assert app.adopt_from_roles(_member(1, [pf])) is False  # only once


def test_highest_role_wins_and_veteran_is_granted(cfg, copy):
    app = _app(cfg, copy)
    assert app.adopt_from_roles(_member(2, [app.rank_role("scavenger"), app.rank_role("veteran")]))
    u = app.engine.get_user(2)
    assert u["rank_key"] == "veteran" and u["veteran_granted"] == 1


def test_no_rank_role_or_bot_is_left_alone(cfg, copy):
    app = _app(cfg, copy)
    assert app.adopt_from_roles(_member(3, [])) is False
    assert app.adopt_from_roles(_member(4, [app.rank_role("vanguard")], bot=True)) is False
    assert app.engine.get_user(3) is None
