# Real screenshots (local only)

Drop real Player Stats captures here to run `tests/test_ocr.py::test_real_screenshots`. They are gitignored
because they show players' in-game names; only their numbers are kept in `../expected_stats.json`.

For each file, add an entry to `expected_stats.json` (copy an existing one) with the true values. Use
`missing_fields` for fields that are not on the captured tab (e.g. the Combat tab has no containers/expeditions).
A good set mixes clean PC captures, console captures, phone photos of a TV, and one deliberately bad image.

Expectations: on clean captures every field reads correctly; on photos a field may come back unsure or missing,
but never confidently wrong.
