from datetime import timedelta

import pytest

from arcbot.db import iso, utcnow
from arcbot.jobs import JobService, flag_hits

GOOD = "Need a hand clearing the Spaceport quest chain tonight, bring meds and a shield."


@pytest.fixture
def svc(conn, cfg, engine):
    for uid, rank in ((1, "scavenger"), (2, "pathfinder"), (3, "green_horn"), (4, "veteran")):
        engine.place(uid, rank, grant_veteran=rank == "veteran")
    return JobService(conn, cfg, engine)


def test_clean_listing_posts_directly(svc):
    job_id, status, reasons = svc.create(1, "Spaceport quests", GOOD, "anyone", has_image=False)
    assert status == "open" and reasons == []


HOLD = ["real_money_trading", "account_trading_and_sharing", "boosting_and_carrying", "cheating_and_exploits", "scams"]
NOTIFY = ["trade_talk", "low_effort_or_spam_signals"]


def test_flag_categories_are_all_routed(cfg):
    cats = {k for k, v in cfg.flag_list.items() if isinstance(v, dict)}
    assert cats == set(HOLD) | set(NOTIFY)
    assert all(cfg.flag_list[k]["action"] == "hold" for k in HOLD)
    assert all(cfg.flag_list[k]["action"] == "notify" for k in NOTIFY)


@pytest.mark.parametrize("category", HOLD)
def test_hold_categories_wait_for_a_mod(svc, cfg, category):
    term = cfg.flag_list[category]["terms"][0]
    _, status, reasons = svc.create(1, "Help wanted", f"{GOOD} Also {term} if you want.", "anyone", has_image=False)
    assert status == "pending_review"
    assert any(category in r for r in reasons)


@pytest.mark.parametrize("category", NOTIFY)
def test_notify_categories_post_with_heads_up(svc, cfg, category):
    term = cfg.flag_list[category]["terms"][0]
    _, status, reasons = svc.create(1, "Help wanted", f"{GOOD} Also {term} if you want.", "anyone", has_image=False)
    assert status == "open"
    assert any("heads-up" in r and category in r for r in reasons)


def test_hold_beats_notify(svc):
    _, status, _ = svc.create(1, "WTS", GOOD + " wts, paypal only", "anyone", has_image=False)
    assert status == "pending_review"


def test_every_flag_term_is_detected(cfg):
    for cat, body in cfg.flag_list.items():
        if not isinstance(body, dict):
            continue
        for term in body["terms"]:
            assert flag_hits(f"looking for {term} today", cfg.flag_list), (cat, term)


def test_disguised_terms(cfg):
    assert flag_hits("pay me on p ay pal please", cfg.flag_list)
    assert flag_hits("send it via c4shapp", cfg.flag_list)


def test_no_false_positive_on_normal_text(cfg):
    assert flag_hits(GOOD, cfg.flag_list) == []
    assert flag_hits("Europe server squad for Buried City, thousands of containers", cfg.flag_list) == []


@pytest.mark.parametrize("desc,reason", [
    ("short", "too short"),
    (GOOD + " join at https://example.com", "link"),
    (GOOD + " discord.gg/abcd", "link"),
    ("🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥", "emoji only"),
    (GOOD + " this is shit", "profanity"),
])
def test_listing_checks(svc, desc, reason):
    _, status, reasons = svc.create(1, "Help wanted", desc, "anyone", has_image=False)
    assert status == "pending_review" and any(reason in r for r in reasons), reasons


def test_duplicate_and_rate_caps(svc):
    svc.create(1, "Spaceport quests", GOOD, "anyone", has_image=False)
    _, status, reasons = svc.create(1, "Spaceport quests", GOOD, "anyone", has_image=False)
    assert status == "pending_review" and any("duplicate" in r for r in reasons)
    _, status, reasons = svc.create(1, "Another one", GOOD + " again", "anyone", has_image=False)
    assert status == "open"  # third post of the day is still fine
    _, status, reasons = svc.create(1, "Fourth", GOOD + " fourth", "anyone", has_image=False)
    assert status == "pending_review"
    assert any("open jobs" in r for r in reasons) and any("posting rate" in r for r in reasons)


