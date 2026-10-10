from datetime import timedelta

import pytest

from arcbot.challenges import ChallengeService
from arcbot.db import utcnow
from arcbot.jobs import JobService

GOOD = "Clear the frigate and extract with the Emperor core, full squad needed."
V = ("vanguard", 10.0)


@pytest.fixture
def world(conn, cfg, engine):
    for uid in (1, 2, 3, 4, 5, 6, 7):
        engine.place(uid, "vanguard")
    engine.place(9, "green_horn")
    jobs = JobService(conn, cfg, engine)
    job_id, status, _ = jobs.create(1, "Emperor run", GOOD, "vanguard_plus", has_image=False, kind="challenge")
    assert status == "open"
    return ChallengeService(conn, cfg, engine), jobs, job_id


def _squad(ch, job_id, leader, mates, *, t=None):
    sq, reason = ch.form(job_id, leader, rank="vanguard", days_in_guild=10)
    assert reason is None
    invited, skipped, _ = ch.invite(sq["id"], leader, {m: V for m in mates})
    assert invited == list(mates), skipped
    for m in mates:
        assert ch.respond(sq["id"], m, True) is None
    return sq["id"]


def _proofs(ch, sid, users, t0=None):
    t0 = t0 or utcnow()
    sq = None
    for n, u in enumerate(users):
        sq, _, block = ch.submit_proof(sid, u, None, f"hash-{sid}-{u}", now=t0 + timedelta(minutes=n))
        assert block is None
    return sq


def test_full_squad_clear_rewards_everyone_equally(world, engine):
    ch, _, job_id = world
    sid = _squad(ch, job_id, 2, [3, 4])
    sq = _proofs(ch, sid, [2, 3])
    assert sq["status"] == "submitting"  # waiting on 4
    sq = _proofs(ch, sid, [4])
    assert sq["status"] == "in_review" and ch.flags(sid) == []
    before = {u: engine.get_user(u)["points"] for u in (2, 3, 4)}
    sq, awards, _ = ch.decide(sid, True, by=1)
    assert sq["status"] == "approved" and sq["place"] == 1
    assert {a.user_id: a.points for a in awards} == {2: 22, 3: 22, 4: 22}  # 15 + 50% first-clear bonus
    assert all(engine.get_user(u)["points"] == before[u] + 22 for u in (2, 3, 4))
    assert ch.hall(job_id) == [(1, [2, 3, 4])]


def test_later_squads_get_the_plain_reward_and_members_are_rewarded_once(world, engine):
    ch, _, job_id = world
    first = _squad(ch, job_id, 2, [3])
    _proofs(ch, first, [2, 3])
    ch.decide(first, True, by=1)
    second = _squad(ch, job_id, 5, [2, 6])  # 2 comes back to help
    _proofs(ch, second, [5, 2, 6])
    assert any("already rewarded" in f for f in ch.flags(second))
    before = engine.get_user(2)["points"]
    sq, awards, _ = ch.decide(second, True, by=1)
    assert sq["place"] == 2
    assert {a.user_id: (a.points, a.already_rewarded) for a in awards} == {5: (15, False), 2: (0, True), 6: (15, False)}
    assert engine.get_user(2)["points"] == before
    assert ch.hall(job_id) == [(1, [2, 3]), (2, [5, 2, 6])]


def test_guild_master_can_squad_up_and_approve_their_own_clear(world, engine):
    ch, _, job_id = world
    sid = _squad(ch, job_id, 1, [2])  # 1 posted the challenge
    _proofs(ch, sid, [1, 2])
    _, awards, _ = ch.decide(sid, True, by=1)
    assert {a.user_id for a in awards} == {1, 2}


