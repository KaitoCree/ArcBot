"""One-time Discord server setup. The running bot never changes channel permissions; this tool does, once,
from a printed plan, after an explicit confirmation, with a rollback file.

    python -m arcbot.setup_server                    show the plan for both stages, change nothing
    python -m arcbot.setup_server --apply new        stage 1: Newcomer role, #apply, #rank-promotion,
                                                     #mod-review (+ any missing bot channels), bot access
    python -m arcbot.setup_server --apply lockdown   stage 2: hide public channels from members without a
                                                     rank, Newcomer sees #apply/#rules/#welcome, unranked
                                                     members get Newcomer
    python -m arcbot.setup_server --rollback data/setup-rollback-....json   (or --rollback latest-lockdown)

Stage 2 leaves private channels (already hidden from @everyone) alone, and keeps any explicit "deny view" a
role already has. It never deletes anything; rollback restores overwrites and removes the Newcomer roles it
added, and lists anything it created so you can delete it by hand if you want.

Needs the bot role to have Manage Roles and Manage Channels while it runs (give Manage Channels just for this).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import ROOT, Config, load_config
from .naming import norm_channel

log = logging.getLogger("arcbot.setup")

Target = tuple  # ("everyone",) | ("role", name) | ("bot",)
EVERYONE: Target = ("everyone",)
BOT: Target = ("bot",)

# discord.py reports some permissions under their old internal names; the planner uses the modern ones
# (otherwise an existing 'deny view' would be missed and a private channel would look public).
ALIASES = {"read_messages": "view_channel", "external_emojis": "use_external_emojis",
           "external_stickers": "use_external_stickers", "manage_emojis": "manage_expressions"}


def canon(name: str) -> str:
    return ALIASES.get(name, name)


def perms_from_pair(allow, deny) -> dict[str, bool]:
    """discord.py (allow, deny) Permissions pair -> {modern_name: True/False} for the flags that are set."""
    out = {canon(p): True for p, v in allow if v}
    out.update({canon(p): False for p, v in deny if v})
    return out


BOT_PERMS = ["view_channel", "send_messages", "send_messages_in_threads", "embed_links", "attach_files",
             "read_message_history", "add_reactions", "pin_messages", "create_public_threads",
             "create_private_threads", "manage_threads"]


def role(name: str) -> Target:
    return ("role", name)


def tlabel(t: Target) -> str:
    return {"everyone": "@everyone", "bot": "arcbot"}.get(t[0], t[-1])


# ------------------------------------------------------------------ snapshot
@dataclass
class ChannelSnap:
    id: int
    name: str
    kind: str  # text | voice | category | other
    category_id: int | None
    overwrites: dict[Target, dict[str, bool]] = field(default_factory=dict)


@dataclass
class MemberSnap:
    id: int
    name: str
    roles: set[str]
    bot: bool = False
    is_self: bool = False  # arcbot itself


@dataclass
class Snapshot:
    roles: set[str]
    bot_perms: set[str]
    channels: list[ChannelSnap]
    members: list[MemberSnap]
    everyone_can_view: bool = True

    def channel(self, name: str) -> ChannelSnap | None:
        """Match ignoring emoji/punctuation decoration ('✅vouch' == 'vouch'). Text channels win over voice."""
        want = norm_channel(name)
        hits = [c for c in self.channels if c.kind not in ("category", "other") and norm_channel(c.name) == want]
        hits.sort(key=lambda c: c.kind != "text")
        return hits[0] if hits else None

    def where(self, ch: ChannelSnap) -> str:
        cat = self.category_of(ch)
        return cat.name if cat else ""

    def in_categories(self, ch: ChannelSnap, names: set[str]) -> bool:
        if ch.kind == "category":
            return norm_channel(ch.name) in names
        cat = self.category_of(ch)
        return norm_channel(ch.name) in names or (cat is not None and norm_channel(cat.name) in names)

    def category_of(self, ch: ChannelSnap) -> ChannelSnap | None:
        for c in self.channels:
            if c.kind == "category" and c.id == ch.category_id:
                return c
        return None


# --------------------------------------------------------------------- plan
@dataclass
class Action:
    kind: str  # create_role | create_channel | set_overwrite | add_role | note
    text: str
    data: dict[str, Any] = field(default_factory=dict)


def _bot_allow(snap: Snapshot, extra: list[str] | None = None) -> dict[str, bool]:
    wanted = BOT_PERMS + (extra or [])
    # Discord refuses overwrites that grant permissions the bot itself lacks
    return {p: True for p in wanted if p in snap.bot_perms or "administrator" in snap.bot_perms}


def channel_specs(cfg: Config, snap: Snapshot) -> dict[str, dict[Target, dict[str, bool]]]:
    """Desired overwrites for the channels arcbot owns or uses, keyed by config channel key."""
    ranked = [role(r.role) for r in cfg.ranks]
    mods = [role(m) for m in cfg.mod_roles]
    newcomer = role(cfg.unplaced_role)
    mod_rw = {"view_channel": True, "send_messages": True, "read_message_history": True}
    bot = _bot_allow(snap)

    def spec(base: dict[Target, dict[str, bool]]) -> dict[Target, dict[str, bool]]:
        out = {EVERYONE: {"view_channel": False}, **base, BOT: bot}
        for m in mods:
            out[m] = mod_rw
        return out

    read_only = {"view_channel": True, "send_messages": False, "read_message_history": True}
    return {
        "apply": spec({newcomer: {"view_channel": True, "send_messages": False, "read_message_history": True,
                                  "add_reactions": False}}),
        "rank_promotion": spec({r: read_only for r in ranked}),
        "mod_review": spec({}),
        "vouch": spec({r: {"view_channel": True, "send_messages": True, "read_message_history": True}
                       for r in ranked}),
        "job_board": spec({r: {**read_only, "send_messages_in_threads": True} for r in ranked}),
        "event_timers": spec({r: read_only for r in ranked}),
        "rank_up_announcements": spec({r: {"view_channel": True, "send_messages": True,
                                           "read_message_history": True} for r in ranked}),
    }


# channels the tool may create when missing; the rest of the config channels must already exist
CREATABLE = ("apply", "rank_promotion", "mod_review", "vouch", "job_board", "event_timers")
OWNED = ("apply", "rank_promotion", "mod_review")


def _diff(current: dict[str, bool] | None, wanted: dict[str, bool]) -> dict[str, bool]:
    cur = current or {}
    return {k: v for k, v in wanted.items() if cur.get(k) != v}


def plan_new(cfg: Config, snap: Snapshot) -> list[Action]:
    acts: list[Action] = []
    if cfg.unplaced_role not in snap.roles:
        acts.append(Action("create_role", f"create role '{cfg.unplaced_role}' (no permissions)",
                           {"name": cfg.unplaced_role}))
    specs = channel_specs(cfg, snap)
    category = cfg.raw.get("setup", {}).get("new_channel_category")
    for key, wanted in specs.items():
        name = cfg.channels[key].lstrip("#")
        ch = snap.channel(name)
        if ch is not None:
            name = ch.name
        if ch is None:
            if key in CREATABLE:
                acts.append(Action("create_channel", f"create #{name}" + (f" in '{category}'" if category else ""),
                                   {"name": name, "category": category, "overwrites": wanted}))
            else:
                acts.append(Action("note", f"#{name} is missing: create it yourself or change channels.{key}"))
            continue
        # existing channel: arcbot's own channels get the full permission set; channels it only uses just get bot access
        targets = wanted if key in OWNED else {BOT: wanted[BOT]}
        for t, perms in targets.items():
            d = _diff(ch.overwrites.get(t), perms)
            if d:
                acts.append(Action("set_overwrite", f"#{name}: {tlabel(t)} {_fmt(d)}",
                                   {"channel_id": ch.id, "channel": name, "target": t, "perms": d,
                                    "before": ch.overwrites.get(t), "where": snap.where(ch), "kind": ch.kind}))
    return acts


def _effective_everyone_view(snap: Snapshot, ch: ChannelSnap) -> bool:
    own = ch.overwrites.get(EVERYONE, {}).get("view_channel")
    if own is not None:
        return own
    cat = snap.category_of(ch)
    if cat is not None:
        v = cat.overwrites.get(EVERYONE, {}).get("view_channel")
        if v is not None:
            return v
    return snap.everyone_can_view


def plan_lockdown(cfg: Config, snap: Snapshot) -> list[Action]:
    acts: list[Action] = []
    setup = cfg.raw.get("setup", {}) or {}
    owned = {norm_channel(cfg.channels[k]) for k in OWNED}
    bot_channels = {norm_channel(cfg.channels[k]) for k in ("vouch", "job_board", "event_timers",
                                                            "rank_up_announcements")}
    newcomer_visible = {norm_channel(n) for n in setup.get("newcomer_visible", [])}
    mod_only = {norm_channel(n) for n in setup.get("mod_only", [])}
    ranked = [role(r.role) for r in cfg.ranks]
    mods = [role(m) for m in cfg.mod_roles]
    newcomer = role(cfg.unplaced_role)
    private: list[str] = []
    others = other_bot_roles(cfg, snap)
    for b in snap.members:
        if b.bot and not b.is_self and not (b.roles - {"@everyone"}):
            acts.append(Action("note", f"bot {b.name} has no role of its own: it will lose access after lockdown "
                                       "(give it a role, then re-run the plan)"))
    bot_view = {"view_channel": True} if "view_channel" in snap.bot_perms or "administrator" in snap.bot_perms else {}
    for ch in snap.channels:
        n = norm_channel(ch.name)
        if n in owned or ch.kind == "other":
            continue
        if not _effective_everyone_view(snap, ch):
            private.append(ch.name)
            continue
        label = ch.name if ch.kind != "category" else f"[category] {ch.name}"
        wanted: dict[Target, dict[str, bool]] = {EVERYONE: {"view_channel": False}}
        if snap.in_categories(ch, mod_only):
            # staff channels: Guild Master (and any mod role) only, never the rank roles
            for t in mods:
                wanted[t] = {"view_channel": True}
            for t in ranked:
                if ch.overwrites.get(t, {}).get("view_channel"):
                    wanted[t] = {"view_channel": False}
            wanted[BOT] = _bot_allow(snap)
        else:
            for t in ranked + mods + others:
                if ch.overwrites.get(t, {}).get("view_channel") is False:
                    continue  # someone hid this channel from that role on purpose: keep it
                wanted[t] = {"view_channel": True}
            if n in newcomer_visible:
                wanted[newcomer] = {"view_channel": True, "send_messages": False, "add_reactions": False}
            wanted[BOT] = _bot_allow(snap) if n in bot_channels else bot_view
        for t, perms in safe_order(wanted):
            d = _diff(ch.overwrites.get(t), perms)
            if d:
                acts.append(Action("set_overwrite", f"{'#' if ch.kind != 'category' else ''}{label}: {tlabel(t)} {_fmt(d)}",
                                   {"channel_id": ch.id, "channel": ch.name, "target": t, "perms": d,
                                    "before": ch.overwrites.get(t), "where": snap.where(ch), "kind": ch.kind,
                                    "mod_only": snap.in_categories(ch, mod_only)}))
    for raw in setup.get("newcomer_visible", []):
        if snap.channel(raw) is None:
            acts.append(Action("note", f"#{raw} (setup.newcomer_visible) doesn't exist; Newcomers won't see it"))
    if private:
        acts.append(Action("note", f"left alone (already private): {', '.join('#' + p for p in sorted(private))}"))
    if cfg.raw.get("setup", {}).get("give_unranked_newcomer", True):
        rank_roles = {r.role for r in cfg.ranks}
        for m in snap.members:
            if m.bot or m.roles & rank_roles or m.roles & set(cfg.mod_roles) or cfg.unplaced_role in m.roles:
                continue
            acts.append(Action("add_role", f"give {cfg.unplaced_role} to {m.name} (no rank role yet)",
                               {"member_id": m.id, "role": cfg.unplaced_role}))
    return acts


def safe_order(wanted: dict[Target, dict[str, bool]]) -> list[tuple[Target, dict[str, bool]]]:
    """Bot access first, @everyone last. Hiding a channel from @everyone before the bot has its own access
    locks the bot out mid-edit (Discord 50001 Missing Access)."""
    rank = {BOT: 0, EVERYONE: 2}
    return sorted(wanted.items(), key=lambda kv: rank.get(kv[0], 1))


def other_bot_roles(cfg: Config, snap: Snapshot) -> list[Target]:
    """Roles held only by other bots (their integration roles), so lockdown doesn't cut them off."""
    special = {"@everyone", cfg.unplaced_role, *cfg.mod_roles, *(r.role for r in cfg.ranks)}
    human_roles = {r for m in snap.members if not m.bot for r in m.roles}
    names: list[str] = []
    for m in snap.members:
        if not m.bot or m.is_self:
            continue
        for r in sorted(m.roles - special - human_roles):
            if r not in names:
                names.append(r)
    return [role(n) for n in names]


