import pytest

from arcbot.ocr import parse, reader
from arcbot.scoring import parse_hours
from tests import synth
from tests.conftest import FIXTURES

needs_tesseract = pytest.mark.skipif(not reader.available(), reason="tesseract not installed")
needs_font = pytest.mark.skipif(not synth.have_font(), reason="no TrueType font for synthetic screens")

A = dict(hours="268:02:47", knockouts=350, squad_revives=219, stranger_revives=43, quests=26,
         containers=14099, expeditions=1)
B = dict(hours="131:18:27", knockouts=31, squad_revives=44, stranger_revives=10, quests=31,
         containers=6381, expeditions=1)


def truth(vals):
    out = dict(vals)
    out["hours"] = parse_hours(vals["hours"])
    return out


def W(text, left, top, w=60, h=20, conf=95, line=(1, 1, 1)):
    return parse.Word(text, conf / 100, left, top, w, h, line)


# ---------------------------------------------------------------- pure parsing
def test_overview_number_above_label():
    words = [
        W("350", 120, 100, w=60, h=40, line=(1, 1, 1)),
        W("PLAYERS", 80, 160, w=70, line=(2, 1, 1)), W("KNOCKED", 155, 160, w=70, line=(2, 1, 1)),
        W("OUT", 230, 160, w=30, line=(2, 1, 1)),
        W("999", 600, 100, w=60, h=40, line=(3, 1, 1)),  # another column's number: too far sideways
    ]
    r = parse.read_field(words, "knockouts")
    assert r.value == 350


def test_list_row_rightmost_value():
    words = [W("Containers", 50, 400, w=110, line=(5, 1, 1)), W("Looted", 165, 400, w=70, line=(5, 1, 1)),
             W("14,099", 1500, 401, w=80, line=(5, 1, 2)), W("Expeditions", 50, 470, w=120, line=(6, 1, 1))]
    assert parse.read_field(words, "containers").value == 14099


def test_split_number_tokens_are_merged():
    words = [W("Containers", 50, 400, w=110, line=(5, 1, 1)), W("Looted", 165, 400, w=70, line=(5, 1, 1)),
             W("14,", 1500, 401, w=30, line=(5, 1, 1)), W("099", 1532, 401, w=40, line=(5, 1, 1))]
    assert parse.read_field(words, "containers").value == 14099


def test_fuzzy_label_tolerates_ocr_noise():
    words = [W("268:02:47", 100, 100, w=150, h=40, line=(1, 1, 1)),
             W("T0TAL", 90, 160, w=50, line=(2, 1, 1)), W("TIME", 145, 160, w=40, line=(2, 1, 1)),
             W("SPENT", 190, 160, w=50, line=(2, 1, 1)), W("TOPSlDE", 245, 160, w=70, line=(2, 1, 1))]
    r = parse.read_field(words, "hours")
    assert r.value == pytest.approx(parse_hours("268:02:47"))


def test_missing_label_means_missing_value():
    assert parse.read_field([W("hello", 1, 1)], "expeditions").value is None


def test_insane_values_are_rejected():
    words = [W("9999999", 100, 100, h=40, line=(1, 1, 1)),
             W("QUESTS", 90, 160, line=(2, 1, 1)), W("COMPLETED", 155, 160, line=(2, 1, 1))]
    assert parse.read_field(words, "quests").value is None


def test_names_match():
    assert parse.names_match("RaiderOne", "RAIDERONE")
    assert parse.names_match("Raider One", "RaiderOne#1234")
    assert not parse.names_match("RaiderOne", "ShadowFox")


def test_combine_disagreement_is_unsure():
    reads = [parse.FieldRead(350, 0.95), parse.FieldRead(850, 0.92)]
    value, conf, sure = reader._combine(reads, 0.8)
    assert value == 350 and not sure


def test_combine_single_low_conf_is_unsure():
    value, conf, sure = reader._combine([parse.FieldRead(12, 0.5)], 0.8)
    assert value == 12 and not sure


# ---------------------------------------------------------- tesseract pipeline
@needs_tesseract
@needs_font
@pytest.mark.parametrize("vals", [A, B], ids=["player_a", "player_b"])
def test_synthetic_clean_capture_reads_every_field(cfg, vals):
    r = reader.read_stats(synth.to_bytes(synth.render(vals)), cfg.ocr)
    t = truth(vals)
    for f in reader.FIELDS:
        assert r.sure(f), (f, r.values[f], r.unsure, r.missing)
        assert r.values[f] == pytest.approx(t[f]), f
    assert r.ingame_name == "RaiderOne"


@needs_tesseract
@needs_font
def test_synthetic_1440p_and_jpeg(cfg):
    img = synth.render(A, width=2560, height=1440)
    r = reader.read_stats(synth.to_bytes(img, "JPEG", quality=85), cfg.ocr)
    t = truth(A)
    for f in reader.FIELDS:
        assert r.values[f] == pytest.approx(t[f]), f


@needs_tesseract
@needs_font
@pytest.mark.parametrize("vals", [A, B], ids=["player_a", "player_b"])
def test_synthetic_phone_photo_never_silently_wrong(cfg, vals):
    r = reader.read_stats(synth.to_bytes(synth.photo_of_screen(synth.render(vals)), "JPEG", quality=80), cfg.ocr)
    t = truth(vals)
    for f in reader.FIELDS:
        if r.sure(f):
            assert r.values[f] == pytest.approx(t[f]), f"{f} read confidently but wrong"


@needs_tesseract
@needs_font
def test_combat_tab_marks_list_fields_missing(cfg):
    r = reader.read_stats(synth.to_bytes(synth.render(A, combat_tab=True)), cfg.ocr)
    assert {"containers", "expeditions"} <= r.missing
    assert r.sure("knockouts")


@needs_tesseract
def test_garbage_image_is_all_missing(cfg):
    r = reader.read_stats(b"not an image", cfg.ocr)
    assert r.missing == set(reader.FIELDS) and r.error


# ---------------------------------------------------------- real screenshots
def _real_fixtures(expected):
    out = []
    for p in expected["players"]:
        path = FIXTURES / "screens" / p["screenshot_file"]
        if path.exists():
            out.append((p, path))
    return out


@needs_tesseract
def test_real_screenshots(cfg, expected):
    fixtures = _real_fixtures(expected)
    if not fixtures:
        pytest.skip("no real screenshots in tests/fixtures/screens/ yet")
    keymap = {"hours": "hours_hms", "knockouts": "knockouts", "squad_revives": "squadmate_revives",
              "stranger_revives": "stranger_revives", "quests": "quests", "containers": "containers",
              "expeditions": "expeditions"}
    for p, path in fixtures:
        r = reader.read_stats(path.read_bytes(), cfg.ocr)
        clean = "clean" in p.get("screenshot_kind", "") or "capture" in p.get("screenshot_kind", "")
        for f, k in keymap.items():
            want = parse_hours(p[k]) if f == "hours" else p[k]
            if f in p.get("missing_fields", []):
                assert not r.sure(f), (p["id"], f)
                continue
            if clean and "photo" not in p.get("screenshot_kind", ""):
                assert r.sure(f) and r.values[f] == pytest.approx(want), (p["id"], f, r.values[f])
            elif r.sure(f):
                assert r.values[f] == pytest.approx(want), (p["id"], f, "confident but wrong")
