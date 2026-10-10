"""Points are hidden from players everywhere. This must stay green at all times.

1. Every copy.yaml string, rendered, is leak-free and has no point-like placeholder.
2. The Gateway guard blocks leaking text.
3. Real flows (skip, intake auto/pending/approve, promotion, vouch rank-up, full job cycle, mod award)
   run against fakes; every player-visible output is scanned.
4. Static: point values are only formatted in mod-only code.
"""
import re
from pathlib import Path

import discord
import pytest

from arcbot.app import App
from arcbot.bot import ArcBot
from arcbot.db import connect
from arcbot.discord_io import PointLeakError, leaks_points
from arcbot.scoring import Stats, parse_hours
from tests.fakes import FakeChannel, FakeGuild, FakeInteraction, FakeMember, FakeMessage, RecordingGateway

ROOT = Path(__file__).resolve().parent.parent
POINTISH = re.compile(r"\bpoints?\b|\bpts\b|\bscore\b", re.IGNORECASE)


def assert_clean(texts):
    for t in texts:
        assert not leaks_points(t), t
        assert not POINTISH.search(t), t


# ------------------------------------------------------------------ 1. copy
def test_copy_strings_are_point_free(copy):
    for key in copy.all_keys():
        fields = {f: "X" for f in copy.placeholders(key)}
        assert not any(re.search(r"point|score|pts", f, re.I) for f in fields), key
        assert_clean([copy.t(key, **fields)])


def test_copy_refuses_point_placeholders(copy):
    with pytest.raises(ValueError):
        copy.t("placement.rank_up", name="a", rank="b", points=5)


def test_every_copy_key_used_in_code_exists(copy):
    used = set()
    for p in (ROOT / "arcbot").rglob("*.py"):
        used |= set(re.findall(r"""\.t\(\s*f?["']([a-z_]+\.[a-z_.]+)["']""", p.read_text(encoding="utf-8")))
    missing = [k for k in used if not copy.has(k)]
    assert not missing, missing
    for f in ("hours", "knockouts", "squad_revives", "stranger_revives", "quests", "containers", "expeditions"):
        assert copy.has(f"apply.fields.{f}")


# ------------------------------------------------------------------ 2. guard
@pytest.mark.parametrize("text", ["You earned 5 points!", "points: 12", "Total 150 pts"])
def test_guard_blocks(text):
    assert leaks_points(text)


async def test_gateway_refuses_leak():
    gw = RecordingGateway()
    with pytest.raises(PointLeakError):
        await gw.send(FakeChannel("x"), "You now have 51 points")
    await gw.send(FakeChannel("x"), "mods see 51 points", mod_only=True)  # mod-only is allowed


def test_guard_ignores_user_text():
    assert not leaks_points("Job: carry 10 points of loot", user_texts=["carry 10 points of loot"])


