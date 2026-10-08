from datetime import timedelta

import pytest

from arcbot.db import utcnow
from arcbot.vouching import VouchService, count_words, parse_vouch

TEXT = "<@2> carried me through Dam Battlegrounds, legend"


@pytest.fixture
def svc(conn, cfg, engine):
    engine.place(1, "scavenger")  # voucher
    engine.place(2, "green_horn")
    engine.place(3, "green_horn")
    return VouchService(conn, cfg, engine)


def test_word_count_ignores_mentions():
    assert count_words("<@123> <@!456> thanks a lot mate") == 4


def test_well_formed(cfg):
    p = parse_vouch(TEXT, [(2, False)], 1, cfg)
    assert p.well_formed and p.recipients == [2]


def test_too_few_words(cfg):
    assert not parse_vouch("<@2> thanks mate", [(2, False)], 1, cfg).well_formed


def test_self_vouch_and_bot_mentions_not_well_formed(cfg):
    assert not parse_vouch("<@1> I am the best raider ever", [(1, False)], 1, cfg).well_formed
    assert not parse_vouch("<@9> great bot, love the timers", [(9, True)], 1, cfg).well_formed
    p = parse_vouch("<@9> <@2> both were super helpful today", [(9, True), (2, False)], 1, cfg)
    assert p.recipients == [2]


def test_max_three_recipients(cfg):
    p = parse_vouch("<@2> <@3> <@4> <@5> awesome squad on that run", [(2, False), (3, False), (4, False), (5, False)],
                    1, cfg)
    assert p.recipients == [2, 3, 4]


def test_counts_and_pair_cooldown(svc, engine):
    out = svc.record(100, 1, [2], TEXT)
    assert out[0].counted and engine.get_user(2)["points"] == 1
    again = svc.record(101, 1, [2], TEXT)
    assert not again[0].counted and again[0].reason == "pair_cooldown"
    later = svc.record(102, 1, [2], TEXT, now=utcnow() + timedelta(hours=25))
    assert later[0].counted  # same pair again the next day


def test_daily_cap(svc, engine):
    for uid in range(10, 20):
        engine.place(uid, "green_horn")
    results = [svc.record(200 + i, 1, [10 + i], TEXT)[0] for i in range(7)]
    assert [r.counted for r in results] == [True] * 5 + [False] * 2
    assert results[-1].reason == "daily_cap"


def test_recipient_can_be_vouched_a_few_times_a_day(svc, engine):
    for uid in range(30, 35):
        engine.place(uid, "scavenger")
    results = [svc.record(600 + i, 30 + i, [2], TEXT)[0] for i in range(5)]
    assert [r.counted for r in results] == [True, True, True, False, False]
    assert results[-1].reason == "recipient_daily_cap"
    assert engine.get_user(2)["points"] == 3


def test_near_miss(cfg):
    from arcbot.vouching import is_near_miss

    assert is_near_miss("<@2> ty!", [(2, False)], 1, cfg)
    assert not is_near_miss(TEXT, [(2, False)], 1, cfg)
    assert not is_near_miss("hello all", [], 1, cfg)
    assert not is_near_miss("<@9> ty", [(9, True)], 1, cfg)


def test_brand_new_member_vouch_does_not_count(svc, engine):
    engine.place(40, "green_horn")  # e.g. a free-weekend account that pressed Skip
    out = svc.record(700, 40, [2], TEXT, voucher_days_in_guild=0.5)
    assert not out[0].counted and out[0].reason == "voucher_too_new"
    assert svc.record(701, 40, [3], TEXT, voucher_days_in_guild=3.5)[0].counted


def test_newcomer_cannot_vouch(svc, engine):
    out = svc.record(300, 50, [2], TEXT)  # user 50 has no record / not placed
    assert not out[0].counted and out[0].reason == "voucher_unplaced"
    assert engine.get_user(2)["points"] == 0


def test_same_message_never_counted_twice(svc, engine):
    svc.record(400, 1, [3], TEXT)
    assert svc.record(400, 1, [3], TEXT) == []
    assert engine.get_user(3)["points"] == 1


def test_vouch_text_truncated(svc, conn):
    svc.record(500, 1, [3], "x" * 1000)
    assert len(conn.execute("SELECT text FROM vouches WHERE message_id=500").fetchone()[0]) == 300