def _fmt(perms: dict[str, bool]) -> str:
    return ", ".join(f"{'+' if v else '-'}{k}" for k, v in perms.items())


# -------------------------------------------------------------- discord glue
def snapshot(guild, cfg: Config, members_list=None) -> Snapshot:  # pragma: no cover - needs a live guild
    import discord

    me = guild.me
    bot_perms = {canon(p) for p, v in me.guild_permissions if v}
    bot_target = bot_target_obj(guild)

    def target_of(obj) -> Target | None:
        if obj == bot_target:
            return BOT
        if isinstance(obj, discord.Role):
            return EVERYONE if obj.is_default() else role(obj.name)
        return None

    channels = []
    for ch in guild.channels:
        kind = ("category" if isinstance(ch, discord.CategoryChannel) else
                "text" if isinstance(ch, (discord.TextChannel, discord.ForumChannel)) else
                "voice" if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else "other")
        ows: dict[Target, dict[str, bool]] = {}
        for obj, ow in ch.overwrites.items():
            t = target_of(obj)
            if t is None:
                continue
            ows[t] = perms_from_pair(*ow.pair())
        channels.append(ChannelSnap(ch.id, ch.name, kind, getattr(ch, "category_id", None), ows))
    members = [MemberSnap(m.id, m.display_name, {r.name for r in m.roles}, m.bot, m.id == me.id)
               for m in (members_list if members_list is not None else guild.members)]
    return Snapshot({r.name for r in guild.roles}, bot_perms, channels, members,
                    everyone_can_view=guild.default_role.permissions.view_channel)