# ------------------------------------------------------------------ 3. flows
@pytest.fixture
async def world(cfg, copy, monkeypatch, tmp_path):
    cfg.backups["enabled"] = False
    cfg.timers["enabled"] = False
    app = App(cfg, copy, connect(":memory:"), dry_run=False, guild_id=None)
    gw = RecordingGateway()
    app.gateway = gw
    app.alerts.gateway = gw
    guild = FakeGuild()
    app.guild = guild  # type: ignore[assignment]
    for key in ("rank_up_announcements", "mod_review", "rank_promotion", "job_board", "vouch", "apply"):
        ch = FakeChannel(cfg.channels[key], guild)
        guild.channels[ch.id] = ch
        app.resolved.channels[key] = ch  # type: ignore[assignment]
    for rk in cfg.ranks:
        app.resolved.roles[rk.role] = type("R", (), {"name": rk.role, "mention": "@" + rk.role})()
    app.resolved.roles[cfg.unplaced_role] = type("R", (), {"name": cfg.unplaced_role})()
    members: dict[int, FakeMember] = {}

    async def member(uid):
        return members.setdefault(uid, FakeMember(uid, f"Raider{uid}"))

    monkeypatch.setattr(app, "member", member)
    import arcbot.cogs.intake_ui as iu
    import arcbot.cogs.jobs as cj

    monkeypatch.setattr(iu, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(cj, "JOB_UPLOADS", tmp_path)
    bot = ArcBot(app)
    await bot.setup_hook()
    yield app, bot, gw, members
    await bot.close()


def _session(bot, uid, kind, stats: Stats, name):
    s = bot.intake_flow.start(uid, kind)
    s.name = name
    for f, v in stats.as_dict().items():
        s.values[f] = v
    s.missing.clear()
    s.unsure.clear()
    return s


async def test_player_flows_never_show_points(world):
    app, bot, gw, members = world
    replies: list[str] = []

    async def act(uid, fn, *args, message=None, **kwargs):
        user = await app.member(uid)
        it = FakeInteraction(bot, user, message=message)
        await fn(it, *args, **kwargs)
        replies.extend(it.replies)
        return it

    # Skip
    await act(1, bot.get_cog("Onboarding").skip)
    # intake: auto-place (Pathfinder) and pending (Vanguard) then approve
    b = Stats(parse_hours("131:18:27"), 31, 44, 10, 31, 6381, 1)
    a = Stats(parse_hours("268:02:47"), 350, 219, 43, 26, 14099, 1)
    flow = bot.intake_flow
    await act(2, flow.finalize, _session(bot, 2, "onboarding", b, "Bee"))
    await act(3, flow.finalize, _session(bot, 3, "onboarding", a, "Ay"))
    sub = app.intake.pending_for(3)
    result = app.intake.decide(sub["id"], "approve", mod_id=99)
    await flow.apply_decision(await app.member(99), FakeMessage(), sub["id"], result)
    # (the recorder strips player names before scanning, hence no name in the match)
    assert any("Welcome aboard" in t and "the Vanguard" in t for t in gw.player_visible), "approval should welcome"
    assert any("Welcome aboard" in t and "the Pathfinder" in t for t in gw.player_visible)
    # promotion that changes nothing
    await act(2, flow.finalize, _session(bot, 2, "promotion", Stats(10, 1, 1, 1, 1, 10, 0), "Bee"))
    # readback text
    replies.append(flow.readback(_session(bot, 4, "onboarding", b, "Cee"))[0])

    # vouch that triggers a rank-up (Green Horn 14 -> Scavenger)
    app.engine.place(5, "green_horn")
    app.engine.add_points(5, 14, "test")
    voucher = await app.member(2)
    target = await app.member(5)
    msg = FakeMessage(app.channel("vouch"), f"<@5> saved my whole squad at the Dam today", voucher, [target], guild=1)
    await bot.get_cog("Vouch").on_message(msg)
    assert gw.reactions, "well-formed vouch got no reaction"
    assert app.engine.get_user(5)["rank_key"] == "scavenger"

    # job cycle: post -> several attempt (private thread) -> first marks complete -> poster confirms -> vouch
    jobs = bot.get_cog("Jobs")
    await act(2, jobs.submit, "Spaceport quest run", "Need a hand clearing the Spaceport quest chain tonight.",
              "anyone", [], boost=2)  # not a Guild Master: the boost is ignored
    job = app.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert job["status"] == "open" and job["xp_multiplier"] == 1
    await act(3, jobs.on_accept, job["id"], message=FakeMessage(app.channel("job_board")))
    await act(5, jobs.on_accept, job["id"], message=FakeMessage(app.channel("job_board")))
    thread = gw.threads[-1]
    assert jobs.service.get(job["id"])["thread_id"] == thread.id and sorted(thread.members) == [2, 3, 5]
    it = await act(2, jobs.on_complete, job["id"])  # the poster confirms, never claims
    assert any("can confirm it" in r for r in it.replies)
    await act(5, jobs.on_complete, job["id"])
    it = await act(3, jobs.on_complete, job["id"])  # 5 got there first
    assert any("already marked this one done" in r for r in it.replies)
    assert any("Pending completion" in t for t in gw.player_visible)
    app.engine.place(9, "green_horn")
    it = await act(9, jobs.on_accept, job["id"])  # heads-up before joining a job that may already be done
    assert any("Heads up" in r for r in it.replies) and 9 not in jobs.service.attempters(job["id"])
    await act(9, jobs.on_join, job["id"])
    assert 9 in thread.members
    it = await act(5, jobs.on_confirm, job["id"])
    assert any("Only the raider who posted" in r for r in it.replies)
    await act(2, jobs.on_confirm, job["id"])
    done = jobs.service.get(job["id"])
    assert done["status"] == "completed" and done["helper_id"] == 5
    assert any(t is thread and "vouch" in text for t, text in gw.sent)
    await jobs.housekeeping()
    assert not thread.closed  # waits for the poster's vouch
    await act(2, bot.get_cog("Vouch").guided_vouch, await app.member(5), "Cleared the whole Spaceport chain with me")
    assert thread.closed and jobs.service.get(job["id"])["thread_closed_at"]
    # Guild Master boost: only offered to the Guild Master, and only honoured for them
    import arcbot.cogs.jobs as cj
    assert cj.PostJobModal(app).boost_in is None
    assert cj.PostJobModal(app, can_boost=True).boost_in is not None
    real_gm = app.is_guild_master
    app.is_guild_master = lambda m: m.id == 4  # type: ignore[method-assign]
    await act(4, jobs.submit, "Guild night", "Big guild night run through Buried City, all welcome.", "anyone", [],
              boost=2)
    app.is_guild_master = real_gm  # type: ignore[method-assign]
    special = app.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert special["xp_multiplier"] == app.cfg.job_xp_boosts[2].multiplier > 1
    assert any("Special job" in t for t in gw.player_visible)
    # job held for mods, then rejected
    await act(1, jobs.submit, "Cheap carry", "paid carry service, dm me now for a price", "anyone", [])

    # join ping, guided vouches, heads-up job posts, completion by either side, check-ins, escalation ----
    # a brand-new member gets a self-deleting welcome mention in #apply; a returning one does not
    await bot.get_cog("Onboarding").on_member_join(await app.member(8))
    assert any("Welcome to the Outpost" in t for t in gw.player_visible), "join ping missing"
    pings = sum("Welcome to the Outpost" in t for t in gw.player_visible)
    await bot.get_cog("Onboarding").on_member_join(await app.member(2))  # already placed
    assert sum("Welcome to the Outpost" in t for t in gw.player_visible) == pings
    # guided /vouch and the near-miss hint
    vouch = bot.get_cog("Vouch")
    await act(3, vouch.guided_vouch, await app.member(5), "Helped me clear the whole Spaceport quest chain")
    await act(3, vouch.guided_vouch, await app.member(5), "ty")  # too short -> friendly nudge
    near = FakeMessage(app.channel("vouch"), "<@5> ty!", await app.member(2), [target], guild=1)
    await vouch.on_message(near)
    assert any("few words" in t for t in gw.player_visible), "near-miss hint missing"
    # heads-up post (soft flag word) goes up immediately
    await act(3, jobs.submit, "WTS spare parts run", "Selling nothing, wts just means I'll share spare parts on a run.",
              "anyone", [])
    soft = app.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    assert soft["status"] == "open" and any("heads-up" in t for t in gw.mod_visible)
    # attempter marks done and the poster sends it back once, then confirms; ineligible accept wording
    await act(5, jobs.on_accept, soft["id"], message=FakeMessage(app.channel("job_board")))
    await act(5, jobs.on_complete, soft["id"])
    await act(3, jobs.on_notdone, soft["id"])
    assert jobs.service.get(soft["id"])["status"] == "accepted"
    await act(5, jobs.on_complete, soft["id"])
    await act(3, jobs.on_confirm, soft["id"])
    assert jobs.service.get(soft["id"])["status"] == "completed"
    vet_job, _, _ = jobs.service.create(4, "Veteran run", "Hard Matriarch kill, Veterans only please.", "veterans_only",
                                        has_image=False)
    jobs.service.approve(vet_job)
    it = await act(2, jobs.on_accept, vet_job)
    assert any("Veterans only" in r for r in it.replies)
    # check-ins and a stalled confirmation handed to mods
    from datetime import timedelta as _td
    from arcbot.db import iso as _iso, utcnow as _now
    stall, _, _ = jobs.service.create(2, "Stalled run", "Need help with the Buried City quests this weekend.", "anyone",
                                      has_image=False)
    jobs.service.accept(stall, 3, helper_rank="pathfinder", days_in_guild=10, now=_now() - _td(days=11))
    await jobs.housekeeping()
    assert any("did this one get done" in t for t in gw.player_visible)
    jobs.service.mark_complete(stall, 3)
    app.conn.execute("UPDATE jobs SET completion_requested_at = ? WHERE id = ?", (_iso(_now() - _td(days=8)), stall))
    await jobs.housekeeping()
    assert jobs.service.get(stall)["status"] == "needs_mod"
    await jobs._resolve(FakeInteraction(bot, await app.member(99), message=FakeMessage()), stall, award=True)
    assert jobs.service.get(stall)["status"] == "completed"
    # read-back with an unsure number and the name taken from the screenshot
    s = _session(bot, 7, "onboarding", b, "")
    s.unsure.add("knockouts")
    s.ocr_name, s.ocr_name_conf, s.source = "ScreenName", 0.95, "ocr"
    s.name = s.ocr_name
    content, view = flow.readback(s)
    labels = [getattr(i, "label", "") for i in view.children]
    assert "Looks right" in labels, labels  # unsure numbers can be confirmed as they are
    replies.append(content)
    await act(7, flow.finalize, s)
    assert app.engine.get_user(7)["rank_key"] == "pathfinder"

    # mod award that causes a public rank-up announcement
    mod = bot.get_cog("ModTools")
    app.engine.place(6, "green_horn")
    mt = FakeInteraction(bot, await app.member(99))
    await mod.award.callback(mod, mt, await app.member(6), 20, "carried a whole raid night")

    assert gw.player_visible, "nothing player-visible was produced"
    assert_clean(gw.player_visible)
    assert_clean(replies)
    # sanity: mod channel really does carry the numbers (so the scan above is meaningful)
    assert any("rating" in t for t in gw.mod_visible)


# ------------------------------------------------------------------ 4. static
def test_point_values_only_formatted_in_mod_code():
    allowed = {"modtools.py"}
    pattern = re.compile(r"""\[["']points["']\]|\.points\b|awarded_points""")
    offenders = []
    for p in (ROOT / "arcbot" / "cogs").glob("*.py"):
        if p.name in allowed:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "# mod-only" not in line and "log." not in line:
                offenders.append(f"{p.name}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
