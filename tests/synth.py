"""Synthetic Player Stats screens for OCR tests until real screenshots are added.

Mimics the in-game layout (dark UI, light text): Overview numbers above their labels,
Progression & Economy rows with right-aligned values, in-game name top-right. This proves the
pipeline; real-capture accuracy still needs tests/fixtures/screens/.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont | None:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return None


def have_font() -> bool:
    return font(10) is not None


def render(values: dict, name: str = "RaiderOne", *, width: int = 1920, height: int = 1080,
           combat_tab: bool = False) -> Image.Image:
    img = Image.new("RGB", (width, height), (18, 20, 24))
    d = ImageDraw.Draw(img)
    big, small, row = font(int(height * 0.045)), font(int(height * 0.018)), font(int(height * 0.024))
    d.text((width * 0.04, height * 0.04), "PLAYER STATS", fill=(230, 230, 230), font=row)
    d.text((width * 0.80, height * 0.04), name, fill=(235, 235, 235), font=row)
    overview = [
        ("hours", "TOTAL TIME SPENT TOPSIDE"), ("knockouts", "PLAYERS KNOCKED OUT"),
        ("squad_revives", "TIMES REVIVED A SQUADMATE"), ("stranger_revives", "TIMES REVIVED A STRANGER"),
        ("quests", "QUESTS COMPLETED"),
    ]
    col_w = width * 0.9 / len(overview)
    for i, (key, label) in enumerate(overview):
        cx = width * 0.05 + col_w * (i + 0.5)
        v = values[key]
        text = v if isinstance(v, str) else f"{v:,}"
        tw = d.textlength(text, font=big)
        d.text((cx - tw / 2, height * 0.17), text, fill=(245, 245, 245), font=big)
        lw = d.textlength(label, font=small)
        d.text((cx - lw / 2, height * 0.25), label, fill=(170, 175, 180), font=small)
    tab = "COMBAT" if combat_tab else "PROGRESSION & ECONOMY"
    d.text((width * 0.05, height * 0.36), tab, fill=(220, 200, 120), font=row)
    rows = [("Raider Tokens Earned", 52310), ("Containers Looted", values["containers"]),
            ("Expeditions Completed", values["expeditions"]), ("Items Crafted", 412)]
    if combat_tab:
        rows = [("ARC Destroyed", 812), ("Damage Dealt", 1203344), ("Headshots", 3021)]
    for j, (label, v) in enumerate(rows):
        y = height * (0.44 + j * 0.07)
        d.line([(width * 0.05, y - height * 0.015), (width * 0.95, y - height * 0.015)], fill=(40, 44, 50), width=2)
        d.text((width * 0.06, y), label, fill=(200, 200, 205), font=row)
        text = f"{v:,}"
        tw = d.textlength(text, font=row)
        d.text((width * 0.94 - tw, y), text, fill=(240, 240, 240), font=row)
    return img


def to_bytes(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def photo_of_screen(img: Image.Image) -> Image.Image:
    """Fake phone photo of a TV: put the screen in a room, skew it, add blue cast, blur and noise."""
    import cv2

    src = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    h, w = src.shape[:2]
    canvas_w, canvas_h = int(w * 1.25), int(h * 1.35)
    dst = np.array([[canvas_w * 0.10, canvas_h * 0.12], [canvas_w * 0.92, canvas_h * 0.07],
                    [canvas_w * 0.95, canvas_h * 0.90], [canvas_w * 0.07, canvas_h * 0.86]], dtype="float32")
    srcq = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(srcq, dst)
    room = np.full((canvas_h, canvas_w, 3), (95, 85, 75), dtype=np.uint8)
    warped = cv2.warpPerspective(src, m, (canvas_w, canvas_h), dst=room, borderMode=cv2.BORDER_TRANSPARENT)
    warped = warped.astype(np.float32)
    warped[..., 0] = np.clip(warped[..., 0] * 1.25 + 12, 0, 255)  # blue cast (BGR)
    warped = cv2.GaussianBlur(warped, (3, 3), 0)
    rng = np.random.default_rng(3)
    warped = np.clip(warped + rng.normal(0, 6, warped.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))
