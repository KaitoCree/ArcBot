"""Find stat labels in Tesseract word boxes and pick the number that belongs to each.

Overview strip: the number sits above its label, horizontally centered.
Progression & Economy list: the number sits right-aligned on the label's row.
Pure functions over word boxes so they can be unit-tested without Tesseract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..scoring import ParseError, parse_hours, parse_int

# field -> (label phrase, layout)
LABELS: dict[str, tuple[str, str]] = {
    "hours": ("TOTAL TIME SPENT TOPSIDE", "above"),
    "knockouts": ("PLAYERS KNOCKED OUT", "above"),
    "squad_revives": ("TIMES REVIVED A SQUADMATE", "above"),
    "stranger_revives": ("TIMES REVIVED A STRANGER", "above"),
    "quests": ("QUESTS COMPLETED", "above"),
    "containers": ("CONTAINERS LOOTED", "row"),
    "expeditions": ("EXPEDITIONS COMPLETED", "row"),
}

# generous sanity limits; anything beyond is treated as a misread, not a value
SANITY_MAX = {"hours": 20000, "knockouts": 200000, "squad_revives": 200000, "stranger_revives": 200000,
              "quests": 2000, "containers": 5_000_000, "expeditions": 100}

_NUM = re.compile(r"^\d{1,3}(?:[,.]\d{3})+$|^\d+$")
_HMS = re.compile(r"^\d[\d,]*:\d{2}:\d{2}$")


@dataclass
class Word:
    text: str
    conf: float  # 0..1
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, int, int]

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def cx(self) -> float:
        return self.left + self.width / 2

    @property
    def cy(self) -> float:
        return self.top + self.height / 2


@dataclass
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class FieldRead:
    value: float | int | None
    conf: float
    raw: str = ""


def words_from_data(data: dict) -> list[Word]:
    out: list[Word] = []
    for i, text in enumerate(data["text"]):
        t = (text or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < 0:
            continue
        out.append(Word(t, conf / 100.0, int(data["left"][i]), int(data["top"][i]), int(data["width"][i]),
                        int(data["height"][i]),
                        (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9 ]", "", s.upper()).strip()


def _lines(words: list[Word]) -> list[list[Word]]:
    groups: dict[tuple[int, int, int], list[Word]] = {}
    for w in words:
        groups.setdefault(w.line, []).append(w)
    return [sorted(g, key=lambda w: w.left) for g in groups.values()]


def find_label(words: list[Word], phrase: str, min_ratio: float = 0.8) -> Box | None:
    target = _norm(phrase)
    n = len(target.split())
    best: tuple[float, Box] | None = None
    for line in _lines(words):
        for size in {max(1, n - 1), n, n + 1}:
            for i in range(0, max(1, len(line) - size + 1)):
                seq = line[i:i + size]
                if not seq:
                    continue
                text = _norm(" ".join(w.text for w in seq))
                ratio = SequenceMatcher(None, text, target).ratio()
                if ratio >= min_ratio and (best is None or ratio > best[0]):
                    box = Box(min(w.left for w in seq), min(w.top for w in seq),
                              max(w.right for w in seq), max(w.bottom for w in seq))
                    best = (ratio, box)
    return best[1] if best else None


def _clean_token(t: str) -> str:
    t = t.strip().strip("|[](){}'\"`")
    # common OCR confusions inside numbers
    return t.replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1").replace("S", "5") \
        if re.search(r"\d", t) else t


def _parse(field: str, raw: str) -> float | int | None:
    t = _clean_token(raw)
    try:
        if field == "hours":
            if not _HMS.match(t):
                return None
            v: float | int = parse_hours(t)
        else:
            if not _NUM.match(t):
                return None
            v = parse_int(t)
    except ParseError:
        return None
    if v < 0 or v > SANITY_MAX[field]:
        return None
    return v


def _merge_numeric_neighbours(words: list[Word]) -> list[Word]:
    """Join tokens like '14' ',' '099' or '268:02' ':47' that Tesseract split on one line."""
    out: list[Word] = []
    for line in _lines(words):
        cur: Word | None = None
        for w in line:
            if cur is not None and re.fullmatch(r"[\d,.:]+", cur.text) and re.fullmatch(r"[\d,.:]+", w.text) \
                    and w.left - cur.right <= max(cur.height, w.height) * 0.6:
                cur = Word(cur.text + w.text, min(cur.conf, w.conf), cur.left, min(cur.top, w.top),
                           w.right - cur.left, max(cur.bottom, w.bottom) - min(cur.top, w.top), cur.line)
            else:
                if cur is not None:
                    out.append(cur)
                cur = w
        if cur is not None:
            out.append(cur)
    return out


def list_right_limit(words: list[Word], label: Box) -> float | None:
    """Left edge of the Round History panel, which sits to the right of the stats list. List values never
    lie beyond it, and its numbers (loot values, timers) must never be read as list values."""
    hdr = find_label(words, "ROUND HISTORY")
    if hdr is not None and hdr.left > label.right:
        return hdr.left - label.height
    # header unreadable (dark text on a light bar after some binarisations): list rows start at the same left
    # edge as this label and their values are right-aligned, so the typical right edge of the first number on
    # each row marks the list's right edge
    lh = max(label.height, 1)
    nums = [w for w in _merge_numeric_neighbours(words) if re.search(r"\d", w.text)]
    rights: list[float] = []
    for row_start in words:
        if abs(row_start.left - label.left) > lh * 1.5 or not re.search(r"[A-Za-z]{3}", row_start.text):
            continue
        on_row = [n for n in nums if n.left > row_start.right + lh and abs(n.cy - row_start.cy) <= lh * 0.8]
        if on_row:
            rights.append(min(on_row, key=lambda n: n.left).right)
    if len(rights) >= 2:
        rights.sort()
        return rights[len(rights) // 2] + lh * 1.5
    return None


def read_field(words: list[Word], field: str) -> FieldRead:
    phrase, layout = LABELS[field]
    if layout == "above":
        col = overview_columns(words).get(field)
        if col is not None:
            r = _number_above(words, field, col)
            if r.value is not None:
                return r
    label = find_label(words, phrase)
    if label is None:
        return FieldRead(None, 0.0)
    merged = _merge_numeric_neighbours(words)
    lh = max(label.height, 1)
    limit = list_right_limit(words, label) if layout == "row" else None
    candidates: list[tuple[float, Word]] = []
    for w in merged:
        if field != "hours" and not re.search(r"\d", w.text):
            continue
        if layout == "above":
            if w.bottom > label.top + lh * 0.3:
                continue
            gap = label.top - w.bottom
            if gap > lh * 6:
                continue
            if abs(w.cx - label.cx) > max(label.width * 0.6, w.width):
                continue
            candidates.append((gap + abs(w.cx - label.cx) * 0.2, w))
        else:  # row
            if w.left < label.right:
                continue
            if limit is not None and w.right > limit:
                continue
            if abs(w.cy - label.cy) > lh * 0.8:
                continue
            candidates.append((-w.right, w))  # rightmost first
    for _, w in sorted(candidates, key=lambda c: c[0]):
        v = _parse(field, w.text)
        if v is not None:
            return FieldRead(v, w.conf, w.text)
    return FieldRead(None, 0.0)


# ---------------------------------------------------------------- Overview strip (real game layout)
# The five labels wrap onto two lines ("PLAYERS KNOCKED / OUT", "TIMES REVIVED A / SQUADMATE", ...), the two revive
# labels share their first line, and the big numbers sit far above them. So each column is located by its
# distinctive word, and its number is the numeric token above that column.
ANCHORS: dict[str, tuple[str, ...]] = {
    "knockouts": ("KNOCKED", "OUT"),
    "squad_revives": ("SQUADMATE",),
    "stranger_revives": ("STRANGER",),
    "hours": ("TOPSIDE",),
    "quests": ("QUESTS",),
}


@dataclass
class Column:
    cx: float
    band_top: float  # top of the label block
    label_h: float


def _fuzzy_word(words: list[Word], target: str, min_ratio: float = 0.75) -> list[Word]:
    out = []
    for w in words:
        t = _norm(w.text)
        if t and SequenceMatcher(None, t, target).ratio() >= min_ratio:
            out.append(w)
    return out


def overview_columns(words: list[Word]) -> dict[str, Column]:
    """Column centre for each Overview field, from the label words."""
    strong: dict[str, Word] = {}
    for field in ("squad_revives", "stranger_revives", "hours", "quests"):
        hits = _fuzzy_word(words, ANCHORS[field][0])
        if hits:
            strong[field] = max(hits, key=lambda w: w.conf)
    if not strong:
        return {}
    # the label band: where the distinctive words sit (all on roughly the same rows)
    band_y = sorted(w.cy for w in strong.values())[len(strong) // 2]
    lh = sorted(w.height for w in strong.values())[len(strong) // 2]

    def in_band(w: Word) -> bool:
        return abs(w.cy - band_y) <= lh * 2.5

    cols: dict[str, Column] = {}
    for field, w in strong.items():
        if not in_band(w):
            continue
        cx = w.cx
        if field == "quests":  # single line "QUESTS COMPLETED": centre between both words when available
            comp = [c for c in _fuzzy_word(words, "COMPLETED") if in_band(c) and c.left > w.left
                    and c.left - w.right < lh * 3]
            if comp:
                cx = (w.left + max(comp, key=lambda c: c.conf).right) / 2
        cols[field] = Column(cx, w.top - lh * 1.8, lh)
    # knockouts: "PLAYERS KNOCKED / OUT" -> prefer the centred second-line "OUT" under KNOCKED
    knocked = [w for w in _fuzzy_word(words, "KNOCKED") if in_band(w)]
    if knocked:
        k = max(knocked, key=lambda w: w.conf)
        outs = [w for w in words if _norm(w.text) == "OUT" and in_band(w) and w.top > k.top
                and abs(w.cx - k.left) < k.width * 2]
        players = [w for w in _fuzzy_word(words, "PLAYERS") if abs(w.cy - k.cy) < lh and w.right <= k.left + lh]
        if outs:
            cx = min(outs, key=lambda w: abs(w.cy - k.cy)).cx
        elif players:
            cx = (max(players, key=lambda w: w.right).left + k.right) / 2
        else:
            cx = k.cx
        cols["knockouts"] = Column(cx, k.top - lh * 0.5, lh)
    return cols


OVERVIEW_ORDER = ("knockouts", "squad_revives", "stranger_revives", "hours", "quests")


def fill_columns(cols: dict[str, Column]) -> dict[str, Column]:
    """The five Overview columns are evenly spaced in a fixed order: place missing ones from >= 2 found ones
    (handles labels a phone photo smeared beyond recognition)."""
    known = [(OVERVIEW_ORDER.index(f), c) for f, c in cols.items() if f in OVERVIEW_ORDER]
    if len(known) < 2:
        return dict(cols)
    n = len(known)
    mi = sum(i for i, _ in known) / n
    mx = sum(c.cx for _, c in known) / n
    var = sum((i - mi) ** 2 for i, _ in known)
    if var == 0:
        return dict(cols)
    slope = sum((i - mi) * (c.cx - mx) for i, c in known) / var
    if slope <= 0:
        return dict(cols)
    band = sorted(c.band_top for _, c in known)[n // 2]
    lh = sorted(c.label_h for _, c in known)[n // 2]
    out = dict(cols)
    for i, f in enumerate(OVERVIEW_ORDER):
        if f not in out:
            out[f] = Column(mx + slope * (i - mi), band, lh)
    return out


def column_spacing(cols: dict[str, Column]) -> float | None:
    xs = sorted(c.cx for c in cols.values())
    gaps = [b - a for a, b in zip(xs, xs[1:]) if b - a > 0]
    return min(gaps) if gaps else None


def _number_above(words: list[Word], field: str, col: Column) -> FieldRead:
    merged = _merge_numeric_neighbours(words)
    xs = sorted(c.cx for c in overview_columns(words).values())
    spacing = min((b - a for a, b in zip(xs, xs[1:])), default=col.label_h * 12)
    best: tuple[float, Word] | None = None
    for w in merged:
        if not re.search(r"\d", w.text) or w.bottom > col.band_top + col.label_h:
            continue
        gap = col.band_top - w.bottom
        if gap > col.label_h * 16:  # numbers sit well above the labels, but not in the header bar
            continue
        dx = abs(w.cx - col.cx)
        if dx > spacing * 0.45:
            continue
        if _parse(field, w.text) is None:
            continue
        score = dx + gap * 0.05
        if best is None or score < best[0]:
            best = (score, w)
    if best is None:
        return FieldRead(None, 0.0)
    w = best[1]
    return FieldRead(_parse(field, w.text), w.conf, w.text)


def read_name(words: list[Word], width: int, height: int) -> tuple[str | None, float]:
    """In-game name from the top-right corner, if visible."""
    cands = [w for w in words if w.top < height * 0.12 and w.left > width * 0.6
             and re.search(r"[A-Za-z]", w.text) and len(w.text) >= 3]
    if not cands:
        return None, 0.0
    best = max(cands, key=lambda w: (w.conf, len(w.text)))
    return best.text.strip(), best.conf


def names_match(typed: str, read: str) -> bool:
    a, b = _norm(typed).replace(" ", ""), _norm(read).replace(" ", "")
    if not a or not b:
        return True
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.75
