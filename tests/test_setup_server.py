"""Planner tests for the one-time server setup tool (the Discord calls themselves need a live server)."""
import pytest

from arcbot.setup_server import (BOT, EVERYONE, ChannelSnap, MemberSnap, Snapshot, plan_lockdown, plan_new, role)

BOT_PERMS = {"view_channel", "send_messages", "send_messages_in_threads", "embed_links", "attach_files",
             "read_message_history", "add_reactions", "manage_roles", "manage_channels", "create_public_threads"}


@pytest.fixture
def snap(cfg):
    staff_deny = {EVERYONE: {"view_channel": False}, role("Guild Master"): {"view_channel": True}}
    chans = [
        ChannelSnap(1, "Info", "category", None),
        ChannelSnap(2, "rules", "text", 1),
        ChannelSnap(3, "welcome", "text", 1),
        ChannelSnap(10, "Community", "category", None),
        ChannelSnap(11, "general-chat", "text", 10),
        ChannelSnap(12, "vouch", "text", 10),
        ChannelSnap(13, "job-board", "text", 10),
        ChannelSnap(14, "event-timers", "text", 10, {EVERYONE: {"send_messages": False}}),
        ChannelSnap(15, "trading-post", "text", 10, {role("Green Horn"): {"view_channel": False}}),
        ChannelSnap(16, "hall-of-fame", "text", 10),
        ChannelSnap(20, "Staff", "category", None, staff_deny),
        ChannelSnap(21, "staff-chat", "text", 20),  # private through its category
        ChannelSnap(22, "secret", "text", None, {EVERYONE: {"view_channel": False}}),
        ChannelSnap(30, "Raid Comms", "voice", 10),
    ]
    members = [
        MemberSnap(100, "Ranked", {"@everyone", "Pathfinder"}),
        MemberSnap(101, "NoRank", {"@everyone"}),
        MemberSnap(102, "Boss", {"@everyone", "Guild Master"}),
        MemberSnap(103, "SomeBot", {"@everyone"}, bot=True),
    ]
    roles = {"@everyone", "Guild Master", "Green Horn", "Scavenger", "Pathfinder", "Vanguard", "Veteran"}
    return Snapshot(roles, BOT_PERMS, chans, members)


def _creates(acts):
    return {a.data["name"]: a for a in acts if a.kind == "create_channel"}


def test_new_stage_creates_role_and_owned_channels(cfg, snap):
    acts = plan_new(cfg, snap)
    assert any(a.kind == "create_role" and a.data["name"] == "Newcomer" for a in acts)
    created = _creates(acts)
    assert set(created) == {"apply", "rank-promotion", "mod-review"}
    apply = created["apply"].data["overwrites"]
    assert apply[EVERYONE] == {"view_channel": False}
    assert apply[role("Newcomer")]["view_channel"] is True and apply[role("Newcomer")]["send_messages"] is False
    assert apply[role("Guild Master")]["send_messages"] is True
    assert role("Pathfinder") not in apply  # ranked members don't see #apply
    review = created["mod-review"].data["overwrites"]
    assert set(review) == {EVERYONE, role("Guild Master"), BOT}
    promo = created["rank-promotion"].data["overwrites"]
    assert all(promo[role(r)]["send_messages"] is False for r in ("Green Horn", "Veteran"))


def test_new_stage_only_adds_bot_access_to_existing_channels(cfg, snap):
    acts = [a for a in plan_new(cfg, snap) if a.kind == "set_overwrite"]
    assert acts and all(a.data["target"] == BOT for a in acts)
    assert {a.data["channel"] for a in acts} == {"vouch", "job-board", "event-timers", "general-chat"}


def test_bot_never_granted_permissions_it_lacks(cfg, snap):
    for a in plan_new(cfg, snap) + plan_lockdown(cfg, snap):
        perms = a.data.get("perms") or {}
        if a.data.get("target") == BOT:
            assert "pin_messages" not in perms  # bot lacks it in this snapshot


def test_lockdown_hides_public_channels_and_keeps_private_ones(cfg, snap):
    acts = plan_lockdown(cfg, snap)
    touched = {a.data["channel"] for a in acts if a.kind == "set_overwrite"}
    assert {"general-chat", "vouch", "trading-post", "Raid Comms", "Community", "rules"} <= touched
    assert not touched & {"staff-chat", "secret", "Staff"}
    notes = " ".join(a.text for a in acts if a.kind == "note")
    assert "staff-chat" in notes and "secret" in notes


