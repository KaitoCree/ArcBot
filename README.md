# arcbot

A Discord bot for **The Raider's Outpost**, an ARC Raiders community server. It places new members into a guild
rank from their in-game Player Stats, tracks guild activity through hidden points, runs a rank-gated job board,
and gives mods the tools to keep it all fair. It also posts live event timers.

Runs on a single small Linux box, costs nothing to operate (no paid APIs, no AI calls at runtime), and looks after
itself: restarts on crash, nightly backups, self-checks on startup.

## Features

**Onboarding (`#apply`)**
- New members land with a `Newcomer` role and only see `#apply` and `#rules`, with a self-deleting welcome ping.
- Three buttons: upload a Player Stats screenshot, type the numbers in, or skip (Green Horn, no waiting).
- Screenshots are read locally with Tesseract OCR. The member sees a read-back to confirm or fix; anything the
  reader couldn't make out is asked for by name, never guessed.
- Members who leave and rejoin get their rank back.

**Ranks and hidden points**
- One cumulative points total per member, never shown to members. Ranks only ever go up.
- Ranks: Green Horn, Scavenger, Pathfinder, Vanguard (earnable), Veteran (stats + mod approval only).
- Stats placement seeds points to the rank's floor, so later activity builds on it without repeat rank-ups.
- Rank-ups are announced without numbers.

**Promotion (`#rank-promotion`)**
- Members resubmit stats to move up; one submission per 30 days (7 if the last result changed nothing).

**Mod review (`#mod-review`)**
- Vanguard/Veteran claims and anything implausible go to mods with the numbers, a rating breakdown and the
  screenshot. Approve / Set rank / Deny buttons; the member holds a provisional rank meanwhile.

**Vouches (`#vouch`)**
- `@member` + a few words, `/vouch`, or right-click → Apps → *Vouch for this member*.
- Anti-farming: same pair once per 24 h, a member receives at most 3 counted vouches a day, a voucher counts
  5 a day. Every valid vouch gets the same reaction, counted or not.

**Job board (`#job-board`)**
- *Post a job* with a "who can complete this" tier. Listings are screened against a flag list (real-money
  trading, account selling, paid carries, cheats, scams), a profanity filter, links and posting-rate caps.
  Serious hits wait for a mod; softer trade words post immediately with a heads-up to mods.
- Several raiders can tap *I'm attempting* on one job; each is added to the job's private thread with the poster.
  The first to tap *Mark complete* claims it, and the board shows *Pending completion* so others can decide
  whether it's still worth their time (joining then asks for a confirm). The poster confirms (or sends it back
  with *Not done yet*); only the raider who claimed it earns points (with caps).
- After completion the thread stays up until the poster vouches for that raider (a button in the thread opens the
  vouch form; any vouch in #vouch counts too), or `vouch_wait_days` pass. Then it's deleted (or archived).
- Guild Master only: the Post a job form has a *Reward boost* pick (`points.job_xp_boosts`) for special posts.
  Nobody else sees it. The board marks the post as a special job without showing any numbers.
- Check-ins on day 3 and 10; confirmations that stall go to mods instead of expiring.

**Mod tools** (slash commands, private replies, mod roles only)
`/leaderboard`, `/profile`, `/queue`, `/award`, `/revoke-event`, `/set-rank`, `/admin setup-check`,
`/admin import-existing`, `/admin export`, `/admin backup-now`.

**Event timers (`#event-timers`)**
- One pinned embed with active and upcoming events from MetaForge's public API, updated when the schedule
  changes.

## Project layout

```
arcbot/
  __main__.py         entry point (python -m arcbot, --check)
  bot.py app.py       discord client, shared state, role sync
  config.py copytext.py   config/arcbot.yaml and config/copy.yaml loaders
  db.py migrations/   SQLite (WAL) with numbered migrations
  ranks.py engine.py  rank table and the points/rank engine
  scoring.py intake.py    stats -> rating -> rank, submission and review decisions
  vouching.py jobs.py     vouch and job-board rules
  ocr/                screenshot reading (preprocess, layout parsing, Tesseract)
  cogs/               Discord features: onboarding, promotion, vouch, jobs, modtools, timers
  services/           backup.py, keepalive.py
  setup_server.py     one-time Discord channel/permission setup tool
config/               arcbot.yaml (all tunables), copy.yaml (all member-facing text), flag_list.json
deploy/               install.sh, systemd units, backup.sh
docs/                 DEPLOY.md, DISCORD_SETUP.md, SCORING.md
tests/                pytest suite
```

Everything tunable lives in `config/arcbot.yaml` and every member-facing sentence in `config/copy.yaml`;
the code reads them at startup and refuses to start with a readable error if they're invalid.

## Running locally

Python 3.12+, and Tesseract for screenshot reading (optional locally; without it the bot falls back to manual
entry).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
cp .env.example .env                               # fill in DISCORD_TOKEN and GUILD_ID
python -m arcbot --check                           # validates config, copy, database, Tesseract
python -m arcbot                                   # run
```

`ARCBOT_DRY_RUN=1` in `.env` logs every role change and post instead of making it.

## Tests

```bash
pytest
```

Covers the rank engine, scoring (including fairness across playstyles), intake and review decisions, vouch and
job rules, OCR parsing (synthetic screens, plus real captures in `tests/fixtures/screens/` when present),
backups, the keepalive, the setup planner, and an end-to-end check that no member-facing message ever contains
point values.

## Deploying

See [docs/DEPLOY.md](docs/DEPLOY.md) (Oracle Cloud Always Free, Oracle Linux 9) and
[docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md). Scoring details: [docs/SCORING.md](docs/SCORING.md).

## Credits

Event data from [MetaForge](https://metaforge.app/arc-raiders). ARC Raiders is a trademark of Embark Studios;
this project is a fan-made community tool.
