import json
from pathlib import Path

import pytest

from arcbot.cogs.trials import build_embed, needs_fetch, valid_trials

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "weekly_trials.json").read_text())
MAPS = {1538747736: "Pendola Pass"}


def test_parses_this_week_and_next_week():
    t = valid_trials(FIXTURE)
    assert [x["name"] for x in t["active"]] == [
        "Damage ARC enemies using a Stiletto", "Damage flying ARC enemies using Light Impact Grenades",
        "Destroy Skulkers"]  # MetaForge's sort order; past trials are dropped
    assert [x["name"] for x in t["upcoming"]] == ["Damage Bullies", "Damage ARC using a Bantam"]
    assert t["ends"] == FIXTURE["activeWindowEnd"] and t["next"] == FIXTURE["nextWindowStart"]


def test_embed_links_guides_names_maps_and_shows_reset():
    e = build_embed(valid_trials(FIXTURE), MAPS, "Data: MetaForge")
    this_week, next_week = e.fields
    assert f"<t:{FIXTURE['activeWindowEnd']}:R>" in this_week.name
    assert "[Destroy Skulkers](https://metaforge.app/arc-raiders/destroy-skulkers) · Pendola Pass" in this_week.value
    assert "**Damage Bullies** · Pendola Pass" in next_week.value  # no guide yet: plain bold name
    assert "· Pendola Pass" not in this_week.value.splitlines()[0]  # any-map trials name no map
    assert f"<t:{FIXTURE['nextWindowStart']}:f>" in next_week.name
    assert "metaforge.app/arc-raiders/weekly-trials" in e.footer.text


def test_unknown_map_ids_are_left_out():
    e = build_embed(valid_trials(FIXTURE), {}, "Data: MetaForge")
    assert "Pendola" not in e.fields[0].value and "**" not in e.fields[0].value.splitlines()[2].split("·")[-1]


def test_empty_week_and_bad_payload():
    e = build_embed(valid_trials({"data": []}), MAPS, "x")
    assert len(e.fields) == 1 and "No trials" in e.fields[0].value
    with pytest.raises(ValueError):
        valid_trials({"oops": 1})


def test_fetches_only_when_needed():
    t = valid_trials(FIXTURE)
    end = t["ends"]
    assert needs_fetch(None, 0, end - 100, 6)
    assert not needs_fetch(t, end - 3600, end - 100, 6)      # cached and still this week
    assert needs_fetch(t, end - 3600, end + 1, 6)            # the week rolled over
    assert needs_fetch(t, end - 30000, end - 100, 6)         # cache older than refresh_hours