def test_lockdown_respects_explicit_role_denies(cfg, snap):
    acts = plan_lockdown(cfg, snap)
    gh_trading = [a for a in acts if a.kind == "set_overwrite" and a.data["channel"] == "trading-post"
                  and a.data["target"] == role("Green Horn")]
    assert gh_trading == []  # Green Horn was hidden from #trading-post on purpose


def test_lockdown_newcomer_reads_rules_and_welcome_only(cfg, snap):
    acts = plan_lockdown(cfg, snap)
    nc = {a.data["channel"]: a.data["perms"] for a in acts
          if a.kind == "set_overwrite" and a.data["target"] == role("Newcomer")}
    assert set(nc) == {"rules", "welcome"}
    assert nc["rules"] == {"view_channel": True, "send_messages": False, "add_reactions": False}


def test_lockdown_gives_unranked_members_newcomer(cfg, snap):
    adds = [a.data["member_id"] for a in plan_lockdown(cfg, snap) if a.kind == "add_role"]
    assert adds == [101]  # not the ranked member, the Guild Master or the bot


def test_lockdown_keeps_existing_overwrite_keys(cfg, snap):
    acts = plan_lockdown(cfg, snap)
    ev = [a for a in acts if a.data.get("channel") == "event-timers" and a.data.get("target") == EVERYONE]
    assert ev and ev[0].data["perms"] == {"view_channel": False}  # send_messages deny is left as it was
    assert ev[0].data["before"] == {"send_messages": False}


def test_plans_are_idempotent(cfg, snap):
    """Applying a plan to the snapshot leaves nothing to do on a second run."""
    for planner in (plan_new, plan_lockdown):
        for a in planner(cfg, snap):
            if a.kind == "create_role":
                snap.roles.add(a.data["name"])
            elif a.kind == "create_channel":
                snap.channels.append(ChannelSnap(999 + len(snap.channels), a.data["name"], "text", None,
                                                 dict(a.data["overwrites"])))
            elif a.kind == "set_overwrite":
                ch = next(c for c in snap.channels if c.id == a.data["channel_id"])
                ch.overwrites[a.data["target"]] = {**ch.overwrites.get(a.data["target"], {}), **a.data["perms"]}
            elif a.kind == "add_role":
                next(m for m in snap.members if m.id == a.data["member_id"]).roles.add(a.data["role"])
        again = [a for a in planner(cfg, snap) if a.kind != "note"]
        assert again == [], [a.text for a in again]


# ------------------------------------------------------- a realistic server layout (emoji names, staff category)
@pytest.fixture
def outpost(cfg):
    chans = [
        ChannelSnap(1, "Text Channels", "category", None),
        ChannelSnap(2, "💬general-chat", "text", 1),
        ChannelSnap(3, "📋rules", "text", 1),
        ChannelSnap(4, "✅vouch", "text", 1),
        ChannelSnap(5, "💼job-board", "text", 1),
        ChannelSnap(6, "⏰event-timers", "text", 1),
        ChannelSnap(10, "⚔️PvP", "category", None),
        ChannelSnap(11, "Squad 1", "voice", 10),
        ChannelSnap(20, "🛠️PvE", "category", None),
        ChannelSnap(21, "Squad 1", "voice", 20),
        ChannelSnap(30, "Mod", "category", None),
        ChannelSnap(31, "mod-log", "text", 30),
        ChannelSnap(32, "bot-logs", "text", 30),
    ]
    members = [MemberSnap(100, "Ranked", {"@everyone", "Pathfinder"})]
    roles = {"@everyone", "Guild Master", "Green Horn", "Scavenger", "Pathfinder", "Vanguard", "Veteran"}
    return Snapshot(roles, BOT_PERMS, chans, members)


def test_other_bots_keep_access_after_lockdown(cfg, outpost):
    outpost.members += [MemberSnap(200, "Carl-bot", {"@everyone", "Carl-bot"}, bot=True),
                        MemberSnap(201, "ArcBot", {"@everyone", "ArcBot"}, bot=True, is_self=True),
                        MemberSnap(202, "Loner", {"@everyone"}, bot=True)]
    acts = plan_lockdown(cfg, outpost)
    carl = {a.data["channel"] for a in acts if a.kind == "set_overwrite" and a.data["target"] == role("Carl-bot")}
    assert "💬general-chat" in carl and "mod-log" not in carl  # public channels yes, staff channels no
    assert not [a for a in acts if a.kind == "set_overwrite" and a.data["target"] == role("ArcBot")]
    assert any("Loner" in a.text for a in acts if a.kind == "note")