def bot_target_obj(guild):  # pragma: no cover
    """Overwrites for the bot go on its own managed role (falls back to the bot member)."""
    return guild.self_role or guild.me


async def _member(guild, member_id: int):  # pragma: no cover
    import discord

    m = guild.get_member(member_id)
    if m is not None:
        return m
    try:
        return await guild.fetch_member(member_id)
    except discord.HTTPException:
        return None


async def fetch_all_members(guild) -> list:  # pragma: no cover
    """Member list over plain HTTP: either works or fails with a clear error (gateway chunking can stall)."""
    return [m async for m in guild.fetch_members(limit=None)]


async def apply(guild, cfg: Config, actions: list[Action], rollback_path) -> None:  # pragma: no cover
    import discord

    record: dict[str, Any] = {"guild": guild.id, "at": datetime.now().isoformat(timespec="seconds"),
                              "overwrites": [], "added_roles": [], "created": []}

    def save() -> None:
        rollback_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def resolve(t: Target):
        if t == EVERYONE:
            return guild.default_role
        if t == BOT:
            return bot_target_obj(guild)
        r = discord.utils.get(guild.roles, name=t[1])
        if r is None:
            raise RuntimeError(f"role '{t[1]}' not found")
        return r

    for a in actions:
        if a.kind == "note":
            continue
        print(" ->", a.text)
        if a.kind == "create_role":
            r = await guild.create_role(name=a.data["name"], permissions=discord.Permissions.none(),
                                        reason="arcbot setup")
            record["created"].append({"role": r.name, "id": r.id})
        elif a.kind == "create_channel":
            cat = discord.utils.get(guild.categories, name=a.data["category"]) if a.data["category"] else None
            ows = {}
            for t, perms in a.data["overwrites"].items():
                try:
                    ows[resolve(t)] = discord.PermissionOverwrite(**perms)
                except RuntimeError as exc:
                    print(f"    skipped overwrite: {exc}")
            ch = await guild.create_text_channel(a.data["name"], category=cat, overwrites=ows,
                                                 reason="arcbot setup")
            record["created"].append({"channel": ch.name, "id": ch.id})
        elif a.kind == "set_overwrite":
            ch = guild.get_channel(a.data["channel_id"])
            target = resolve(a.data["target"])
            ow = ch.overwrites_for(target)
            record["overwrites"].append({"channel_id": ch.id, "channel": ch.name, "target": list(a.data["target"]),
                                         "before": a.data["before"]})
            ow.update(**a.data["perms"])
            await ch.set_permissions(target, overwrite=ow, reason="arcbot setup")
        elif a.kind == "add_role":
            m = await _member(guild, a.data["member_id"])
            r = discord.utils.get(guild.roles, name=a.data["role"])
            if m is not None and r is not None:
                await m.add_roles(r, reason="arcbot setup: no rank role yet, so they can reach #apply")
                record["added_roles"].append({"member_id": m.id, "role": r.name})
        save()
    save()


