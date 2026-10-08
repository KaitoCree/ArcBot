# Discord setup

## Developer Portal → Bot
- **Server Members Intent:** on (join handling, role sync)
- **Message Content Intent:** on (only used to read `#vouch`)
- Presence Intent: off

## Invite
Scopes: `bot`, `applications.commands`.

Permissions (no Administrator): View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files,
Read Message History, Add Reactions, Manage Roles, Create Public Threads (job threads), Pin Messages (button
panels and the timer message; everything works unpinned).

## Role order
Drag the bot's role above Green Horn, Scavenger, Pathfinder, Vanguard, Veteran and Newcomer. Discord won't let a
bot assign a role at or above its own, and a bot can't move its own role.

## Roles and channels
- Role **Newcomer** (no permissions).
- Channels **#apply**, **#rank-promotion**, **#mod-review** (private).
- Existing channels the bot uses: **#vouch**, **#job-board**, **#event-timers**, and a chat channel for rank-up
  announcements (`channels` in `config/arcbot.yaml`). Emoji or punctuation around a name is ignored, so
  `✅vouch` matches `vouch`.
- Mod roles: `roles.mods` in the config (only these see mod commands and `#mod-review`).

### Intended visibility

| Channel | @everyone | Newcomer | Rank roles | Mods |
|---|---|---|---|---|
| #apply | hidden | view, no send (buttons work) | hidden | view |
| #rules | hidden | view, no send | view | view |
| #rank-promotion | hidden | hidden | view, no send (buttons work) | view |
| #mod-review | hidden | hidden | hidden | view + send |
| everything else | hidden | hidden | view | view |

## Setup tool

`setup_server` creates the above from a printed plan. Run it from a machine with the project and a `.env`:

```bash
python -m arcbot.setup_server                       # print the plan for both stages, change nothing
python -m arcbot.setup_server --apply new           # Newcomer role, #apply, #rank-promotion, #mod-review, bot access
python -m arcbot.setup_server --apply lockdown      # hide public channels from @everyone, open them to ranks
python -m arcbot.setup_server --rollback latest-lockdown
```

- Each stage shows exactly what it will change and asks for confirmation.
- `new` only adds. `lockdown` leaves channels that are already private alone, keeps any role that was
  deliberately denied a channel, keeps other bots' access, makes `setup.mod_only` categories staff-only, lets
  Newcomers read `setup.newcomer_visible`, and gives Newcomer to members with no rank role so nobody is locked out.
- Every edit is written to a rollback file as it happens (`data/setup-rollback-*.json`). Nothing is deleted.
- The bot role needs Manage Roles and, while the tool runs, Manage Channels.

The running bot never changes channel permissions itself; it only manages rank roles and `Newcomer`.

## Existing members
`/admin import-existing` shows a dry run of every member with a rank role and the rank they'll be recorded at,
then imports on confirmation (no role changes). Members who skip this are picked up automatically the first time
they use a button, vouch or the job board.

## Sanity check
`/admin setup-check` reports roles, channels, role order, permissions and intents. Fix what it flags and re-run.