def test_bot_access_first_everyone_last(cfg, outpost):
    """Hiding a channel from @everyone before the bot has its own access would lock the bot out mid-edit."""
    acts = [a for a in plan_lockdown(cfg, outpost) if a.kind == "set_overwrite"]
    by_channel: dict[int, list] = {}
    for a in acts:
        by_channel.setdefault(a.data["channel_id"], []).append(a.data["target"])
    for targets in by_channel.values():
        if EVERYONE in targets:
            assert targets[-1] == EVERYONE, targets
        if BOT in targets:
            assert targets[0] == BOT, targets


def test_real_discord_overwrites_are_understood(cfg):
    """discord.py names view 'read_messages'. A private channel must stay private."""
    import discord

    from arcbot.setup_server import canon, perms_from_pair

    private = perms_from_pair(*discord.PermissionOverwrite(view_channel=False, connect=False).pair())
    assert private == {"view_channel": False, "connect": False}
    bot = {canon(p) for p, v in discord.Permissions(view_channel=True, send_messages=True) if v}
    assert "view_channel" in bot
    snap = Snapshot({"@everyone", "Guild Master", "Green Horn", "Scavenger", "Pathfinder", "Vanguard", "Veteran"},
                    bot | {"manage_roles", "manage_channels"},
                    [ChannelSnap(1, "PvP", "category", None), ChannelSnap(2, "Squad 1", "voice", 1, {EVERYONE: private}),
                     ChannelSnap(3, "content", "text", 1)], [])
    acts = plan_lockdown(cfg, snap)
    touched = {a.data["channel"] for a in acts if a.kind == "set_overwrite"}
    assert "Squad 1" not in touched  # already private: left alone
    content = [a.data["target"] for a in acts if a.kind == "set_overwrite" and a.data["channel"] == "content"]
    assert content[0] == BOT and content[-1] == EVERYONE  # bot can see it before @everyone is hidden


def test_norm_channel():
    from arcbot.naming import norm_channel

    assert norm_channel("✅vouch") == norm_channel("vouch") == norm_channel("#vouch") == "vouch"
    assert norm_channel("💬・general-chat") == "general-chat"
    assert norm_channel("🛠️PvE") == "pve"


def test_emoji_named_channels_are_found_not_duplicated(cfg, outpost):
    created = set(_creates(plan_new(cfg, outpost)))
    assert created == {"apply", "rank-promotion", "mod-review"}  # no second #vouch / #job-board / #event-timers
    notes = " ".join(a.text for a in plan_new(cfg, outpost) if a.kind == "note")
    assert "general-chat" not in notes
    bot_access = {a.data["channel"] for a in plan_new(cfg, outpost) if a.kind == "set_overwrite"}
    assert {"✅vouch", "💼job-board", "⏰event-timers", "💬general-chat"} <= bot_access


def test_emoji_rules_visible_to_newcomers(cfg, outpost):
    nc = {a.data["channel"] for a in plan_lockdown(cfg, outpost)
          if a.kind == "set_overwrite" and a.data["target"] == role("Newcomer")}
    assert nc == {"📋rules"}


def test_mod_category_becomes_staff_only(cfg, outpost):
    acts = [a for a in plan_lockdown(cfg, outpost) if a.kind == "set_overwrite"]
    staff = [a for a in acts if a.data["channel"] in ("Mod", "mod-log", "bot-logs")]
    assert staff and all(a.data.get("mod_only") for a in staff)
    assert not [a for a in staff if a.data["target"] in [role(r) for r in
                                                         ("Green Horn", "Scavenger", "Pathfinder", "Vanguard", "Veteran")]]
    assert any(a.data["target"] == role("Guild Master") for a in staff)


def test_plan_summary_is_one_line_per_channel_with_category(cfg, outpost):
    from arcbot.setup_server import summarize

    lines = summarize(plan_lockdown(cfg, outpost))
    squads = [l for l in lines if "Squad 1" in l]
    assert len(squads) == 2 and any("(in ⚔️PvP)" in l for l in squads) and any("(in 🛠️PvE)" in l for l in squads)
    assert any(l.startswith("#mod-log") and "STAFF ONLY" in l for l in lines)
    assert len(lines) == len({a.data["channel_id"] for a in plan_lockdown(cfg, outpost) if a.kind == "set_overwrite"})