async def rollback(guild, data: dict[str, Any]) -> None:  # pragma: no cover
    import discord

    for item in reversed(data["overwrites"]):
        ch = guild.get_channel(item["channel_id"])
        t = tuple(item["target"])
        target = (guild.default_role if t == EVERYONE else bot_target_obj(guild) if t == BOT
                  else discord.utils.get(guild.roles, name=t[1]))
        if ch is None or target is None:
            print(f" !! skipped {item['channel']} {t}")
            continue
        before = item["before"]
        print(f" <- #{ch.name}: restore {tlabel(t)}")
        if before is None:
            await ch.set_permissions(target, overwrite=None, reason="arcbot setup rollback")
        else:
            await ch.set_permissions(target, overwrite=discord.PermissionOverwrite(**before),
                                     reason="arcbot setup rollback")
    for item in data["added_roles"]:
        m = await _member(guild, item["member_id"])
        r = discord.utils.get(guild.roles, name=item["role"])
        if m is not None and r is not None and r in m.roles:
            await m.remove_roles(r, reason="arcbot setup rollback")
    if data["created"]:
        print("Created by setup (not deleted automatically; delete by hand if you want):")
        for c in data["created"]:
            print("   ", c)


def summarize(acts: list[Action]) -> list[str]:
    """One readable line per channel instead of one per permission."""
    lines: list[str] = []
    grouped: dict[int, list[Action]] = {}
    order: list[int] = []
    for a in acts:
        if a.kind == "set_overwrite":
            cid = a.data["channel_id"]
            if cid not in grouped:
                grouped[cid] = []
                order.append(cid)
            grouped[cid].append(a)
        elif a.kind != "note":
            lines.append(a.text)
    for cid in order:
        group = grouped[cid]
        d = group[0].data
        kind = d.get("kind", "text")
        name = (f"[category] {d['channel']}" if kind == "category"
                else f"{'🔊 ' if kind == 'voice' else '#'}{d['channel']}")
        where = f" (in {d['where']})" if d.get("where") else ""
        parts: list[str] = []
        viewers = [tlabel(a.data["target"]) for a in group
                   if a.data["perms"].get("view_channel") is True and a.data["target"] != BOT]
        hidden = [tlabel(a.data["target"]) for a in group
                  if a.data["perms"].get("view_channel") is False]
        if hidden:
            parts.append("hide from " + ", ".join(hidden))
        if viewers:
            parts.append("visible to " + ", ".join(viewers))
        others = [f"{tlabel(a.data['target'])} {_fmt({k: v for k, v in a.data['perms'].items() if k != 'view_channel'})}"
                  for a in group if any(k != "view_channel" for k in a.data["perms"]) and a.data["target"] != BOT]
        parts += others
        if any(a.data["target"] == BOT for a in group):
            parts.append("bot access")
        tag = "  [STAFF ONLY]" if d.get("mod_only") else ""
        lines.append(f"{name}{where}: " + "; ".join(parts) + tag)
    return lines


