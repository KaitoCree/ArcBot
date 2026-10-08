"""Run Tesseract over several preprocessed variants and combine per-field reads.

A field is "sure" only when its best read clears ocr.min_field_confidence and no other confident variant
disagrees. Anything else comes back unsure (value kept as a hint) or missing. Never guess.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import parse, preprocess

log = logging.getLogger("arcbot.ocr")

FIELDS = tuple(parse.LABELS)
_WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


@dataclass
class OcrResult:
    values: dict[str, float | int | None] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    unsure: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    ingame_name: str | None = None
    name_conf: float = 0.0
    available: bool = True  # False when Tesseract is not installed
    error: str | None = None

    def sure(self, f: str) -> bool:
        return f not in self.unsure and f not in self.missing and self.values.get(f) is not None


def tesseract_cmd() -> str | None:
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd and _WINDOWS_DEFAULT.exists():
        cmd = str(_WINDOWS_DEFAULT)
    return cmd


def available() -> bool:
    return tesseract_cmd() is not None


def _ocr_words(img: Any) -> list[parse.Word]:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd()
    data = pytesseract.image_to_data(img, config="--oem 1 --psm 11", output_type=pytesseract.Output.DICT)
    return parse.words_from_data(data)


def _rescue(img: Any, words: list[parse.Word], f: str) -> parse.FieldRead | None:
    """Re-read only the strip where the value must be, as a single line of digits.

    Full-page OCR tends to drop lone short tokens such as a single "1"."""
    import cv2
    import pytesseract

    phrase, layout = parse.LABELS[f]
    label = parse.find_label(words, phrase)
    if label is None:
        return None
    h, w = img.shape[:2]
    lh = max(label.height, 1)
    if layout == "row":
        limit = parse.list_right_limit(words, label)
        x0, x1 = label.right + lh, int(limit) if limit is not None else w
        y0, y1 = label.top - lh // 2, label.bottom + lh // 2
    else:
        x0, x1 = label.left - label.width // 3, label.right + label.width // 3
        y0, y1 = label.top - lh * 6, label.top - lh // 4
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None
    base = img[y0:y1, x0:x1]
    if layout == "row":
        # a lone short value ("4") at the far right: keep only the right end of the row and enlarge it,
        # older Tesseract (4.1, the server's) drops single small digits otherwise
        base = base[:, int(base.shape[1] * 0.6):]
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd()
    attempts = []
    for scale in (1, 3):
        im = base if scale == 1 else cv2.resize(base, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        im = cv2.copyMakeBorder(im, 20 * scale, 20 * scale, 20 * scale, 20 * scale, cv2.BORDER_REPLICATE)
        attempts += [(im, 7), (im, 8), (im, 13)]
        if layout == "row" and f != "hours":
            attempts.append((im, 10))  # single character
    for im, psm in attempts:
        data = pytesseract.image_to_data(
            im, config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist=0123456789,.:",
            output_type=pytesseract.Output.DICT)
        toks = [t for t in parse.words_from_data(data) if t.text]
        if f != "hours" and layout == "row":
            toks = sorted(toks, key=lambda t: -t.right)[:1]  # rightmost value on the row
        text = "".join(t.text for t in toks)
        value = parse._parse(f, text) if text else None
        if value is not None:
            return parse.FieldRead(value, min(t.conf for t in toks), text)
    return None


def _column_binarizations(crop: Any):
    """Several ways to separate glowing/blurred big digits from the background (phone photos of a TV)."""
    import cv2
    import numpy as np

    yield cv2.threshold(cv2.bitwise_not(crop), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    for t in (170, 200):
        yield cv2.bitwise_not(cv2.threshold(crop, t, 255, cv2.THRESH_BINARY)[1])
    th = cv2.morphologyEx(crop, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (61, 61)))
    yield cv2.bitwise_not(cv2.threshold(th, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
    yield cv2.bitwise_not(cv2.erode(cv2.threshold(crop, 200, 255, cv2.THRESH_BINARY)[1], np.ones((5, 5), np.uint8)))


def _column_reads(gray: Any, words: list[parse.Word], fields: list[str]) -> dict[str, list[parse.FieldRead]]:
    """Crop each Overview column's number box and read it as one line of digits, several ways.

    Column positions come from the label words; columns whose labels were unreadable are placed from the
    fixed, evenly spaced layout."""
    import cv2
    import pytesseract

    out: dict[str, list[parse.FieldRead]] = {f: [] for f in fields}
    cols = parse.fill_columns(parse.overview_columns(words))
    spacing = parse.column_spacing(cols)
    if not cols or not spacing:
        return out
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd()
    h, w = gray.shape[:2]
    for f in fields:
        col = cols.get(f)
        if col is None:
            continue
        x0, x1 = int(col.cx - spacing * 0.45), int(col.cx + spacing * 0.45)
        y0, y1 = int(col.band_top - col.label_h * 7.5), int(col.band_top - col.label_h * 0.3)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if x1 - x0 < 10 or y1 - y0 < 10:
            continue
        crop = gray[y0:y1, x0:x1]
        for b in _column_binarizations(crop):
            b = cv2.copyMakeBorder(b, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
            data = pytesseract.image_to_data(
                b, config="--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789,:",
                output_type=pytesseract.Output.DICT)
            toks = parse.words_from_data(data)
            text = "".join(t.text for t in toks)
            value = parse._parse(f, text) if text else None
            if value is not None:
                out[f].append(parse.FieldRead(value, min(t.conf for t in toks), text))
    return out


def read_stats(image_bytes: bytes, ocr_cfg: dict[str, Any]) -> OcrResult:
    res = OcrResult()
    if not available():
        res.available = False
        res.missing = set(FIELDS)
        return res
    try:
        img = preprocess.downscale(preprocess.decode(image_bytes), int(ocr_cfg.get("max_side_px", 3000)))
    except preprocess.ImageError as exc:
        res.error = str(exc)
        res.missing = set(FIELDS)
        return res

    min_conf = float(ocr_cfg.get("min_field_confidence", 0.8))
    reads: dict[str, list[parse.FieldRead]] = {f: [] for f in FIELDS}
    names: list[tuple[str, float]] = []
    seen: list[tuple[Any, list[parse.Word]]] = []
    orig_words: list[tuple[preprocess.Variant, list[parse.Word]]] = []
    for v in preprocess.variants(img):
        try:
            words = _ocr_words(v.image)
        except Exception as exc:  # noqa: BLE001
            log.warning("tesseract failed on %s: %s", v.name, exc)
            continue
        seen.append((v.image, words))
        if v.name.startswith("orig"):
            orig_words.append((v, words))
        for f in FIELDS:
            r = parse.read_field(words, f)
            if r.value is not None:
                reads[f].append(r)
        n, c = parse.read_name(words, v.image.shape[1], v.image.shape[0])
        if n:
            names.append((n, c))
        # stop early once every field is confidently read and agreed by two variants
        if all(_agreed(reads[f], min_conf) for f in FIELDS):
            break

    # big Overview numbers that aren't settled yet: per-column crops of the unwarped image
    overview = [f for f in parse.OVERVIEW_ORDER if not _agreed(reads[f], min_conf)]
    if overview and orig_words:
        import cv2

        v, words = max(orig_words, key=lambda vw: len(parse.overview_columns(vw[1])))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if v.scale != 1.0:
            gray = cv2.resize(gray, (v.image.shape[1], v.image.shape[0]), interpolation=cv2.INTER_CUBIC)
        try:
            for f, extra in _column_reads(gray, words, overview).items():
                reads[f].extend(extra)
        except Exception as exc:  # noqa: BLE001
            log.warning("column re-read failed: %s", exc)

    for f in FIELDS:
        if reads[f]:
            continue
        for vimg, words in seen[:3]:
            try:
                r = _rescue(vimg, words, f)
            except Exception as exc:  # noqa: BLE001
                log.warning("rescue read failed for %s: %s", f, exc)
                r = None
            if r is not None:
                reads[f].append(r)

    for f in FIELDS:
        value, conf, sure = _combine(reads[f], min_conf)
        res.values[f] = value
        res.confidence[f] = conf
        if value is None:
            res.missing.add(f)
        elif not sure:
            res.unsure.add(f)
    if names:
        res.ingame_name, res.name_conf = max(names, key=lambda t: t[1])
    return res


def _agreed(reads: list[parse.FieldRead], min_conf: float) -> bool:
    good = [r for r in reads if r.conf >= min_conf]
    return len(good) >= 2 and len({_key(r.value) for r in good}) == 1


def _key(v: float | int | None) -> Any:
    return round(v, 3) if isinstance(v, float) else v


def _combine(reads: list[parse.FieldRead], min_conf: float) -> tuple[float | int | None, float, bool]:
    if not reads:
        return None, 0.0, False
    best = max(reads, key=lambda r: r.conf)
    confident = [r for r in reads if r.conf >= min_conf]
    disagree = len({_key(r.value) for r in confident}) > 1
    support = sum(1 for r in reads if _key(r.value) == _key(best.value))
    sure = best.conf >= min_conf and not disagree
    # three or more agreeing variants just under the bar count as sure; a lone weak read never does
    if not sure and support >= 3 and not disagree and best.conf >= min_conf * 0.85:
        sure = True
    return best.value, best.conf, sure
