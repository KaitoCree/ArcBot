# Scoring

Stats come from **Raider → Codex → Player Stats → Progression & Economy**: the five Overview numbers along the
top (players knocked out, times revived a squadmate, times revived a stranger, total time spent topside, quests
completed) and two list rows below (containers looted, expeditions completed).

## Rating (0–100)

Configured under `stats_rating` in `config/arcbot.yaml`. Each metric contributes
`max_points × curve(min(value / cap, 1))`, with `linear` or `sqrt` curves.

| Metric | Points | How |
|---|---|---|
| Hours topside | 40 | linear, cap 300 h |
| Playstyle | 53 | revives (cap 600), knockouts (cap 800), containers (cap 15,000), each sqrt; half from the strongest of the three, half from their average |
| Quests completed | 5 | linear, cap 40 |
| Expeditions completed | 2 | sqrt, cap 2 (1 → +1.4, 2+ → +2) |

The playstyle blend exists because knockouts and revives depend heavily on how someone plays: PvE players can have
a handful of knockouts after hundreds of hours, solo players have no squadmate revives, while almost everyone
loots. Scoring the strongest of the three plus the average keeps medics, fighters and solo looters with equal
hours within a few points of each other (enforced by `tests/test_scoring.py`).

Expeditions are a small bonus, never a requirement. An expedition resets the "Quests Completed" counter along with
quest progress, which is one reason quests carry only 5 points (the expedition bonus roughly offsets the loss).

## Rating → rank

`bands`: Green Horn 0, Scavenger 20, Pathfinder 40, Vanguard 60, Veteran 90.
Results above Pathfinder, or anything that trips a plausibility check, go to `#mod-review`.

## Plausibility checks

Any hit sends the claim to a mod (never an automatic rejection): hours, quests or expeditions above plausible
maximums; knockouts, containers or revives per hour too high; tiny hours with big numbers; a typed in-game name
that doesn't match the screenshot; and zero knockouts and zero revives after many hours (a known Player Stats
display issue can show zeros in the Overview strip).

## Reference data

Steam's public achievement percentages (all owners) show how skewed the raw stats are: ~79% have knocked out at
least one raider and ~50% at least ten; ~41% have revived a squadmate ten times; ~80% have searched 50 containers.
The game currently has 100 quests.