def print_plan(title: str, acts: list[Action]) -> None:
    lines = summarize(acts)
    print(f"\n=== {title}: {len(lines)} channel/role change(s), {sum(a.kind != 'note' for a in acts)} permission edits",
          flush=True)
    for line in lines:
        print("  - " + line, flush=True)
    for a in acts:
        if a.kind == "note":
            print("    note: " + a.text, flush=True)


def say(msg: str) -> None:
    print(msg, flush=True)


CONNECT_TIMEOUT_S = 60
CHUNK_TIMEOUT_S = 90


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level="WARNING")
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # hide "voice will NOT be supported" (unused)
    ap = argparse.ArgumentParser(prog="arcbot.setup_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", choices=["new", "lockdown"])
    ap.add_argument("--rollback", type=str)
    args = ap.parse_args(argv)
    token = (os.environ.get("DISCORD_TOKEN") or "").strip()
    gid_text = (os.environ.get("GUILD_ID") or "").strip()
    if not token or not gid_text:
        say(f"DISCORD_TOKEN and GUILD_ID must both be filled in {ROOT / '.env'}")
        return 2
    if not gid_text.isdigit():
        say(f"GUILD_ID should be a long number (right-click the server icon > Copy Server ID), not '{gid_text}'")
        return 2
    cfg = load_config()
    try:
        return asyncio.run(_run(args, cfg, token, int(gid_text)))
    except KeyboardInterrupt:
        say("stopped")
        return 1


