"""Image preparation for Tesseract: downscale, grayscale, upscale small text, normal + inverted thresholds,
and a best-effort perspective/contrast fix for phone photos of a screen."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


class ImageError(ValueError):
    pass


@dataclass
class Variant:
    name: str
    image: np.ndarray  # single-channel uint8
    scale: float  # variant pixels per original pixel (after downscale)


def decode(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ImageError("not a readable PNG/JPG")
    return img


def downscale(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    side = max(h, w)
    if side <= max_side:
        return img
    f = max_side / side
    return cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)


def _order_corners(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")


def find_screen_quad(img: np.ndarray) -> np.ndarray | None:
    """Largest convex 4-corner contour covering a good share of the photo (a TV/monitor), or None."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 30, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area_img = img.shape[0] * img.shape[1]
    best = None
    best_area = 0.0
    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        area = cv2.contourArea(approx)
        if len(approx) == 4 and cv2.isContourConvex(approx) and area > 0.25 * area_img and area > best_area:
            best, best_area = approx, area
    if best is None or best_area > 0.97 * area_img:
        return None  # nothing found, or the "quad" is just the image border (already a clean capture)
    return _order_corners(best)


def warp(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = quad
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(img, m, (width, height))


def normalize_contrast(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    return clahe.apply(gray)


def variants(img: np.ndarray, *, min_height: int = 1400) -> list[Variant]:
    """Return candidate single-channel images for OCR, in rough order of usefulness."""
    out: list[Variant] = []
    sources: list[tuple[str, np.ndarray]] = [("orig", img)]
    quad = find_screen_quad(img)
    if quad is not None:
        try:
            sources.append(("warped", warp(img, quad)))
        except cv2.error:
            pass
    for sname, src in sources:
        gray = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
        scale = 1.0
        if gray.shape[0] < min_height:
            scale = min(3.0, min_height / gray.shape[0])
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        # UI is light text on dark: invert so text is dark on light, which Tesseract prefers
        inv = cv2.bitwise_not(gray)
        _, inv_otsu = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        out.append(Variant(f"{sname}-inv-otsu", inv_otsu, scale))
        out.append(Variant(f"{sname}-inv-gray", inv, scale))
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        out.append(Variant(f"{sname}-otsu", otsu, scale))
        if sname != "orig" or quad is None:
            eq = cv2.bitwise_not(normalize_contrast(gray))
            out.append(Variant(f"{sname}-clahe", eq, scale))
    return out
