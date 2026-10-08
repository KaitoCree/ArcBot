import pytest

from arcbot.scoring import (ParseError, Stats, assessed_rank, format_hours, parse_hours, parse_int,
                            plausibility_flags, rating)


def stats_of(p):
    return Stats(
        hours=parse_hours(p["hours_hms"]), knockouts=p["knockouts"], squad_revives=p["squadmate_revives"],
        stranger_revives=p["stranger_revives"], quests=p["quests"], containers=p["containers"],
        expeditions=p["expeditions"],
    )


def test_golden_players(cfg, expected):
    tol = expected["rating_tolerance"]
    for p in expected["players"]:
        s = stats_of(p)
        r = rating(s, cfg)
        assert abs(r - p["expected_rating"]) <= tol, (p["id"], r)
        rank = assessed_rank(r, cfg)
        assert rank == p["expected_assessed_rank"]
        above = cfg.rank_keys.index(rank) > cfg.rank_keys.index(cfg.flag_assessed_rank_above)
        flagged = above or bool(plausibility_flags(s, cfg))
        assert flagged == p["expected_flagged_for_mods"], p["id"]


def test_reference_player_ratings(cfg, expected):
    a, b = expected["players"][:2]
    assert round(rating(stats_of(a), cfg), 1) == 86.3
    assert round(rating(stats_of(b), cfg), 1) == 50.2


# Illustrative archetypes at the same 150 hours (not real data): the scoring must not favour one playstyle.
STYLES = {
    "squad medic": Stats(150, 40, 180, 30, 35, 8000, 0),
    "pvp hunter": Stats(150, 450, 40, 5, 20, 6000, 0),
    "solo pve looter": Stats(150, 15, 0, 25, 38, 11000, 0),
    "solo pvp": Stats(150, 300, 0, 3, 15, 7000, 0),
    "stranger helper": Stats(150, 10, 20, 150, 30, 8000, 0),
    "average squad": Stats(150, 120, 80, 15, 30, 9000, 0),
}


def test_playstyles_score_close_at_equal_hours(cfg):
    scores = {k: rating(s, cfg) for k, s in STYLES.items()}
    assert max(scores.values()) - min(scores.values()) <= 7.0, scores
    assert {assessed_rank(v, cfg) for v in scores.values()} == {"pathfinder"}, scores


def test_expeditions_rewarded_but_small(cfg):
    from dataclasses import replace

    base = STYLES["average squad"]
    r0, r1, r5 = (rating(replace(base, expeditions=n), cfg) for n in (0, 1, 5))
    assert 0 < r1 - r0 <= 1.5
    assert r5 - r0 <= 2.0


def test_more_hours_never_hurts(cfg):
    from dataclasses import replace

    s = STYLES["solo pve looter"]
    assert rating(replace(s, hours=300), cfg) > rating(s, cfg) > rating(replace(s, hours=50), cfg)


def test_quest_limit_ignores_expeditions(cfg):
    from dataclasses import replace

    base = STYLES["average squad"]
    # 100 quests exist; first-time completions only, so expeditions never raise the count or the limit
    assert "quests_over_max" in plausibility_flags(replace(base, quests=120, expeditions=5), cfg)
    assert "quests_over_max" not in plausibility_flags(replace(base, quests=100, expeditions=0), cfg)


def test_zero_combat_after_many_hours_goes_to_a_mod(cfg):
    from dataclasses import replace

    base = STYLES["average squad"]
    bugged = replace(base, knockouts=0, squad_revives=0, stranger_revives=0)
    assert "zero_combat" in plausibility_flags(bugged, cfg)
    assert "zero_combat" not in plausibility_flags(replace(bugged, hours=20), cfg)  # new players: normal
    assert "zero_combat" not in plausibility_flags(replace(bugged, knockouts=1), cfg)


def test_weights_add_up_to_100(cfg):
    assert sum(float(m["max_points"]) for m in cfg.metrics.values()) == pytest.approx(100)


@pytest.mark.parametrize("text,hours", [("268:02:47", 268 + 2 / 60 + 47 / 3600), ("268", 268.0),
                                        ("1,234:00:00", 1234.0), (" 12:30 ", 12.5)])
def test_parse_hours(text, hours):
    assert parse_hours(text) == pytest.approx(hours)


@pytest.mark.parametrize("bad", ["", "abc", "12:61:00", "-5", "12:00:75"])
def test_parse_hours_rejects(bad):
    with pytest.raises(ParseError):
        parse_hours(bad)


@pytest.mark.parametrize("text,val", [("350", 350), ("14,099", 14099), ("14.099", 14099), ("14 099", 14099),
                                      ("1,000,000", 1000000)])
def test_parse_int(text, val):
    assert parse_int(text) == val


@pytest.mark.parametrize("bad", ["3.5", "-1", "1,23", "", "12a"])
def test_parse_int_rejects(bad):
    with pytest.raises(ParseError):
        parse_int(bad)


def test_format_hours_roundtrip():
    assert format_hours(parse_hours("268:02:47")) == "268:02:47"


def test_rating_is_capped_and_bounded(cfg):
    huge = Stats(10_000, 10**6, 10**6, 10**6, 10**6, 10**8, 10**3)
    assert rating(huge, cfg) == pytest.approx(100.0)
    zero = Stats(0, 0, 0, 0, 0, 0, 0)
    assert rating(zero, cfg) == 0
    assert assessed_rank(0, cfg) == "green_horn"
    assert assessed_rank(100, cfg) == "veteran"


def test_band_edges(cfg):
    assert assessed_rank(19.99, cfg) == "green_horn"
    assert assessed_rank(20, cfg) == "scavenger"
    assert assessed_rank(59.99, cfg) == "pathfinder"
    assert assessed_rank(60, cfg) == "vanguard"
    assert assessed_rank(89.99, cfg) == "vanguard"


def test_plausibility_flags(cfg):
    s = Stats(2, 100, 0, 0, 0, 5000, 0)
    flags = plausibility_flags(s, cfg, name_mismatch=True)
    assert {"knockouts_per_hour", "containers_per_hour", "tiny_hours_big_numbers", "name_mismatch"} <= set(flags)
    ok = Stats(100, 300, 100, 20, 20, 5000, 1)
    assert plausibility_flags(ok, cfg) == []