def test_veterans_only_always_previews(svc):
    _, status, reasons = svc.create(1, "Hard raid", GOOD, "veterans_only", has_image=False)
    assert status == "pending_review" and "mod preview" in reasons[0]


def test_accept_gating(svc):
    job_id, _, _ = svc.create(1, "Pathfinder help", GOOD, "pathfinder_plus", has_image=False)
    job = svc.get(job_id)
    assert svc.accept_block_reason(job, 3, helper_rank="green_horn", days_in_guild=10) == "rank_too_low"
    assert svc.accept_block_reason(job, 1, helper_rank="scavenger", days_in_guild=10) == "own"
    assert svc.accept_block_reason(job, 2, helper_rank="pathfinder", days_in_guild=1) == "too_new"
    assert svc.accept_block_reason(job, 9, helper_rank=None, days_in_guild=10) == "not_placed"
    row, reason = svc.accept(job_id, 2, helper_rank="pathfinder", days_in_guild=10)
    assert reason is None and row["status"] == "accepted"
    _, reason = svc.accept(job_id, 4, helper_rank="veteran", days_in_guild=10)
    assert reason == "taken"


def test_veterans_only_needs_veteran(svc):
    job_id, _, _ = svc.create(1, "Hard raid", GOOD, "veterans_only", has_image=False)
    svc.approve(job_id)
    job = svc.get(job_id)
    assert svc.accept_block_reason(job, 2, helper_rank="vanguard", days_in_guild=10) == "rank_too_low"
    assert svc.accept_block_reason(job, 4, helper_rank="veteran", days_in_guild=10) is None


def _complete(svc, poster, helper, tier="pathfinder_plus", title="Job", now=None):
    job_id, status, _ = svc.create(poster, f"{title}", GOOD + f" {title}", tier, has_image=False, now=now)
    if status != "open":
        svc.approve(job_id)
    svc.accept(job_id, helper, helper_rank="vanguard", days_in_guild=10, now=now)
    svc.mark_complete(job_id, poster)
    return job_id, svc.confirm(job_id, helper, now=now)


def test_only_helper_earns_points(svc, engine):
    before_poster = engine.get_user(1)["points"]
    before_helper = engine.get_user(2)["points"]
    job_id, (row, award) = _complete(svc, 1, 2)
    assert row["status"] == "completed" and award.points == 10
    assert engine.get_user(2)["points"] == before_helper + 10
    assert engine.get_user(1)["points"] == before_poster


def test_either_side_marks_done_the_other_confirms(svc, engine):
    for starter, confirmer in ((1, 2), (2, 1)):
        job_id, _, _ = svc.create(1, f"Job {starter}", GOOD + f" {starter}", "anyone", has_image=False)
        svc.accept(job_id, 2, helper_rank="pathfinder", days_in_guild=10)
        assert svc.confirm(job_id, confirmer)[1] is None  # nothing to confirm yet
        assert svc.mark_complete(job_id, 3) is None  # outsiders can't
        assert svc.mark_complete(job_id, starter)["completion_requested_by"] == starter
        assert svc.confirm(job_id, starter)[1] is None  # can't confirm your own "done"
        assert svc.confirm(job_id, 3)[1] is None
        before = engine.get_user(2)["points"]
        row, award = svc.confirm(job_id, confirmer)
        assert row["status"] == "completed" and award.points == 2
        assert engine.get_user(2)["points"] == before + 2  # helper earns, whoever confirmed


def test_checkins_day_3_and_10(svc, conn):
    job_id, _, _ = svc.create(1, "Job", GOOD, "anyone", has_image=False)
    t0 = utcnow()
    svc.accept(job_id, 2, helper_rank="pathfinder", days_in_guild=10, now=t0)
    assert svc.due_checkins(now=t0 + timedelta(days=2)) == []
    assert [r["id"] for r in svc.due_checkins(now=t0 + timedelta(days=3, minutes=1))] == [job_id]
    assert svc.due_checkins(now=t0 + timedelta(days=5)) == []
    assert [r["id"] for r in svc.due_checkins(now=t0 + timedelta(days=10, minutes=1))] == [job_id]
    assert svc.due_checkins(now=t0 + timedelta(days=12)) == []


