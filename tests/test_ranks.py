import random

import pytest

from arcbot.ranks import RankTable


def test_rank_from_points(cfg):
    rt = RankTable(cfg)
    assert rt.rank_from_points(0) == "green_horn"
    assert rt.rank_from_points(14) == "green_horn"
    assert rt.rank_from_points(15) == "scavenger"
    assert rt.rank_from_points(50) == "pathfinder"
    assert rt.rank_from_points(150) == "vanguard"
    assert rt.rank_from_points(10**9) == "vanguard"  # never veteran


def test_engine_cases(engine, expected):
    for i, case in enumerate(expected["rank_engine_cases"]):
        uid = 1000 + i
        engine.ensure_user(uid)
        start = case["start_points"]
        if start:
            engine.conn.execute("UPDATE users SET points = ? WHERE discord_id = ?", (start, uid))
        # every case starts as a placed user at the rank their points imply
        engine.place(uid, engine.ranks.rank_from_points(start), seed=False)
        ups = []
        if "assessed" in case:
            vet = case["assessed"] == "veteran"
            engine.place(uid, case["assessed"], grant_veteran=vet and case.get("mod_approved", False))
            assert engine.get_user(uid)["points"] == case["expect_points_after_seed"], case["desc"]
        if "then_add" in case:
            ch = engine.add_points(uid, case["then_add"], "vouch")
            if ch:
                ups.append(ch)
        if "expect_rank" in case:
            assert engine.get_user(uid)["rank_key"] == case["expect_rank"], case["desc"]
        if "expect_rank_up_events" in case:
            assert len(ups) == case["expect_rank_up_events"], case["desc"]


def test_placement_seeds_and_next_vouch_is_not_a_rank_up(engine):
    ch = engine.place(1, "pathfinder")
    assert ch.first_placement and ch.new == "pathfinder"
    assert engine.get_user(1)["points"] == 50
    assert engine.add_points(1, 1, "vouch") is None
    assert engine.get_user(1)["points"] == 51


def test_seeding_is_idempotent(engine):
    engine.place(2, "scavenger")
    engine.place(2, "scavenger")
    engine.seed_from_stats(2, "scavenger", approved=False)
    assert engine.get_user(2)["points"] == 15
    seeds = engine.conn.execute(
        "SELECT COUNT(*) FROM point_events WHERE discord_id=2 AND source='stat_seed'").fetchone()[0]
    assert seeds == 1


def test_veteran_unreachable_by_points(engine):
    engine.place(3, "green_horn")
    for _ in range(50):
        engine.add_points(3, 100, "mod_award")
    assert engine.get_user(3)["rank_key"] == "vanguard"


def test_veteran_placement_requires_grant(engine):
    with pytest.raises(ValueError):
        engine.place(4, "veteran")
    ch = engine.place(4, "veteran", grant_veteran=True)
    assert ch.new == "veteran" and engine.get_user(4)["points"] == 150


def test_monotonic_rank_property(engine):
    rng = random.Random(7)
    engine.place(5, "green_horn")
    last = engine.ranks.order("green_horn")
    for _ in range(500):
        delta = rng.choice([1, 2, 5, 10, -3, -20, 15])
        engine.add_points(5, delta, "test")
        if rng.random() < 0.05:
            engine.place(5, rng.choice(["green_horn", "scavenger", "pathfinder", "vanguard"]))
        now = engine.ranks.order(engine.get_user(5)["rank_key"])
        assert now >= last
        last = now


def test_unplaced_accumulate_then_place_at_max(engine):
    assert engine.add_points(6, 60, "vouch") is None
    assert engine.get_user(6)["rank_key"] is None
    ch = engine.place(6, "scavenger")
    assert ch.new == "pathfinder"  # max(rank_from_points, assessed)


def test_provisional_can_be_lowered_but_real_rank_cannot(engine):
    engine.place(7, "pathfinder", provisional=True)
    assert engine.get_user(7)["provisional"] == 1
    assert engine.get_user(7)["points"] == 0  # no seeding while provisional
    ch = engine.place(7, "scavenger")  # mod "Set rank" lower
    assert ch.new == "scavenger" and engine.get_user(7)["provisional"] == 0
    assert engine.place(7, "green_horn") is None
    assert engine.get_user(7)["rank_key"] == "scavenger"


def test_revert_provisional(engine):
    engine.place(8, "pathfinder", provisional=True)
    ch = engine.revert_provisional(8, None, "green_horn")
    assert ch.new == "green_horn" and engine.get_user(8)["provisional"] == 0
    engine.place(9, "scavenger")
    engine.place(9, "pathfinder", provisional=True)
    ch = engine.revert_provisional(9, "scavenger", "green_horn")
    assert ch.new == "scavenger"


def test_point_events_are_append_only_audit(engine):
    engine.place(10, "green_horn")
    engine.add_points(10, 5, "mod_award", actor_id=99, reason="great help")
    engine.add_points(10, -5, "revoke", ref="2")
    rows = engine.conn.execute("SELECT delta FROM point_events WHERE discord_id=10 ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [5, -5]
    assert engine.get_user(10)["points"] == 0