def test_invites_must_be_accepted_and_respect_the_rules(world):
    ch, _, job_id = world
    sid = ch.form(job_id, 2, rank="vanguard", days_in_guild=10)[0]["id"]
    invited, skipped, _ = ch.invite(sid, 2, {2: V, 3: V, 9: ("green_horn", 10.0), 5: ("vanguard", 1.0), 8: (None, 5)})
    assert invited == [3] and skipped == {2: "own", 9: "rank_too_low", 5: "too_new", 8: "not_placed"}
    assert ch.members(sid) == [2]  # 3 hasn't accepted yet
    assert ch.invite(sid, 3, {4: V})[2] == "not_leader"
    assert ch.respond(sid, 4, True) == "not_invited"
    assert ch.respond(sid, 3, True) is None and ch.members(sid) == [2, 3]
    assert ch.invite(sid, 2, {4: V, 5: V})[1] == {5: "full"}  # leader + 2 is a full squad of 3
    # someone in one squad can't join another until it's decided
    other = ch.form(job_id, 6, rank="vanguard", days_in_guild=10)[0]["id"]
    assert ch.invite(other, 6, {3: V})[1] == {3: "in_squad"}
    assert ch.form(job_id, 3, rank="vanguard", days_in_guild=10)[1] == "in_squad"
    assert ch.respond(sid, 2, False) == "leader"
    assert ch.respond(sid, 3, False) is None and ch.members(sid) == [2]  # left
    assert ch.invite(other, 6, {3: V})[0] == [3]


def test_first_screenshot_locks_the_roster(world):
    ch, _, job_id = world
    sid = ch.form(job_id, 2, rank="vanguard", days_in_guild=10)[0]["id"]
    ch.invite(sid, 2, {3: V, 4: V})
    ch.respond(sid, 3, True)  # 4 never answers
    sq, _, _ = ch.submit_proof(sid, 2, None, "a")
    assert sq["status"] == "submitting" and ch.invited(sid) == []  # 4's invite is cancelled
    assert ch.respond(sid, 4, True) == "locked" and ch.invite(sid, 2, {5: V})[2] == "locked"
    assert ch.submit_proof(sid, 9, None, "x")[2] == "not_member"
    sq, _, _ = ch.submit_proof(sid, 3, None, "b")
    assert sq["status"] == "in_review"
    assert ch.submit_proof(sid, 3, None, "c")[2] == "locked"


def test_flags_for_late_or_duplicate_screenshots(world, cfg):
    ch, _, job_id = world
    sid = _squad(ch, job_id, 2, [3, 4])
    t0 = utcnow()
    ch.submit_proof(sid, 2, None, "same", now=t0)
    ch.submit_proof(sid, 3, None, "same", now=t0 + timedelta(minutes=1))
    ch.submit_proof(sid, 4, None, "other", now=t0 + timedelta(minutes=cfg.challenge_proof_window_min + 5))
    flags = ch.flags(sid)
    assert any("minutes apart" in f for f in flags) and any("same image" in f for f in flags)


def test_reject_and_disband(world, engine):
    ch, _, job_id = world
    sid = _squad(ch, job_id, 2, [3])
    _proofs(ch, sid, [2, 3])
    before = engine.get_user(2)["points"]
    sq, awards, _ = ch.decide(sid, False, by=1)
    assert sq["status"] == "rejected" and awards == [] and engine.get_user(2)["points"] == before
    assert ch.decide(sid, True, by=1)[0] is None  # already decided
    again = _squad(ch, job_id, 2, [3])  # free to try again
    assert ch.disband(again, 3)[0] is None  # leader only
    assert ch.disband(again, 2)[0]["status"] == "disbanded"
    assert ch.form(job_id, 3, rank="vanguard", days_in_guild=10)[1] is None


def test_closing_stops_new_squads_but_not_running_ones(world):
    ch, _, job_id = world
    sid = _squad(ch, job_id, 2, [3])
    assert ch.close(job_id, 2) is None  # poster only
    assert ch.close(job_id, 1)["status"] == "closed"
    assert ch.form(job_id, 5, rank="vanguard", days_in_guild=10)[1] == "closed"
    assert _proofs(ch, sid, [2, 3])["status"] == "in_review"
    assert ch.decide(sid, True, by=1)[0]["status"] == "approved"


def test_boost_applies_to_challenge_rewards(conn, cfg, engine):
    for uid in (1, 2):
        engine.place(uid, "vanguard")
    jobs = JobService(conn, cfg, engine)
    job_id, _, _ = jobs.create(1, "Boosted", GOOD, "vanguard_plus", has_image=False, kind="challenge",
                               xp_multiplier=2.0)
    ch = ChallengeService(conn, cfg, engine)
    sid = ch.form(job_id, 2, rank="vanguard", days_in_guild=10)[0]["id"]
    ch.submit_proof(sid, 2, None, "a")
    assert ch.decide(sid, True, by=1)[1][0].points == 45  # 15 x2, +50% first clear