def test_overdue_checkins_collapse_to_one(svc):
    job_id, _, _ = svc.create(1, "Job", GOOD, "anyone", has_image=False)
    t0 = utcnow()
    svc.accept(job_id, 2, helper_rank="pathfinder", days_in_guild=10, now=t0)
    assert len(svc.due_checkins(now=t0 + timedelta(days=11))) == 1
    assert svc.due_checkins(now=t0 + timedelta(days=12)) == []


def test_silent_confirmation_goes_to_mods_not_expiry(svc, engine):
    job_id, _, _ = svc.create(1, "Job", GOOD, "pathfinder_plus", has_image=False)
    t0 = utcnow()
    svc.accept(job_id, 2, helper_rank="pathfinder", days_in_guild=10, now=t0)
    svc.mark_complete(job_id, 2, now=t0 + timedelta(days=1))
    assert svc.expire_due(now=t0 + timedelta(days=30)) == []  # never silently expires
    assert svc.due_escalations(now=t0 + timedelta(days=7)) == []
    rows = svc.due_escalations(now=t0 + timedelta(days=8, minutes=1))
    assert [r["status"] for r in rows] == ["needs_mod"]
    before = engine.get_user(2)["points"]
    row, award = svc.mod_resolve(job_id, award=True)
    assert row["status"] == "completed" and award.points == 10
    assert engine.get_user(2)["points"] == before + 10
    assert svc.mod_resolve(job_id, award=True) == (None, None)  # idempotent


def test_mod_close_without_award(svc, engine):
    job_id, _, _ = svc.create(1, "Job", GOOD, "anyone", has_image=False)
    svc.accept(job_id, 2, helper_rank="pathfinder", days_in_guild=10)
    svc.mark_complete(job_id, 1)
    before = engine.get_user(2)["points"]
    row, award = svc.mod_resolve(job_id, award=False)
    assert row["status"] == "closed" and award.points == 0 and engine.get_user(2)["points"] == before


def test_mod_can_take_down_a_posted_job(svc):
    job_id, status, _ = svc.create(1, "WTS", GOOD + " wts", "anyone", has_image=False)
    assert status == "open"
    assert svc.remove(job_id)["status"] == "removed"
    assert svc.remove(job_id) is None


def test_pair_cap(svc, conn):
    awards = []
    for i in range(4):
        conn.execute("DELETE FROM jobs WHERE status != 'completed'")
        awards.append(_complete(svc, 1, 2, title=f"Run {i}", now=utcnow() + timedelta(hours=i * 25))[1][1])
    assert [a.points for a in awards] == [10, 10, 10, 0] and awards[-1].capped == "pair_cap"


def test_daily_cap(svc, engine, conn):
    for p in range(20, 26):
        engine.place(p, "scavenger")
    pts = []
    for p in range(20, 25):
        pts.append(_complete(svc, p, 2, tier="vanguard_plus", title=f"from {p}")[1][1].points)
    assert pts == [15, 15, 10, 0, 0]  # third award trimmed to the 40/day cap, then 0


def test_expiry(svc, conn):
    job_id, _, _ = svc.create(1, "Old job", GOOD, "anyone", has_image=False)
    conn.execute("UPDATE jobs SET posted_at = ? WHERE id = ?", (iso(utcnow() - timedelta(days=8)), job_id))
    acc_id, _, _ = svc.create(3, "Accepted job", GOOD + " x", "anyone", has_image=False)
    svc.accept(acc_id, 2, helper_rank="pathfinder", days_in_guild=10, now=utcnow() - timedelta(days=15))
    expired = {r["id"] for r in svc.expire_due()}
    assert expired == {job_id, acc_id}
    assert svc.get(job_id)["status"] == "expired" and svc.get(job_id)["awarded_points"] is None


def test_cancel_only_by_poster(svc):
    job_id, _, _ = svc.create(1, "Job", GOOD, "anyone", has_image=False)
    assert svc.cancel(job_id, 2) is None
    assert svc.cancel(job_id, 1)["status"] == "cancelled"