async def _run(args, cfg: Config, token: str, gid: int) -> int:  # pragma: no cover
    import discord

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    client = discord.Client(intents=intents, chunk_guilds_at_startup=False)
    ready = asyncio.Event()

    @client.event
    async def on_ready() -> None:
        ready.set()

    say("Connecting to Discord...")
    runner = asyncio.create_task(client.start(token))
    waiter = asyncio.create_task(ready.wait())
    done, _ = await asyncio.wait({runner, waiter}, timeout=CONNECT_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
    try:
        if runner in done:
            exc = runner.exception()
            if isinstance(exc, discord.LoginFailure):
                say("Discord rejected the token. Reset it in the Developer Portal (Bot > Reset Token) and paste the "
                    "new one into .env as DISCORD_TOKEN=... (no quotes, no spaces).")
            elif isinstance(exc, discord.PrivilegedIntentsRequired):
                say("Discord refused the connection: turn ON 'Server Members Intent' (and 'Message Content Intent') "
                    "in the Developer Portal > your app > Bot, click Save, then run this again.")
            else:
                say(f"Could not connect: {type(exc).__name__}: {exc}")
            return 2
        if waiter not in done:
            say(f"No answer from Discord after {CONNECT_TIMEOUT_S}s. Check your internet connection and that the "
                "token in .env is the current one, then try again.")
            return 2
        say(f"Logged in as {client.user}.")
        guild = client.get_guild(gid)
        if guild is None:
            names = ", ".join(f"{g.name} ({g.id})" for g in client.guilds) or "none"
            say(f"The bot is not in server {gid}. Servers it can see: {names}. Fix GUILD_ID in .env or invite the bot.")
            return 2
        say(f"Loading the member list of '{guild.name}'...")
        try:
            members = await asyncio.wait_for(fetch_all_members(guild), timeout=CHUNK_TIMEOUT_S)
        except asyncio.TimeoutError:
            say(f"The member list did not load within {CHUNK_TIMEOUT_S}s. Check your connection and try again.")
            return 2
        except discord.Forbidden:
            say("Discord refused the member list: turn ON 'Server Members Intent' in the Developer Portal > Bot "
                "and click Save, then run this again.")
            return 2
        say(f"Loaded {len(members)} members.")
        return await _work(args, cfg, guild, members)
    except Exception as exc:  # noqa: BLE001
        log.exception("setup failed")
        say(f"setup failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await client.close()
        for t in (runner, waiter):
            t.cancel()


async def _work(args, cfg: Config, guild, members: list) -> int:  # pragma: no cover
    import discord

    if args.rollback:
        path = args.rollback
        if path in ("latest", "latest-lockdown", "latest-new"):
            stage = {"latest": "*", "latest-lockdown": "lockdown", "latest-new": "new"}[path]
            found = sorted((ROOT / "data").glob(f"setup-rollback-{stage}-*.json"), key=lambda p: p.stat().st_mtime)
            if not found:
                say("no rollback files in data/")
                return 2
            path = str(found[-1])
            say(f"Using {path}")
        args.rollback = path
        data = json.loads(open(args.rollback, encoding="utf-8").read())
        if (await asyncio.to_thread(input, f"Roll back {args.rollback} on '{guild.name}'? Type YES: ")).strip().upper() != "YES":
            say("nothing changed")
            return 0
        await rollback(guild, data)
        say("rollback done")
        return 0
    perms = guild.me.guild_permissions
    snap = snapshot(guild, cfg, members)
    if "view_channel" not in snap.bot_perms and "administrator" not in snap.bot_perms:
        say("!! the bot's role has no View Channels permission at server level; give it View Channels first.")
        return 2
    stage_new, stage_lock = plan_new(cfg, snap), plan_lockdown(cfg, snap)
    say(f"Server: {guild.name}  |  members: {len(members)}  |  bot role: {guild.me.top_role.name}")
    bots = [m for m in snap.members if m.bot and not m.is_self]
    if bots:
        keep = ", ".join(tlabel(t) for t in other_bot_roles(cfg, snap)) or "none found"
        say(f"Other bots: {', '.join(b.name for b in bots)}  |  roles kept able to see channels: {keep}")
    can_apply = perms.administrator or (perms.manage_roles and perms.manage_channels)
    if not can_apply:
        say("!! the bot needs Manage Roles + Manage Channels to apply anything (give Manage Channels "
            "temporarily, remove it after).")
    managed = [discord.utils.get(guild.roles, name=n) for n in [cfg.unplaced_role, *[r.role for r in cfg.ranks]]]
    high = [r.name for r in managed if r is not None and r >= guild.me.top_role]
    if high:
        say(f"!! drag the bot's role above: {', '.join(high)} (Server Settings > Roles). Needed to assign ranks.")
    if args.apply is None:
        print_plan("Stage 1: new (safe, adds only)", stage_new)
        print_plan("Stage 2: lockdown (changes who sees existing channels)", stage_lock)
        say("\nNothing changed. Run with --apply new, check it, then --apply lockdown.")
        return 0
    acts = stage_new if args.apply == "new" else stage_lock
    print_plan(f"Applying stage '{args.apply}'", acts)
    if not any(a.kind != "note" for a in acts):
        say("nothing to do")
        return 0
    if not can_apply:
        return 2
    if (await asyncio.to_thread(input, "Type YES to apply exactly this: ")).strip().upper() != "YES":
        say("nothing changed")
        return 0
    path = ROOT / "data" / f"setup-rollback-{args.apply}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    await apply(guild, cfg, acts, path)
    say(f"\ndone. Rollback file: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
