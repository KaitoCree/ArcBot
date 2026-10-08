from datetime import timedelta

import pytest

from arcbot.db import iso, utcnow
from arcbot.intake import IntakeService
from arcbot.scoring import Stats, parse_hours

A = Stats(parse_hours("268:02:47"), 350, 219, 43, 26, 14099, 1)  # 88.7 vanguard
B = Stats(parse_hours("131:18:27"), 31, 44, 10, 31, 6381, 1)  # 42.1 pathfinder
LOW = Stats(20, 10, 5, 1, 3, 600, 0)  # green horn
VET = Stats(320, 450, 300, 50, 40, 16000, 2)  # ~100 veteran


@pytest.fixture
def intake(conn, cfg, engine):
    return IntakeService(conn, cfg, engine)


def submit(intake, uid, stats, kind="onboarding", **kw):
    return intake.submit(uid, kind=kind, source="manual", ingame_name=f"p{uid}", stats=stats, **kw)


def test_pathfinder_auto_places_and_seeds(intake, engine):
    r = submit(intake, 1, B)
    assert r.status == "auto_placed" and r.change.first_placement
    u = engine.get_user(1)
    assert u["rank_key"] == "pathfinder" and u["points"] == 50 and u["provisional"] == 0


def test_vanguard_claim_goes_pending_with_provisional(intake, engine):
    r = submit(intake, 2, A)
    assert r.status == "pending" and r.provisional_rank == "pathfinder"
    u = engine.get_user(2)
    assert u["rank_key"] == "pathfinder" and u["provisional"] == 1 and u["points"] == 0


def test_approve_places_assessed_rank(intake, engine):
    r = submit(intake, 3, A)
    d = intake.decide(r.submission_id, "approve", mod_id=99)
    assert d.ok and d.final_rank == "vanguard"
    u = engine.get_user(3)
    assert u["points"] == 150 and u["provisional"] == 0
    again = intake.decide(r.submission_id, "approve", mod_id=99)
    assert not again.ok and again.reason == "already_decided"


def test_set_rank_may_go_below_provisional(intake, engine):
    r = submit(intake, 4, A)
    d = intake.decide(r.submission_id, "set_rank", mod_id=99, rank="scavenger")
    assert d.ok and d.final_rank == "scavenger" and engine.get_user(4)["points"] == 15


def test_set_rank_never_below_previous_real_rank(intake, engine):
    engine.place(5, "scavenger")
    r = submit(intake, 5, A, kind="promotion")
    d = intake.decide(r.submission_id, "set_rank", mod_id=99, rank="green_horn")
    assert d.final_rank == "scavenger"


def test_deny_reverts_to_skip_rank_and_resets_cooldown(intake, engine):
    r = submit(intake, 6, A)
    d = intake.decide(r.submission_id, "deny", mod_id=99)
    u = engine.get_user(6)
    assert d.status == "denied" and u["rank_key"] == "green_horn" and u["provisional"] == 0
    assert u["last_stats_submission_at"] is None
    assert intake.cooldown_days_left(6) == 0


def test_deny_on_promotion_restores_previous_rank(intake, engine):
    engine.place(7, "scavenger")
    r = submit(intake, 7, A, kind="promotion")
    assert engine.get_user(7)["rank_key"] == "pathfinder"
    intake.decide(r.submission_id, "deny", mod_id=99)
    assert engine.get_user(7)["rank_key"] == "scavenger"


def test_promotion_upward_only(intake, engine):
    engine.place(8, "pathfinder")
    r = submit(intake, 8, LOW, kind="promotion")
    assert r.status == "no_change" and engine.get_user(8)["rank_key"] == "pathfinder"
    assert intake.get(r.submission_id)["status"] == "no_change"  # stored anyway


def test_cooldown(intake, engine):
    engine.place(9, "green_horn")
    assert intake.cooldown_days_left(9) == 0  # Skip users: first submission free
    assert submit(intake, 9, B, kind="promotion").status == "auto_placed"
    assert intake.cooldown_days_left(9) == 30
    later = utcnow() + timedelta(days=29, hours=1)
    assert intake.cooldown_days_left(9, now=later) == 1
    assert intake.cooldown_days_left(9, now=utcnow() + timedelta(days=30, minutes=1)) == 0


def test_no_change_result_only_waits_a_week(intake, engine):
    engine.place(20, "pathfinder")
    r = submit(intake, 20, LOW, kind="promotion")
    assert r.status == "no_change"
    assert intake.cooldown_days_left(20) == 7
    assert intake.cooldown_days_left(20, now=utcnow() + timedelta(days=7, minutes=1)) == 0


def test_notes_only_shown_on_pending(intake):
    r = submit(intake, 21, A, notes=["unsure_confirmed"])
    assert "unsure_confirmed" in intake.get(r.submission_id)["flags"]
    r2 = submit(intake, 22, B, notes=["unsure_confirmed"])
    assert r2.status == "auto_placed"  # notes never force review


def test_flagged_low_claim_gets_provisional_no_higher_than_claim(intake, engine, cfg):
    weird = Stats(2, 100, 0, 0, 0, 5000, 0)  # plausibility flags, assessed green_horn/scavenger
    r = submit(intake, 10, weird)
    assert r.status == "pending" and r.evaluation.flags
    assert engine.ranks.order(engine.get_user(10)["rank_key"]) <= engine.ranks.order(r.evaluation.assessed)


def test_name_mismatch_forces_review(intake):
    r = submit(intake, 11, B, name_mismatch=True)
    assert r.status == "pending" and "name_mismatch" in r.evaluation.flags


def test_veteran_set_rank_needs_guild_master(intake, engine):
    r = submit(intake, 12, A)
    d = intake.decide(r.submission_id, "set_rank", mod_id=99, rank="veteran")
    assert not d.ok and d.reason == "needs_gm"
    d = intake.decide(r.submission_id, "set_rank", mod_id=99, rank="veteran", is_guild_master=True)
    assert d.ok and d.final_rank == "veteran" and engine.get_user(12)["veteran_granted"] == 1


def test_veteran_claim_approved_by_mod(intake, engine):
    r = submit(intake, 13, VET)
    assert r.evaluation.assessed == "veteran" and r.status == "pending"
    d = intake.decide(r.submission_id, "approve", mod_id=99)
    u = engine.get_user(13)
    assert d.final_rank == "veteran" and u["points"] == 150 and u["veteran_granted"] == 1


def test_pending_blocks_and_reminders(intake, conn):
    r = submit(intake, 14, A)
    assert intake.pending_for(14) is not None
    conn.execute("UPDATE stats_submissions SET created_at = ? WHERE id = ?",
                 (iso(utcnow() - timedelta(hours=49)), r.submission_id))
    due = intake.due_reminders()
    assert [s["id"] for s in due] == [r.submission_id]
    intake.mark_reminded(r.submission_id)
    assert intake.due_reminders() == []


def test_image_deleted_after_decision(intake, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    r = submit(intake, 15, A, image_path=str(img))
    assert img.exists()
    intake.decide(r.submission_id, "approve", mod_id=1)
    assert not img.exists()
    img2 = tmp_path / "auto.png"
    img2.write_bytes(b"x")
    submit(intake, 16, B, image_path=str(img2))  # auto-placed: deleted right away
    assert not img2.exists()
