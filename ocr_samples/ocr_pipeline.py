from __future__ import annotations

import argparse
import ast
from difflib import SequenceMatcher
import json
import os
import re
import sys
import traceback
import time
from tempfile import TemporaryDirectory
import unicodedata
from html import escape
from html.parser import HTMLParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from tqdm import tqdm


PAGE_RE = re.compile(r"page_(?P<num>\d+)\.png$", re.IGNORECASE)
SIGNATURE_CUES = (
    "NGUOI LAP BIEU",
    "KE TOAN TRUONG",
    "GIAM DOC",
    "TONG GIAM DOC",
    "KIEM TOAN VIEN",
    "NGUOI DAI DIEN",
    "NGUOI LAP"
)

OCR_DIGIT_MAP = str.maketrans({
    "I": "1", "l": "1", "|": "1", "!": "1", "¡": "1",
    "O": "0", "o": "0",
})

COMMON_FINANCIAL_TEXT_FIXES = (
    (r"\bngắn hạnh\b", "ngắn hạn"),
    (r"\bngắn hạng\b", "ngắn hạn"),
    (r"\bngăn hạng\b", "ngắn hạn"),
    (r"\bdài hạnh\b", "dài hạn"),
    (r"\bdài hạng\b", "dài hạn"),
    (r"\bChúng khoán\b", "Chứng khoán"),
)

# Standard Vietnamese balance-sheet labels. Prefix repair is only allowed when
# the phrase itself closely matches one of these labels.
CANONICAL_FINANCIAL_DESCRIPTIONS = (
    "A. TÀI SẢN NGẮN HẠN",
    "I. Tiền và các khoản tương đương tiền",
    "II. Đầu tư tài chính ngắn hạn",
    "1. Chứng khoán kinh doanh",
    "III. Các khoản phải thu ngắn hạn",
    "1. Phải thu ngắn hạn của khách hàng",
    "2. Trả trước cho người bán ngắn hạn",
    "3. Phải thu ngắn hạn khác",
    "4. Dự phòng phải thu ngắn hạn khó đòi",
    "IV. Hàng tồn kho",
    "1. Hàng tồn kho",
    "2. Dự phòng giảm giá hàng tồn kho",
    "V. Tài sản ngắn hạn khác",
    "1. Chi phí trả trước ngắn hạn",
    "B. TÀI SẢN DÀI HẠN",
    "I. Các khoản phải thu dài hạn",
    "1. Phải thu dài hạn khác",
    "II. Tài sản cố định",
    "1. Tài sản cố định hữu hình",
    "2. Tài sản cố định vô hình",
    "III. Tài sản dở dang dài hạn",
    "1. Chi phí xây dựng cơ bản dở dang",
    "IV. Tài sản dài hạn khác",
    "1. Chi phí trả trước dài hạn",
)


class JsonProgress:
    """Send progress events to a notebook without terminal control sequences."""

    def __init__(self, pages: list[Path]) -> None:
        self.pages = pages
        self.completed = 0
        self.set_postfix_str("Starting")

    def set_postfix_str(self, stage: str) -> None:
        print("OCR_EVENT " + json.dumps({
            "total": len(self.pages), "completed": self.completed, "stage": stage,
        }), flush=True)

    def __iter__(self):
        for page in self.pages:
            yield page
            self.completed += 1
            self.set_postfix_str(f"Finished {page.name}")

    def close(self) -> None:
        pass


@dataclass
class ScaleInfo:
    original_width: int
    original_height: int
    detection_width: int
    detection_height: int

    @property
    def sx(self) -> float:
        return self.original_width / self.detection_width

    @property
    def sy(self) -> float:
        return self.original_height / self.detection_height


@dataclass
class TextCrop:
    id: str
    bbox: list[int]
    image: Image.Image
    source: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LogoCache:
    values: dict[str, str] = field(default_factory=dict)
    max_entries: int = 4096

    def classify(self, crop: Image.Image, bbox: list[int], page_width: int, page_height: int) -> tuple[str, str]:
        phash = dhash(crop)
        if phash in self.values:
            return self.values[phash], phash

        x1, y1, x2, y2 = bbox
        area_ratio = ((x2 - x1) * (y2 - y1)) / max(1, page_width * page_height)
        role = "logo_candidate" if y1 < page_height * 0.25 and area_ratio < 0.08 else "image"
        if len(self.values) >= self.max_entries:
            self.values.pop(next(iter(self.values)))
        self.values[phash] = role
        return role, phash


def page_number(path: Path) -> int:
    match = PAGE_RE.search(path.name)
    if not match:
        return 0
    return int(match.group("num"))


def relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def infer_symbol_year(page_path: Path, pages_root: Path, symbol: str | None, year: str | None) -> tuple[str, str]:
    try:
        rel = page_path.resolve().relative_to(pages_root.resolve())
        parts = rel.parts
    except ValueError:
        parts = page_path.parts

    if len(parts) >= 3:
        return parts[-3], parts[-2]
    return symbol or "UNKNOWN", year or "UNKNOWN"


def output_json_path(
    output_root: Path,
    page_path: Path,
    pages_root: Path,
    symbol: str | None,
    year: str | None,
) -> Path:
    symbol, year = infer_symbol_year(page_path, pages_root, symbol, year)
    return output_root / symbol / year / f"{page_path.stem}.json"


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def clamp_bbox(bbox: Iterable[float | int], width: int, height: int) -> list[int]:
    x1, y1, x2, y2 = [int(round(float(v))) for v in list(bbox)[:4]]
    x1 = max(0, min(x1, width))
    y1 = max(0, min(y1, height))
    x2 = max(0, min(x2, width))
    y2 = max(0, min(y2, height))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return [x1, y1, x2, y2]


def polygon_to_bbox(poly: Any) -> list[int] | None:
    if poly is None:
        return None
    if hasattr(poly, "tolist"):
        poly = poly.tolist()
    if isinstance(poly, (list, tuple)) and len(poly) == 0:
        return None
    if isinstance(poly, (list, tuple)) and len(poly) == 1:
        return polygon_to_bbox(poly[0])
    if len(poly) == 4 and all(isinstance(v, (int, float)) for v in poly):
        return [int(round(v)) for v in poly]
    if len(poly) == 8 and all(isinstance(v, (int, float)) for v in poly):
        poly = list(zip(poly[::2], poly[1::2]))
    try:
        xs = [point[0] for point in poly]
        ys = [point[1] for point in poly]
        return [
            int(round(min(xs))),
            int(round(min(ys))),
            int(round(max(xs))),
            int(round(max(ys))),
        ]
    except (TypeError, IndexError, ValueError):
        return None


def make_detection_view(image: Image.Image, long_side: int) -> tuple[Image.Image, ScaleInfo]:
    width, height = image.size
    longest = max(width, height)
    if longest <= long_side:
        resized = image.copy()
    else:
        scale = long_side / longest
        resized = image.resize((round(width * scale), round(height * scale)), Image.Resampling.BILINEAR)
    det_width, det_height = resized.size
    return resized, ScaleInfo(width, height, det_width, det_height)


def map_bbox_to_original(bbox: list[int], scale: ScaleInfo) -> list[int]:
    """
    Map ONE XYXY bounding box from detection coordinates to original-page
    coordinates.

    bbox is always FOUR COORDINATES:
        [x1, y1, x2, y2]

    The number 3 is only the Python index of y2. It has no relationship to
    how many ROIs/signatures/seals exist on a page.
    """
    if len(bbox) < 4:
        raise ValueError(f"Expected XYXY bbox with 4 coordinates, got: {bbox}")

    x1, y1, x2, y2 = bbox[:4]

    mapped_bbox = [
        x1 * scale.sx,
        y1 * scale.sy,
        x2 * scale.sx,
        y2 * scale.sy,
    ]

    return clamp_bbox(
        mapped_bbox,
        scale.original_width,
        scale.original_height,
    )


def map_bboxes_to_original(
    bboxes: list[list[int]],
    scale: ScaleInfo,
) -> list[list[int]]:
    """
    Map ANY NUMBER of bounding boxes.

    0 boxes -> []
    1 box   -> [mapped_box]
    N boxes -> N mapped boxes
    """
    return [
        map_bbox_to_original(bbox, scale)
        for bbox in bboxes
    ]


def crop_image_memory(image: Image.Image, bbox: list[int], padding: int = 2) -> Image.Image:
    width, height = image.size
    x1, y1, x2, y2 = bbox
    padded = clamp_bbox([x1 - padding, y1 - padding, x2 + padding, y2 + padding], width, height)
    return image.crop(tuple(padded))


def save_crop(image: Image.Image, bbox: list[int], output_path: Path, padding: int = 2) -> Image.Image:
    crop = crop_image_memory(image, bbox, padding=padding)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(output_path)
    return crop


def union_bbox(boxes: list[list[int]], width: int, height: int) -> list[int]:
    return clamp_bbox(
        [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
        width,
        height,
    )


def bbox_gap(a: list[int], b: list[int]) -> int:
    horizontal_gap = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    vertical_gap = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return max(horizontal_gap, vertical_gap)


def bbox_intersects(a: list[int], b: list[int]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def bbox_area(bbox: list[int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def bbox_iou(a: list[int], b: list[int]) -> float:
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = bbox_area([ix1, iy1, ix2, iy2])
    union = bbox_area(a) + bbox_area(b) - inter
    return inter / union if union > 0 else 0.0


def bbox_overlap_ratio(a: list[int], b: list[int]) -> float:
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = bbox_area([ix1, iy1, ix2, iy2])
    smallest = min(bbox_area(a), bbox_area(b))
    return inter / smallest if smallest > 0 else 0.0


def bbox_intersection_area(a: list[int], b: list[int]) -> int:
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    return bbox_area([ix1, iy1, ix2, iy2])


def bbox_coverage(subject: list[int], other: list[int]) -> float:
    area = bbox_area(subject)
    return bbox_intersection_area(subject, other) / area if area > 0 else 0.0


def bbox_center(bbox: list[int]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def bbox_height(bbox: list[int]) -> int:
    return max(0, bbox[3] - bbox[1])


def vertical_overlap_ratio(a: list[int], b: list[int]) -> float:
    overlap = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    denom = max(1, min(bbox_height(a), bbox_height(b)))
    return overlap / denom


def crop_image_memory_masked(
    image: Image.Image,
    bbox: list[int],
    mask_bboxes: list[list[int]],
    padding: int = 2,
    mask_padding: int = 2,
) -> Image.Image:
    from PIL import ImageDraw

    width, height = image.size
    x1, y1, x2, y2 = bbox
    padded = clamp_bbox([x1 - padding, y1 - padding, x2 + padding, y2 + padding], width, height)
    crop = image.crop(tuple(padded)).convert("RGB")
    draw = ImageDraw.Draw(crop)
    for other in mask_bboxes:
        if not bbox_intersects(padded, other):
            continue
        ox1, oy1, ox2, oy2 = clamp_bbox(
            [other[0] - mask_padding, other[1] - mask_padding, other[2] + mask_padding, other[3] + mask_padding],
            width,
            height,
        )
        local = [
            max(0, ox1 - padded[0]),
            max(0, oy1 - padded[1]),
            min(crop.size[0], ox2 - padded[0]),
            min(crop.size[1], oy2 - padded[1]),
        ]
        if local[2] > local[0] and local[3] > local[1]:
            draw.rectangle(local, fill=(255, 255, 255))
    return crop


def crop_has_meaningful_ink(image: Image.Image) -> bool:
    import numpy as np

    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    if gray.size == 0:
        return False
    dark = int(np.count_nonzero(gray < 205))
    return dark >= max(4, int(round(gray.size * 0.0025)))


def dhash(image: Image.Image, hash_size: int = 8) -> str:
    gray = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    bits = []
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits.append(pixels[offset + col] > pixels[offset + col + 1])
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{hash_size * hash_size // 4}x}"


def ascii_upper(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return without_marks.replace("Đ", "D").replace("đ", "d").upper()


def pil_to_numpy(image: Image.Image):
    import numpy as np

    return np.array(image.convert("RGB"))


def make_bbox_mask(shape: tuple[int, int], bboxes: list[list[int]] | None) -> Any:
    import numpy as np

    height, width = shape
    mask = np.zeros((height, width), dtype=bool)
    for bbox in bboxes or []:
        x1, y1, x2, y2 = clamp_bbox(bbox, width, height)
        if x2 > x1 and y2 > y1:
            mask[y1:y2, x1:x2] = True
    return mask


def make_layer_view(mask: Any, luma: Any) -> Image.Image:
    import numpy as np

    view = np.full(luma.shape, 255, dtype="uint8")
    view[mask] = np.clip(luma[mask], 0, 220).astype("uint8")
    return Image.fromarray(view, mode="L").convert("RGB")


def connected_ink_mask(strong: Any, weak: Any, min_area: int = 2) -> Any:
    import cv2
    import numpy as np

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (weak | strong).astype(np.uint8), connectivity=8,
    )
    seeded = np.bincount(labels[strong], minlength=count) > 0
    keep = seeded & (stats[:, cv2.CC_STAT_AREA] >= min_area)
    keep[0] = False
    return keep[labels]


def remove_red_ink(image: Image.Image) -> Image.Image:
    """Keep black/blue strokes and antialiasing; remove confidently red ink."""
    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    r, g, b = np.moveaxis(rgb.astype(np.int16), 2, 0)
    dominance = r - np.maximum(g, b)
    strongest = np.maximum.reduce([r, g, b])
    dark_stroke = (strongest < 180) & (dominance < strongest * 0.40)
    red = (dominance >= 4) & (r - g >= 4) & ~dark_stroke
    # Estimate the red channel of unmixed stamp ink. Dropout is continuous:
    # red-tinted dark handwriting must not be erased to white at crossings.
    stamp_seed = (dominance > strongest * 0.50) & (r >= 130)
    red_reference = float(np.median(r[stamp_seed])) if np.count_nonzero(stamp_seed) >= 32 else 210.0
    # The darkest channel preserves thin/faint blue handwriting in grayscale.
    gray = np.minimum.reduce([r, g, b]).astype(np.uint8)
    gray[red] = np.clip(r[red].astype(np.float32) * (255.0 / red_reference), 0, 255).astype(np.uint8)
    return Image.fromarray(gray).convert("RGB")


def make_signature_ink_view(image: Image.Image) -> Image.Image:
    """
    Preserve signature strokes in their original color while removing red seal
    ink. Signature ink is intentionally multi-color: blue, dark blue, black,
    gray, and antialiased stroke edges are all valid.
    """
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return image.convert("RGB")

    rgb_i = rgb.astype(np.int16)
    r, g, b = np.moveaxis(rgb_i, 2, 0)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, saturation, value = np.moveaxis(hsv, 2, 0)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_a = lab[:, :, 1] - 128.0
    lab_b = lab[:, :, 2] - 128.0

    darkest = np.minimum.reduce([r, g, b])
    spread = np.max(rgb_i, axis=2) - np.min(rgb_i, axis=2)
    red_dom = r - np.maximum(g, b)
    blue_dom = b - ((r + g) // 2)

    red_hue = (h <= 16) | (h >= 166)
    blue_hue = (h >= 84) & (h <= 146)

    dark_stroke_protect = (darkest < 165) & (red_dom < np.maximum(18, value.astype(np.int16) * 0.36))
    blue_stroke_protect = blue_hue & (blue_dom >= 5) & (saturation >= 12)
    red_strong = red_hue & (saturation >= 34) & (red_dom >= 18) & ~dark_stroke_protect & ~blue_stroke_protect
    red_weak = red_hue & (saturation >= 8) & (red_dom >= 5) & ~dark_stroke_protect & ~blue_stroke_protect
    red_mask = connected_ink_mask(red_strong, red_weak, min_area=2)
    red_remove = cv2.dilate(red_mask.astype(np.uint8), np.ones((2, 2), dtype=np.uint8), iterations=1) > 0

    blue_strong = blue_hue & (saturation >= 30) & ((blue_dom >= 13) | (lab_b <= -8.0))
    blue_weak = blue_hue & (saturation >= 8) & ((blue_dom >= 3) | (lab_b <= -3.0))

    neutral_dark_strong = (darkest < 150) & (spread <= 70) & (red_dom < 18)
    neutral_dark_weak = (darkest < 215) & (spread <= 86) & (red_dom < 24)
    gray_edge_weak = (darkest < 226) & (spread <= 36) & (value < 238)

    strong = (blue_strong | neutral_dark_strong) & ~red_remove
    weak = (blue_weak | neutral_dark_weak | gray_edge_weak) & ~red_remove
    stroke_mask = connected_ink_mask(strong, weak, min_area=1)

    # Reconnect one-pixel antialias gaps without growing the stroke outline.
    repaired = cv2.morphologyEx(
        stroke_mask.astype(np.uint8),
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2)),
        iterations=1,
    ) > 0
    stroke_mask = (stroke_mask | (repaired & weak)) & ~red_remove

    canvas = np.full_like(rgb, 255)
    canvas[stroke_mask] = rgb[stroke_mask]
    return Image.fromarray(canvas, mode="RGB")


def make_color_separation_views(
    image: Image.Image,
    red_bboxes: list[list[int]] | None = None,
    blue_bboxes: list[list[int]] | None = None,
) -> dict[str, Image.Image]:
    """Three diagnostic layers; RGB/ink crops remain authoritative for signatures."""
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width = rgb.shape[:2]
    r, g, b = np.moveaxis(rgb.astype(np.int16), 2, 0)
    h, saturation, value = np.moveaxis(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV), 2, 0)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_b = lab[:, :, 2] - 128.0
    red_roi = make_bbox_mask((height, width), red_bboxes)
    blue_roi = make_bbox_mask((height, width), blue_bboxes)
    red_dom = r - np.maximum(g, b)
    blue_dom = b - np.maximum(r, g)
    blue_red_gap = b - r
    red_hue = (h <= 16) | (h >= 166)
    blue_hue = (h >= 85) & (h <= 145)
    darkest = np.minimum.reduce([r, g, b])
    spread = value.astype(np.int16) - darkest
    dark_neutral = (value < 180) & (red_dom < value * 0.40)
    red_strong = red_roi & red_hue & (saturation >= 30) & (red_dom >= 16) & ~dark_neutral
    red_weak = red_roi & red_hue & (saturation >= 10) & (red_dom >= 5) & ~dark_neutral
    red_observed = connected_ink_mask(red_strong, red_weak, min_area=2)

    # Scanned black ink commonly has a weak blue cast.  Require a genuinely
    # chromatic blue seed, then retain only connected antialiased pixels that
    # still have measurable blue separation.  This keeps black signatures and
    # printed text in black.png while preserving dark edges of blue pen strokes.
    blue_strong = (
        blue_roi
        & blue_hue
        & (saturation >= 55)
        & (blue_dom >= 18)
        & (blue_red_gap >= 28)
        & (lab_b <= -10.0)
    )
    blue_weak = (
        blue_roi
        & blue_hue
        & (saturation >= 28)
        & (blue_dom >= 8)
        & (blue_red_gap >= 16)
        & (lab_b <= -5.0)
    )
    blue_mask = connected_ink_mask(
        blue_strong,
        blue_weak & ~red_weak,
        min_area=2,
    )
    if np.any(blue_mask):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            blue_mask.astype(np.uint8), connectivity=8,
        )
        min_blue_area = max(2, int(round(width * height * 0.0005)))
        keep = stats[:, cv2.CC_STAT_AREA] >= min_blue_area
        keep[0] = False
        blue_mask = keep[labels]

    red_repaired = red_observed.copy()
    for box in red_bboxes or []:
        x1, y1, x2, y2 = clamp_bbox(box, width, height)
        local = red_observed[y1:y2, x1:x2].astype(np.uint8)
        if local.size == 0 or not np.any(local):
            continue
        closed = cv2.morphologyEx(
            local, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            borderType=cv2.BORDER_CONSTANT, borderValue=0,
        )
        missing = (closed > 0) & (local == 0)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(missing.astype(np.uint8), connectivity=8)
        # Only tiny holes/gaps, never broad dilation or full-ring reconstruction.
        keep = (stats[:, cv2.CC_STAT_AREA] <= 6) & (stats[:, cv2.CC_STAT_WIDTH] <= 4) & (stats[:, cv2.CC_STAT_HEIGHT] <= 4)
        keep[0] = False
        additions = keep[labels]
        if np.count_nonzero(additions) <= np.count_nonzero(local) * 0.04:
            red_repaired[y1:y2, x1:x2] |= additions

    # Faint edge pixels stay faint. Painting all weak pixels solid dark red
    # makes the stamp look swollen even without any dilation.
    alpha = np.clip(red_dom.astype(np.float32) / 140.0, 0.0, 1.0)
    added = red_repaired & ~red_observed
    if np.any(added):
        nearby = cv2.GaussianBlur(np.where(red_observed, alpha, 0), (3, 3), 0)
        support = cv2.GaussianBlur(red_observed.astype(np.float32), (3, 3), 0)
        alpha[added] = np.clip(nearby[added] / np.maximum(support[added], 1e-6), 0.10, 0.65)
    red_canvas = np.full_like(rgb, 255)
    target = np.array([195, 20, 35], dtype=np.float32)
    red_canvas[red_repaired] = np.clip(
        255 - (255 - target) * alpha[red_repaired, None], 0, 255,
    ).astype(np.uint8)
    blue_canvas = np.full_like(rgb, 255)
    blue_canvas[blue_mask] = rgb[blue_mask]
    # No erosion, halo dilation, or brightness cutoff on remaining black ink.
    black = np.minimum.reduce([r, g, b]).astype(np.uint8)
    black[red_observed | red_weak | blue_mask] = 255
    return {
        "black": Image.fromarray(black).convert("RGB"),
        "red": Image.fromarray(red_canvas),
        "blue": Image.fromarray(blue_canvas),
    }


def local_color_views(
    original: Image.Image, bbox: list[int],
    red_bboxes: list[list[int]], blue_bboxes: list[list[int]],
) -> dict[str, Image.Image]:
    def localize(boxes: list[list[int]]) -> list[list[int]]:
        return [[box[0] - bbox[0], box[1] - bbox[1], box[2] - bbox[0], box[3] - bbox[1]]
                for box in boxes if bbox_intersects(box, bbox)]

    return make_color_separation_views(
        crop_image_memory(original, bbox, padding=0),
        localize(red_bboxes), localize(blue_bboxes),
    )


def save_color_separation_crops(
    views: dict[str, Image.Image],
    bbox: list[int],
    output_dir: Path,
    root: Path,
    padding: int = 8,
) -> dict[str, str]:
    """
    Save exactly three files inside ONE folder belonging to one object:
        black.png, red.png, blue.png

    Example:
      color_views/seals/seal_001/{black,red,blue}.png
      color_views/signatures/signature_001/{black,red,blue}.png
      color_views/seal_signature/seal_signature_001/{black,red,blue}.png
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for name in ("black", "red", "blue"):
        view = views.get(name)
        if view is None:
            continue
        crop_path = output_dir / f"{name}.png"
        save_crop(view, bbox, crop_path, padding=padding)
        paths[f"{name}_view_crop"] = relpath(crop_path, root)
    return paths

def validate_referenced_crops(elements: list[dict[str, Any]], root: Path) -> None:
    missing = []
    for item in elements:
        for key, value in item.items():
            if isinstance(value, dict):
                validate_referenced_crops([value], root)
            elif isinstance(value, list):
                validate_referenced_crops([entry for entry in value if isinstance(entry, dict)], root)
            if not isinstance(value, str):
                continue
            if key != "crop" and not key.endswith("_view_crop"):
                continue
            crop_path = root / value
            if not crop_path.exists() or crop_path.stat().st_size <= 0:
                missing.append(str(crop_path))
    if missing:
        sample = "\n".join(f"- {path}" for path in missing[:20])
        raise FileNotFoundError(f"JSON references missing or empty crop files:\n{sample}")


class CheckboxDetectorONNX:
    class_names = ["checkbox_checked", "checkbox_unchecked"]

    def __init__(
        self,
        repo_id: str = "wendys-llc/checkbox-detector",
        threshold: float = 0.50,
        input_size: int = 1024,
        source_dpi: int = 300,
        model_dpi: int = 72,
        nms_threshold: float = 0.35,
        magnify_dpi: int = 144,
        enable_geometry_fallback: bool = False,
        model_path: Path | None = None,
        output_format: str = "auto",
        threads: int = 2,
        magnify_mode: str = "auto",
    ) -> None:
        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        self.np = np
        self.repo_id = repo_id
        self.threshold = threshold
        self.proposal_threshold = min(threshold, 0.30)
        self.input_size = input_size
        self.source_dpi = source_dpi
        self.model_dpi = model_dpi
        self.nms_threshold = nms_threshold
        self.magnify_dpi = max(model_dpi, magnify_dpi)
        self.enable_geometry_fallback = enable_geometry_fallback
        self.magnify_mode = magnify_mode
        self.last_stats: dict[str, Any] = {}

        if model_path is None:
            model_file = "checkbox_yolo12n.onnx"
            try:
                model_path = hf_hub_download(repo_id=repo_id, filename=model_file, local_files_only=True)
            except FileNotFoundError:
                model_path = hf_hub_download(repo_id=repo_id, filename=model_file)

        providers = ["CPUExecutionProvider"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=options, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        shape = self.session.get_inputs()[0].shape
        if isinstance(shape[-1], int) and isinstance(shape[-2], int):
            if shape[-1] != shape[-2]:
                raise ValueError(f"Checkbox model needs square NCHW input, got {shape}")
            self.input_size = shape[-1]
        metadata_names = self._read_class_names_from_metadata()
        if metadata_names:
            self.class_names = metadata_names
        meta = self.session.get_modelmeta().custom_metadata_map
        if output_format == "auto":
            try:
                export_args = ast.literal_eval(meta.get("args", "{}"))
            except (ValueError, SyntaxError):
                export_args = {}
            end2end = str(meta.get("end2end", "false")).lower() == "true"
            if export_args.get("nms") or end2end:
                output_format = "end2end"
            elif self.session.get_outputs()[0].shape[1] == 4 + len(self.class_names):
                output_format = "yolo"
            else:
                raise ValueError("Unknown checkbox ONNX output. Set --checkbox-output-format explicitly.")
        self.output_format = output_format
        self.model_path = str(model_path)

    def _read_class_names_from_metadata(self) -> list[str] | None:
        raw_values = []
        try:
            meta = self.session.get_modelmeta().custom_metadata_map
            raw_values.extend(
                str(value)
                for key, value in meta.items()
                if key.lower() in {"names", "classes", "class_names"}
            )
        except Exception:
            pass
        for raw in raw_values:
            names = re.findall(r"checkbox_[a-z_]+", raw)
            if len(names) >= 2:
                return names
        return None

    def _suppress(self, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for item in sorted(detections, key=lambda item: float(item["confidence"]), reverse=True):
            if any(bbox_iou(item["bbox"], old["bbox"]) > self.nms_threshold for old in kept):
                continue
            kept.append(item)
        return kept

    def detect(self, image: Image.Image) -> list[dict[str, Any]]:
        start = time.perf_counter()
        raw = self._detect_at_dpi(image, self.model_dpi, pass_name="native")
        native_count = len(raw)
        proposals = self._suppress(raw)
        gated: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        def gate(items: list[dict[str, Any]]) -> None:
            for item in items:
                box = item["bbox"]
                side = max(box[2] - box[0], bbox_height(box))
                near_edge = min(box[0], box[1], image.width - box[2], image.height - box[3]) < side
                if near_edge:
                    if image.width - box[2] < side:
                        label_x1, label_x2 = box[0] - side * 8, box[0] - 3
                    else:
                        label_x1, label_x2 = box[2] + 3, box[2] + side * 8
                    neighborhood = clamp_bbox([label_x1, box[1] - side // 2, label_x2, box[3] + side // 2], *image.size)
                    if not crop_has_meaningful_ink(crop_image_memory(image, neighborhood, padding=0)):
                        continue
                geometry = self._checkbox_geometry_evidence(image, item["bbox"])
                item["raw"]["geometry"] = geometry
                # A tick can protrude outside the frame and reduce the outer
                # contour's rectangularity. Require orthogonal sides/corners.
                tick_frame = (
                    item["checked"]
                    and geometry.get("frame_score", 0) >= 0.68
                    and geometry.get("side_hits", 0) >= 3
                    and geometry.get("corner_hits", 0) >= 3
                    and geometry.get("max_h", 0) >= 24
                    and geometry.get("max_v", 0) >= 24
                    and geometry.get("rectangularity", 0) >= 0.45
                )
                if geometry.get("valid") or tick_frame:
                    item["raw"]["frame_accepted"] = True
                    if item["confidence"] >= self.threshold:
                        gated.append(item)
                    else:
                        pending.append(item)

        gate(proposals)
        magnify = self.magnify_mode == "always" or (
            self.magnify_mode == "auto" and (
                not gated or any(min(item["bbox"][2] - item["bbox"][0], bbox_height(item["bbox"]))
                                 * self.model_dpi / self.source_dpi < 10 for item in gated)
                or len(gated) < len(proposals)
            )
        )
        magnified_count = 0
        if magnify and self.magnify_dpi > self.model_dpi:
            more = self._detect_at_dpi(image, self.magnify_dpi, pass_name="magnified")
            magnified_count = len(more)
            # Suppress duplicates before expensive image/contour validation.
            novel = [item for item in self._suppress(more)
                     if not any(bbox_iou(item["bbox"], old["bbox"]) > self.nms_threshold for old in gated)]
            gate(novel)
        anchors = list(gated)
        for item in pending:
            box = item["bbox"]
            neighbors = [old for old in anchors
                         if abs(bbox_center(old["bbox"])[1] - bbox_center(box)[1]) <= bbox_height(box) * 0.8
                         and 0.6 <= bbox_height(old["bbox"]) / max(1, bbox_height(box)) <= 1.6
                         and bbox_gap(old["bbox"], box) <= max(1, box[2] - box[0]) * 35
                         and not bbox_intersects(old["bbox"], box)]
            if len(neighbors) >= 2:
                item["raw"]["context_recovered"] = True
                gated.append(item)
        if self.enable_geometry_fallback:
            gated.extend(self._detect_geometric_candidates(image))
        kept = self._suppress(gated)
        self.last_stats = {
            "native_proposals": native_count, "magnified_proposals": magnified_count,
            "accepted": len(kept), "threshold": self.threshold,
            "proposal_threshold": getattr(self, "proposal_threshold", self.threshold),
            "seconds": round(time.perf_counter() - start, 3),
        }
        return sorted(kept, key=lambda item: (item["bbox"][1], item["bbox"][0]))

    def _detect_at_dpi(self, image: Image.Image, target_dpi: int, pass_name: str) -> list[dict[str, Any]]:
        width, height = image.size
        scale_to_model = target_dpi / self.source_dpi if self.source_dpi > 0 else 1.0
        if scale_to_model <= 0:
            scale_to_model = 1.0

        work = image.convert("RGB")
        if abs(scale_to_model - 1.0) > 1e-3:
            work = work.resize(
                (max(1, round(width * scale_to_model)), max(1, round(height * scale_to_model))),
                Image.Resampling.BILINEAR,
            )

        detections: list[dict[str, Any]] = []
        work_w, work_h = work.size
        tile_size = self.input_size
        stride = max(1, int(round(tile_size * 0.80)))
        xs = self._tile_starts(work_w, tile_size, stride)
        ys = self._tile_starts(work_h, tile_size, stride)

        for y0 in ys:
            for x0 in xs:
                tile = work.crop((x0, y0, min(work_w, x0 + tile_size), min(work_h, y0 + tile_size)))
                for det in self._detect_tile(tile):
                    x1, y1, x2, y2 = det["bbox"]
                    mapped = [
                        (x1 + x0) / scale_to_model,
                        (y1 + y0) / scale_to_model,
                        (x2 + x0) / scale_to_model,
                        (y2 + y0) / scale_to_model,
                    ]
                    bbox = clamp_bbox(mapped, width, height)
                    bw = bbox[2] - bbox[0]
                    bh = bbox[3] - bbox[1]
                    if bw < 8 or bh < 8:
                        continue
                    aspect = bw / max(1, bh)
                    if aspect < 0.58 or aspect > 1.55:
                        continue
                    if max(bw, bh) > min(width, height) * 0.075:
                        continue
                    label = det["label"]
                    detections.append({
                        "type": "checkbox",
                        "bbox": bbox,
                        "checked": label == "checkbox_checked",
                        "confidence": det["confidence"],
                        "source": "wendys_llc_checkbox_detector_onnx",
                        "raw": {
                            "model": self.repo_id,
                            "label": label,
                            "tile_origin": [x0, y0],
                            "inference_dpi": target_dpi,
                            "source_dpi": self.source_dpi,
                            "pass": pass_name,
                        },
                    })
        return detections

    def _checkbox_geometry_evidence(self, image: Image.Image, bbox: list[int]) -> dict[str, Any]:
        """
        Verify that an ONNX candidate contains a real square frame.

        The previous gate relied too much on Hough line counts, so digits/letters
        could survive while damaged real checkboxes were sometimes rejected.
        This version combines straight-side evidence with outer-contour
        rectangularity. Rounded glyphs such as 0/O/8 have noticeably lower
        rectangularity and/or no four-corner frame.
        """
        import cv2
        import numpy as np

        w0 = max(1, bbox[2] - bbox[0])
        h0 = max(1, bbox[3] - bbox[1])
        pad = max(2, int(round(max(w0, h0) * 0.10)))
        crop = crop_image_memory(image, bbox, padding=pad).convert("L")
        gray = np.asarray(crop, dtype=np.uint8)
        if gray.size == 0:
            return {"valid": False, "frame_score": 0.0, "reason": "empty"}

        norm = cv2.resize(gray, (96, 96), interpolation=cv2.INTER_AREA)
        blur = cv2.GaussianBlur(norm, (3, 3), 0)
        _, ink = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        edges = cv2.Canny(blur, 45, 130)

        # ---- straight horizontal / vertical border evidence ----
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180.0,
            threshold=16,
            minLineLength=24,
            maxLineGap=9,
        )
        h_lines = 0
        v_lines = 0
        max_h = 0.0
        max_v = 0.0
        if lines is not None:
            for line in lines[:, 0, :]:
                x1, y1, x2, y2 = [int(v) for v in line]
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                length = float((dx * dx + dy * dy) ** 0.5)
                if dx >= max(1, dy) * 2.8 and length >= 24:
                    h_lines += 1
                    max_h = max(max_h, length)
                if dy >= max(1, dx) * 2.8 and length >= 24:
                    v_lines += 1
                    max_v = max(max_v, length)

        # ---- support along four sides / four corners ----
        band = 18
        top = ink[7:7 + band, 8:88]
        bottom = ink[89 - band:89, 8:88]
        left = ink[8:88, 7:7 + band]
        right = ink[8:88, 89 - band:89]
        side_density = [
            float(np.mean(top > 0)),
            float(np.mean(bottom > 0)),
            float(np.mean(left > 0)),
            float(np.mean(right > 0)),
        ]
        side_hits = sum(value >= 0.055 for value in side_density)

        corner_windows = (
            ink[6:30, 6:30],
            ink[6:30, 66:90],
            ink[66:90, 6:30],
            ink[66:90, 66:90],
        )
        corner_density = [
            float(np.mean(win > 0)) if win.size else 0.0
            for win in corner_windows
        ]
        corner_hits = sum(value >= 0.028 for value in corner_density)

        # ---- outer contour rectangularity: strongest anti-0/O/8 signal ----
        contours, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        best_rectangularity = 0.0
        best_vertices = 0
        best_contour_aspect = 0.0
        best_contour_coverage = 0.0
        for contour in contours:
            x, y, w, h2 = cv2.boundingRect(contour)
            if w < 24 or h2 < 24 or w > 92 or h2 > 92:
                continue
            aspect = w / max(1.0, float(h2))
            if not (0.66 <= aspect <= 1.48):
                continue
            area = float(abs(cv2.contourArea(contour)))
            rect_area = float(max(1, w * h2))
            rectangularity = area / rect_area
            perimeter = float(cv2.arcLength(contour, True))
            vertices = 0
            if perimeter > 0:
                approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
                vertices = int(len(approx))
            coverage = rect_area / float(96 * 96)
            score = rectangularity + min(0.12, coverage * 0.18)
            old_score = best_rectangularity + min(0.12, best_contour_coverage * 0.18)
            if score > old_score:
                best_rectangularity = rectangularity
                best_vertices = vertices
                best_contour_aspect = aspect
                best_contour_coverage = coverage

        contour_square = (
            best_rectangularity >= 0.78
            and 3 <= best_vertices <= 7
            and 0.70 <= best_contour_aspect <= 1.42
            and best_contour_coverage >= 0.10
        )

        direction_score = min(1.0, (max_h + max_v) / 105.0)
        side_score = side_hits / 4.0
        corner_score = corner_hits / 4.0
        rect_score = min(1.0, best_rectangularity / 0.90)
        frame_score = (
            0.30 * rect_score
            + 0.25 * side_score
            + 0.20 * corner_score
            + 0.15 * direction_score
            + 0.10 * (1.0 if contour_square else 0.0)
        )

        # Two acceptance paths:
        # 1) clear rectangular outer frame + reasonable side/corner evidence;
        # 2) damaged/broken frame but strong orthogonal lines and corners.
        valid = (
            contour_square
            and side_hits >= 3
            and corner_hits >= 2
            and (h_lines >= 1 or max_h >= 28)
            and (v_lines >= 1 or max_v >= 28)
            and frame_score >= 0.56
        ) or (
            best_rectangularity >= 0.70
            and side_hits >= 3
            and corner_hits >= 3
            and max_h >= 28
            and max_v >= 28
            and frame_score >= 0.58
        )

        return {
            "valid": bool(valid),
            "h_lines": int(h_lines),
            "v_lines": int(v_lines),
            "max_h": round(max_h, 2),
            "max_v": round(max_v, 2),
            "side_hits": int(side_hits),
            "side_density": [round(vv, 4) for vv in side_density],
            "corner_hits": int(corner_hits),
            "corner_density": [round(vv, 4) for vv in corner_density],
            "rectangularity": round(best_rectangularity, 4),
            "contour_vertices": int(best_vertices),
            "contour_aspect": round(best_contour_aspect, 4),
            "contour_coverage": round(best_contour_coverage, 4),
            "contour_square": bool(contour_square),
            "line_score": round(direction_score, 4),
            "frame_score": round(frame_score, 4),
        }

    def _detect_geometric_candidates(self, image: Image.Image) -> list[dict[str, Any]]:
        import cv2
        import numpy as np

        gray = np.asarray(image.convert("L"), dtype=np.uint8)
        if gray.size == 0:
            return []
        # Adaptive threshold preserves light gray checkbox borders on scans.
        binary = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            31,
            13,
        )
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        page_h, page_w = gray.shape
        min_side = max(18, int(round(self.source_dpi * 0.065)))
        max_side = min(150, int(round(self.source_dpi * 0.45)))
        candidates: list[dict[str, Any]] = []

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w < min_side or h < min_side or w > max_side or h > max_side:
                continue
            aspect = w / max(1.0, float(h))
            if not (0.72 <= aspect <= 1.38):
                continue
            bbox = clamp_bbox([x, y, x + w, y + h], page_w, page_h)
            geometry = self._checkbox_geometry_evidence(image, bbox)
            if not geometry["valid"]:
                continue

            # Classify checked/unchecked from interior ink after ignoring the
            # rectangular border. Diagonal/central ink is a checked signal.
            crop = np.asarray(crop_image_memory(image, bbox, padding=0).convert("L"), dtype=np.uint8)
            if crop.size == 0:
                continue
            hh, ww = crop.shape
            mx = max(2, int(round(ww * 0.24)))
            my = max(2, int(round(hh * 0.24)))
            inner = crop[my:max(my + 1, hh - my), mx:max(mx + 1, ww - mx)]
            if inner.size == 0:
                continue
            threshold = min(210, int(np.percentile(crop, 35)) + 35)
            inner_ratio = float(np.mean(inner < threshold))
            checked = inner_ratio >= 0.065
            confidence = min(0.86, 0.64 + geometry["line_score"] * 0.16 + min(0.06, inner_ratio * 0.10))
            candidates.append({
                "type": "checkbox",
                "bbox": bbox,
                "checked": bool(checked),
                "confidence": float(confidence),
                "source": "checkbox_geometry_fallback",
                "raw": {
                    "model": None,
                    "label": "checkbox_checked" if checked else "checkbox_unchecked",
                    "geometry": geometry,
                    "interior_ink_ratio": round(inner_ratio, 5),
                    "source_dpi": self.source_dpi,
                },
            })

        candidates.sort(key=lambda item: float(item["confidence"]), reverse=True)
        kept: list[dict[str, Any]] = []
        for item in candidates:
            if any(bbox_iou(item["bbox"], old["bbox"]) > 0.40 for old in kept):
                continue
            kept.append(item)
        return kept

    def _tile_starts(self, length: int, tile_size: int, stride: int) -> list[int]:
        if length <= tile_size:
            return [0]
        starts = list(range(0, max(1, length - tile_size + 1), stride))
        last = max(0, length - tile_size)
        if starts[-1] != last:
            starts.append(last)
        return starts

    def _decode_predictions(self, output: Any) -> tuple[Any, Any, Any]:
        np = self.np
        pred = np.asarray(output)
        if pred.ndim == 3 and pred.shape[0] == 1:
            pred = pred[0]
        if pred.ndim != 2:
            raise ValueError(f"Unexpected checkbox output shape: {pred.shape}")
        if self.output_format == "yolo":
            channels = 4 + len(self.class_names)
            if pred.shape[0] == channels:
                pred = pred.T
            if pred.shape[1] != channels:
                raise ValueError(f"Expected {channels} YOLO channels, got {pred.shape}")
            scores = pred[:, 4:]
            class_ids = scores.argmax(axis=1)
            confidence = scores[np.arange(len(pred)), class_ids]
            coords = pred[:, :4].astype(float)
            boxes = np.column_stack((
                coords[:, 0] - coords[:, 2] / 2, coords[:, 1] - coords[:, 3] / 2,
                coords[:, 0] + coords[:, 2] / 2, coords[:, 1] + coords[:, 3] / 2,
            ))
        else:
            if pred.shape[1] != 6:
                raise ValueError(f"Expected end-to-end [N,6], got {pred.shape}")
            boxes = pred[:, :4].astype(float)
            confidence = pred[:, 4]
            class_ids = pred[:, 5]
        valid = (
            np.isfinite(boxes).all(axis=1) & np.isfinite(confidence)
            & (confidence >= getattr(self, "proposal_threshold", self.threshold)) & (class_ids >= 0)
            & (class_ids < len(self.class_names)) & (class_ids == np.floor(class_ids))
            & (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
        )
        return boxes[valid], confidence[valid], class_ids[valid].astype(int)

    def _detect_tile(self, tile: Image.Image) -> list[dict[str, Any]]:
        np = self.np
        input_size = self.input_size
        tile_w, tile_h = tile.size
        scale = min(input_size / tile_w, input_size / tile_h)
        resized_w, resized_h = max(1, round(tile_w * scale)), max(1, round(tile_h * scale))
        resized = tile.resize((resized_w, resized_h), Image.Resampling.BILINEAR)
        canvas = Image.new("RGB", (input_size, input_size), (255, 255, 255))
        pad_x, pad_y = (input_size - resized_w) // 2, (input_size - resized_h) // 2
        canvas.paste(resized, (pad_x, pad_y))
        arr = np.asarray(canvas, dtype=np.float32) / 255.0
        arr = np.ascontiguousarray(np.transpose(arr, (2, 0, 1))[None, ...])
        outputs = self.session.run(None, {self.input_name: arr})
        boxes, confidence, class_ids = self._decode_predictions(outputs[0])
        # Rounding the letterbox dimensions makes x/y scales slightly different.
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_x) * tile_w / resized_w
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_y) * tile_h / resized_h
        detections = []
        for box, score, class_id in zip(boxes, confidence, class_ids):
            bbox = clamp_bbox(box, tile_w, tile_h)
            if bbox_area(bbox) > 0:
                detections.append({"bbox": bbox, "confidence": float(score), "label": self.class_names[class_id]})
        return self._suppress(detections)


class VietOCRRecognizer:
    def __init__(self, device: str = "cuda:0", config_name: str = "vgg_transformer") -> None:
        from vietocr.tool.config import Cfg
        from vietocr.tool.predictor import Predictor

        config = Cfg.load_config_from_name(config_name)
        config["device"] = device
        config["predictor"]["beamsearch"] = False
        self.predictor = Predictor(config)

    def predict_batch(self, images: list[Image.Image], batch_size: int) -> list[tuple[str, float | None]]:
        if not images:
            return []

        results: list[tuple[str, float | None]] = []
        if batch_size < 1:
            raise ValueError("VietOCR batch size must be positive")
        start = 0
        effective_size = min(batch_size, getattr(self, "_batch_limit", batch_size))
        while start < len(images):
            batch = [image.convert("RGB") for image in images[start:start + effective_size]]
            try:
                if hasattr(self.predictor, "predict_batch"):
                    texts = self.predictor.predict_batch(batch)
                else:
                    texts = [self.predictor.predict(image) for image in batch]
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower() or len(batch) <= 1:
                    raise
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                effective_size = max(1, len(batch) // 2)
                self._batch_limit = effective_size
                print(f"[warn] VietOCR memory limit: retrying batch_size={effective_size}", flush=True)
                continue
            if len(texts) != len(batch):
                raise ValueError("VietOCR result count does not match its input batch")
            results.extend((str(text), None) for text in texts)
            start += len(batch)
        return results


class PaddleTextDetector:
    def __init__(
        self,
        lang: str = "vi",
        det_limit_side_len: int = 1600,
        det_db_thresh: float = 0.30,
        det_db_box_thresh: float = 0.50,
    ) -> None:
        from paddleocr import PaddleOCR

        self.ocr = PaddleOCR(
            lang=lang,
            use_angle_cls=False,
            show_log=False,
            det_limit_side_len=det_limit_side_len,
            det_db_thresh=det_db_thresh,
            det_db_box_thresh=det_db_box_thresh,
        )

    def detect_lines(self, image: Image.Image) -> list[dict[str, Any]]:
        import numpy as np

        np_image = pil_to_numpy(image)[:, :, ::-1].copy()
        if hasattr(self.ocr, "text_detector"):
            raw = self.ocr.text_detector(np_image)
            if isinstance(raw, tuple):
                raw = raw[0]
        else:
            raw = self.ocr.ocr(np_image, det=True, rec=False, cls=False)
        if raw is None or len(raw) == 0 or (len(raw) == 1 and raw[0] is None):
            return []
        # Paddle 2.x wraps a single page's polygons in an outer list.
        polygons = np.asarray(raw, dtype=float)
        if polygons.size == 0:
            return []
        if polygons.ndim == 4 and polygons.shape[0] == 1:
            polygons = polygons[0]
        if polygons.shape == (4, 2):
            polygons = polygons[None, ...]
        if polygons.ndim != 3 or polygons.shape[1:] != (4, 2):
            raise ValueError(f"Unexpected Paddle text detection shape: {polygons.shape}")
        lines: list[dict[str, Any]] = []
        for item in polygons.tolist():
            bbox = polygon_to_bbox(item)
            if bbox:
                lines.append({"bbox": bbox, "polygon": item})
        return sorted(lines, key=lambda x: (x["bbox"][1], x["bbox"][0]))

    def recognize_text(self, image: Image.Image) -> tuple[str, float | None]:
        """Use Paddle's already-loaded recognizer as a conditional second opinion."""
        np_image = pil_to_numpy(image)[:, :, ::-1].copy()
        raw = self.ocr.ocr(np_image, det=False, rec=True, cls=False)
        candidates: list[tuple[str, float | None]] = []

        def collect(value: Any) -> None:
            if isinstance(value, (list, tuple)):
                if (
                    len(value) == 2
                    and isinstance(value[0], str)
                    and isinstance(value[1], (int, float))
                ):
                    candidates.append((value[0], float(value[1])))
                    return
                for child in value:
                    collect(child)

        collect(raw)
        if not candidates:
            return "", None
        text = " ".join(candidate.strip() for candidate, _ in candidates if candidate.strip())
        confidence_values = [confidence for _, confidence in candidates if confidence is not None]
        confidence = min(confidence_values) if confidence_values else None
        return text, confidence


class PaddleStructureEngine:
    def __init__(self, lang: str = "vi", use_seal_recognition: bool = False, table_only: bool = False, lazy: bool = False) -> None:
        self.options = dict(
            lang=lang,
            show_log=False,
            recovery=False,
            use_pdf2docx_api=False,
            use_seal_recognition=use_seal_recognition,
            layout=not table_only,
            table=table_only,
            ocr=False,
        )
        self.engine = None
        if not lazy:
            self._load()

    def _load(self) -> None:
        from paddleocr import PPStructure
        self.engine = PPStructure(**self.options)

    def analyze(self, image: Image.Image) -> list[dict[str, Any]]:
        if self.engine is None:
            self._load()
        return self.engine(pil_to_numpy(image)[:, :, ::-1].copy())


class SignatureDetector:
    def __init__(
        self,
        model_name: str = "mdefrance/yolos-base-signature-detection",
        threshold: float = 0.70,
        device: str = "cuda:0",
    ) -> None:
        import torch
        from transformers import AutoImageProcessor, AutoModelForObjectDetection

        self.torch = torch
        self.device = torch.device(device if device.startswith("cuda") and torch.cuda.is_available() else "cpu")
        self.threshold = threshold
        self.processor = AutoImageProcessor.from_pretrained(model_name)
        self.model = AutoModelForObjectDetection.from_pretrained(model_name).to(self.device)
        self.model.eval()
        self.model_name = model_name

    def detect(self, image: Image.Image, threshold: float | None = None) -> list[dict[str, Any]]:
        inputs = self.processor(images=image.convert("RGB"), return_tensors="pt")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with self.torch.no_grad():
            outputs = self.model(**inputs)

        width, height = image.size
        target_sizes = self.torch.tensor([[height, width]], device=self.device)
        results = self.processor.post_process_object_detection(
            outputs,
            threshold=self.threshold if threshold is None else threshold,
            target_sizes=target_sizes,
        )[0]

        detections: list[dict[str, Any]] = []
        for score, label, box in zip(results["scores"], results["labels"], results["boxes"]):
            detections.append(
                {
                    "bbox": [int(round(v)) for v in box.detach().cpu().tolist()],
                    "score": float(score.detach().cpu().item()),
                    "label": self.model.config.id2label.get(int(label.detach().cpu().item()), "signature"),
                }
            )
        return detections


def structure_bbox(item: dict[str, Any]) -> list[int] | None:
    for key in ("bbox", "box"):
        bbox = polygon_to_bbox(item.get(key))
        if bbox:
            return bbox
    if isinstance(item.get("res"), dict):
        for key in ("bbox", "box"):
            bbox = polygon_to_bbox(item["res"].get(key))
            if bbox:
                return bbox
    return None


def structure_label(item: dict[str, Any]) -> str:
    label = str(item.get("type") or item.get("label") or item.get("category") or "unknown").lower()
    aliases = {
        "stamp": "seal",
        "stamp-seal": "seal",
        "figure": "image",
        "header_image": "image",
        "footer_image": "image",
        "chart": "image",
    }
    return aliases.get(label, label)


def extract_table_cells(item: dict[str, Any]) -> list[dict[str, Any]]:
    res = item.get("res") if isinstance(item.get("res"), dict) else {}
    candidates = (
        item.get("cells"),
        item.get("cell_bbox"),
        res.get("cells"),
        res.get("cell_bbox"),
    )
    cells: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate is None or len(candidate) == 0:
            continue
        for idx, cell in enumerate(candidate):
            if isinstance(cell, dict):
                box = next((cell[key] for key in ("bbox", "box", "points")
                            if cell.get(key) is not None), None)
                bbox = polygon_to_bbox(box)
                row = cell.get("row", cell.get("row_id"))
                col = cell.get("col", cell.get("col_id"))
            else:
                bbox = polygon_to_bbox(cell)
                row = None
                col = None
            if bbox:
                cells.append({"id": idx + 1, "bbox": bbox, "row": row, "col": col})
        if cells:
            break
    return cells


def detect_visual_regions(
    detection_image: Image.Image,
    scale: ScaleInfo,
    structure_engine: PaddleStructureEngine,
) -> list[dict[str, Any]]:
    regions = []
    page_area = max(1, scale.original_width * scale.original_height)
    for item in structure_engine.analyze(detection_image):
        label = structure_label(item)
        if label not in {"table", "image", "seal"}:
            continue
        det_bbox = structure_bbox(item)
        if not det_bbox:
            continue
        bbox = map_bbox_to_original(det_bbox, scale)
        area_ratio = bbox_area(bbox) / page_area
        if label == "image" and area_ratio > 0.65:
            continue
        regions.append({"type": label, "bbox": bbox, "raw": item})
    return regions


def confirmed_table_bboxes(visual_regions: list[dict[str, Any]]) -> list[list[int]]:
    """Return Paddle-layout table regions that own their text-recognition path."""
    return [
        region["bbox"]
        for region in visual_regions
        if region.get("type") == "table" and region.get("bbox")
    ]


def routing_text_outside_tables(
    text_elements: list[dict[str, Any]],
    visual_regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep routing OCR available internally, never as output inside tables."""
    table_bboxes = confirmed_table_bboxes(visual_regions)
    if not table_bboxes:
        return list(text_elements)
    return [
        item for item in text_elements
        if item.get("type") != "text"
        or not item.get("bbox")
        or not any(bbox_coverage(item["bbox"], box) >= 0.70 for box in table_bboxes)
    ]


def _red_ring_angular_coverage(raw_red_mask: Any) -> dict[str, float]:
    """Measure whether red pixels form a seal-like ring around the bbox center."""
    import numpy as np

    mask = np.asarray(raw_red_mask, dtype=bool)
    if mask.size == 0 or not np.any(mask):
        return {"angular_coverage": 0.0, "annulus_density": 0.0}
    height, width = mask.shape
    yy, xx = np.indices(mask.shape)
    cx = (width - 1) / 2.0
    cy = (height - 1) / 2.0
    nx = (xx - cx) / max(1.0, width / 2.0)
    ny = (yy - cy) / max(1.0, height / 2.0)
    rho = np.sqrt(nx * nx + ny * ny)
    angle = (np.arctan2(ny, nx) + 2 * np.pi) % (2 * np.pi)
    annulus = (rho >= 0.65) & (rho <= 1.07)
    sectors = 36
    hits = 0
    densities = []
    for idx in range(sectors):
        sector = annulus & (angle >= (2 * np.pi * idx / sectors)) & (angle < (2 * np.pi * (idx + 1) / sectors))
        denom = int(np.count_nonzero(sector))
        density = float(np.count_nonzero(mask & sector)) / max(1, denom)
        densities.append(density)
        if density >= 0.010:
            hits += 1
    return {
        "angular_coverage": float(hits / sectors),
        "annulus_density": float(np.mean(densities)),
    }


def detect_red_seal_regions(
    detection_image: Image.Image,
    scale: ScaleInfo,
    existing_regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        import cv2
        import numpy as np
    except Exception:
        return []

    rgb = np.array(detection_image.convert("RGB"))
    r = rgb[:, :, 0].astype("int16")
    g = rgb[:, :, 1].astype("int16")
    b = rgb[:, :, 2].astype("int16")
    raw_mask = ((r > 105) & (r - g > 28) & (r - b > 28)).astype("uint8")
    mask = raw_mask * 255

    height, width = mask.shape
    kernel_size = max(5, min(width, height) // 90)
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    candidate_mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    candidate_mask = cv2.dilate(candidate_mask, kernel, iterations=1)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate_mask, connectivity=8)
    candidates: list[dict[str, Any]] = []
    page_area = max(1, width * height)
    existing_seals = [region["bbox"] for region in existing_regions if region["type"] == "seal"]

    for label_id in range(1, count):
        x, y, w, h, area = [int(v) for v in stats[label_id]]
        if w < 24 or h < 24:
            continue
        if area < page_area * 0.00008 or area > page_area * 0.08:
            continue
        aspect = w / max(1, h)

        det_bbox = clamp_bbox([x, y, x + w, y + h], width, height)
        component = (labels[y:y + h, x:x + w] == label_id).astype("uint8")
        band = max(3, min(w, h) // 8)
        top = component[:band, :]
        bottom = component[-band:, :]
        left = component[:, :band]
        right = component[:, -band:]
        top_ratio = float(top.mean())
        bottom_ratio = float(bottom.mean())
        left_ratio = float(left.mean())
        right_ratio = float(right.mean())
        side_hits = sum(value > 0.012 for value in (top_ratio, bottom_ratio, left_ratio, right_ratio))
        border_score = min(top_ratio, bottom_ratio) + min(left_ratio, right_ratio)

        # Critical fix: evaluate ringness on the ORIGINAL red pixels, not on the
        # heavily closed/dilated candidate mask. This rejects red Adobe/digital
        # signature ribbons that previously became a fake square "seal".
        raw_local = raw_mask[y:y + h, x:x + w] > 0
        ring = _red_ring_angular_coverage(raw_local)

        bbox = map_bbox_to_original(det_bbox, scale)
        is_seal_shape = (
            0.70 <= aspect <= 1.45
            and side_hits >= 3
            and border_score >= 0.035
            and ring["angular_coverage"] >= 0.48
            and ring["annulus_density"] >= 0.025
        )
        if not is_seal_shape:
            if area < page_area * 0.00030:
                continue
            candidates.append({
                "type": "red_mark",
                "bbox": bbox,
                "raw": {
                    "source": "red_color_mask_rejected_seal",
                    "det_bbox": det_bbox,
                    "red_area": area,
                    "aspect": aspect,
                    "side_hits": side_hits,
                    "border_score": border_score,
                    **ring,
                },
            })
            continue

        if any(bbox_iou(bbox, existing) > 0.35 for existing in existing_seals):
            continue
        candidates.append({
            "type": "seal",
            "bbox": bbox,
            "raw": {
                "source": "red_seal_color_mask",
                "det_bbox": det_bbox,
                "red_area": area,
                "aspect": aspect,
                "side_hits": side_hits,
                "border_score": border_score,
                **ring,
            },
        })

    return candidates

def materialize_checkbox_elements(
    original: Image.Image,
    checkboxes: list[dict[str, Any]],
    page_crop_dir: Path,
    root: Path,
) -> list[dict[str, Any]]:
    elements = []
    checkbox_dir = page_crop_dir / "checkboxes"
    checkbox_dir.mkdir(parents=True, exist_ok=True)
    for idx, checkbox in enumerate(checkboxes, start=1):
        checkbox_id = f"checkbox_{idx:03d}"
        crop_path = checkbox_dir / f"{checkbox_id}.png"
        # Save essentially the checkbox itself; only 4 px are added so the crop
        # does not turn into a neighbouring text/number crop.
        save_crop(original, checkbox["bbox"], crop_path, padding=4)
        elements.append({
            "id": checkbox_id,
            "type": "checkbox",
            "bbox": checkbox["bbox"],
            "checked": checkbox["checked"],
            "selected": checkbox["checked"],
            "value": "checked" if checkbox["checked"] else "unchecked",
            "confidence": checkbox["confidence"],
            "crop": relpath(crop_path, root),
            "ocr_skipped": True,
            "source": checkbox["source"],
            "raw": checkbox.get("raw", {}),
        })
    return elements

def checkbox_label_key(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = normalized.replace("Đ", "D").replace("đ", "d").upper()
    normalized = re.sub(r"[^A-Z?]+", "", normalized)
    if normalized in {"CO", "C?"}:
        return "co"
    if normalized in {"KHONG", "KH?NG"}:
        return "khong"
    return ""


def attach_checkbox_labels(
    checkbox_elements: list[dict[str, Any]],
    text_elements: list[dict[str, Any]],
) -> None:
    label_candidates = [
        item for item in text_elements
        if item.get("type") == "text" and checkbox_label_key(str(item.get("text") or ""))
    ]
    for checkbox in checkbox_elements:
        cb = checkbox["bbox"]
        cb_cy = (cb[1] + cb[3]) / 2
        cb_h = max(1, cb[3] - cb[1])
        best = None
        best_score = float("inf")
        for label in label_candidates:
            lb = label["bbox"]
            if lb[0] < cb[2] - cb_h * 0.35:
                continue
            gap = lb[0] - cb[2]
            if gap > max(380, cb_h * 7):
                continue
            label_cy = (lb[1] + lb[3]) / 2
            dy = abs(label_cy - cb_cy)
            if dy > max(55, cb_h * 1.35):
                continue
            score = gap + dy * 2
            if score < best_score:
                best = label
                best_score = score
        if best is None:
            continue
        checkbox["label_text"] = best.get("text")
        checkbox["label_key"] = checkbox_label_key(str(best.get("text") or ""))
        checkbox["label_bbox"] = best.get("bbox")
        checkbox["selected_label"] = best.get("text") if checkbox.get("checked") else None


def normalize_checkbox_choice(text: str) -> str | None:
    normalized = ascii_upper(text)
    tokens = re.findall(r"[A-Z0-9?]+", normalized)
    if not tokens:
        return None
    joined = " ".join(tokens)
    if joined in {"CO", "C?", "YES"}:
        return "yes"
    if joined in {"KHONG", "KH?NG", "NO"}:
        return "no"
    return None


def link_checkbox_labels(
    text_elements: list[dict[str, Any]],
    checkbox_elements: list[dict[str, Any]],
    page_width: int,
) -> None:
    if not text_elements or not checkbox_elements:
        return

    used_text_ids: set[str] = set()
    for checkbox in checkbox_elements:
        cb = checkbox["bbox"]
        cb_h = max(1, bbox_height(cb))
        _, cb_cy = bbox_center(cb)
        ranked: list[tuple[float, int, dict[str, Any], str | None]] = []

        for text in text_elements:
            if text.get("type") != "text" or not str(text.get("text") or "").strip():
                continue
            tb = text["bbox"]
            _, ty = bbox_center(tb)
            choice = normalize_checkbox_choice(str(text.get("text") or ""))
            gap_right = tb[0] - cb[2]
            same_row = vertical_overlap_ratio(cb, tb) >= 0.28 or abs(ty - cb_cy) <= max(cb_h, bbox_height(tb)) * 0.75
            if not same_row:
                continue
            if gap_right < -cb_h * 0.55:
                continue
            if gap_right > max(cb_h * 9.0, page_width * 0.10):
                continue

            text_len = len(str(text.get("text") or "").strip())
            if choice is None and text_len > 28:
                continue
            priority = 0 if choice is not None else 1
            score = max(0.0, float(gap_right)) + abs(ty - cb_cy) * 2.2 + text_len * 0.15
            ranked.append((score, priority, text, choice))

        if not ranked:
            continue
        ranked.sort(key=lambda item: (item[1], item[0]))
        _, _, label, choice = ranked[0]
        if label["id"] in used_text_ids and choice is None:
            continue
        used_text_ids.add(label["id"])

        checkbox["label"] = str(label.get("text") or "")
        checkbox["label_text_id"] = label["id"]
        checkbox["choice"] = choice or "unknown"
        checkbox["choice_selected"] = bool(checkbox.get("checked"))
        label["linked_checkbox_id"] = checkbox["id"]
        label["checkbox_checked"] = bool(checkbox.get("checked"))
        label["choice"] = choice or "unknown"
        label["choice_selected"] = bool(checkbox.get("checked"))

    choice_boxes = [cb for cb in checkbox_elements if cb.get("choice") in {"yes", "no"}]
    choice_boxes.sort(key=lambda cb: (bbox_center(cb["bbox"])[1], cb["bbox"][0]))
    groups: list[list[dict[str, Any]]] = []
    for cb in choice_boxes:
        cy = bbox_center(cb["bbox"])[1]
        placed = False
        for group in groups:
            gy = sum(bbox_center(item["bbox"])[1] for item in group) / len(group)
            tol = max([bbox_height(item["bbox"]) for item in group] + [bbox_height(cb["bbox"])]) * 1.15
            if abs(cy - gy) <= tol:
                group.append(cb)
                placed = True
                break
        if not placed:
            groups.append([cb])

    text_by_id = {item.get("id"): item for item in text_elements}
    group_no = 0
    for group in groups:
        choices = {str(item.get("choice")) for item in group}
        if not ({"yes", "no"} <= choices) and len(group) < 2:
            continue
        group_no += 1
        group_id = f"checkbox_group_{group_no:03d}"
        selected = [str(item.get("choice")) for item in group if item.get("checked")]
        selected_choice = selected[0] if len(selected) == 1 else ("multiple" if len(selected) > 1 else None)
        for item in group:
            item["group_id"] = group_id
            item["group_selected_choice"] = selected_choice
            label = text_by_id.get(item.get("label_text_id"))
            if label is not None:
                label["checkbox_group_id"] = group_id
                label["group_selected_choice"] = selected_choice


def collect_text_crops(
    original: Image.Image,
    black_view: Image.Image | None,
    detection_image: Image.Image,
    scale: ScaleInfo,
    text_detector: PaddleTextDetector,
    overlap_bboxes: list[list[int]],
    ignore_bboxes: list[list[int]] | None = None,
) -> list[TextCrop]:
    crops: list[TextCrop] = []
    ignore_bboxes = ignore_bboxes or []
    for idx, line in enumerate(text_detector.detect_lines(detection_image), start=1):
        bbox = map_bbox_to_original(line["bbox"], scale)
        if bbox[2] - bbox[0] < 4 or bbox[3] - bbox[1] < 4:
            continue
        if any(bbox_coverage(bbox, ignored) > 0.68 for ignored in ignore_bboxes):
            continue
        has_overlap = any(bbox_intersects(bbox, other) for other in overlap_bboxes)
        source_image = black_view if has_overlap and black_view is not None else original
        intersecting_ignored = [ignored for ignored in ignore_bboxes if bbox_intersects(bbox, ignored)]
        if intersecting_ignored:
            crop = crop_image_memory_masked(source_image, bbox, intersecting_ignored, padding=2, mask_padding=3)
            if not crop_has_meaningful_ink(crop):
                continue
        else:
            crop = crop_image_memory(source_image, bbox)
        source_name = "paddleocr_fast_text_detection_black_view" if has_overlap else "paddleocr_fast_text_detection_rgb"
        extra: dict[str, Any] = {"color_separation": "black_text_view"} if has_overlap else {}
        if intersecting_ignored:
            source_name += "_checkbox_masked"
            extra["checkbox_masked_before_ocr"] = True
        crops.append(
            TextCrop(
                id=f"text_{idx:04d}",
                bbox=bbox,
                image=crop,
                source=source_name,
                extra=extra,
            )
        )
    return crops


def recognize_text_crops(
    text_crops: list[TextCrop],
    vietocr: VietOCRRecognizer,
    batch_size: int,
    save_dir: Path | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    recognized = vietocr.predict_batch([item.image for item in text_crops], batch_size=batch_size)
    if len(recognized) != len(text_crops):
        raise ValueError(f"VietOCR returned {len(recognized)} results for {len(text_crops)} crops")
    elements = []
    for crop, (text, confidence) in zip(text_crops, recognized):
        record = {
            "id": crop.id,
            "type": "text",
            "bbox": crop.bbox,
            "text": text,
            "confidence": confidence,
            "recognizer": "vietocr_batch",
            "source": crop.source,
            **crop.extra,
        }
        if save_dir is not None and root is not None:
            crop_path = save_dir / f"{crop.id}.png"
            crop_path.parent.mkdir(parents=True, exist_ok=True)
            crop.image.save(crop_path)
            record["crop"] = relpath(crop_path, root)
        elements.append(record)
    return elements


def group_text_rows(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda entry: (bbox_center(entry["bbox"])[1], entry["bbox"][0])):
        box = item["bbox"]
        if rows:
            previous = rows[-1]
            cy = sum(bbox_center(entry["bbox"])[1] for entry in previous) / len(previous)
            height = min(bbox_height(entry["bbox"]) for entry in previous)
            if abs(bbox_center(box)[1] - cy) <= max(3, min(height, bbox_height(box)) * 0.65):
                previous.append(item)
                continue
        rows.append([item])
    return [sorted(row, key=lambda entry: entry["bbox"][0]) for row in rows]


def _financial_amount_tokens(text: str) -> list[str]:
    """Return amount-like tokens from a financial-statement OCR row.

    This deliberately targets thousands-grouped values (for example
    ``161.656.557.109`` or ``(1.409.672.366)``), so report dates such as
    ``31/12/2025`` do not make a title/header look like a data row.
    """
    return re.findall(
        r"(?<!\d)\(?[+-]?\d{1,3}(?:[.,]\d{3}){1,5}\)?%?(?!\d)",
        str(text or ""),
    )


def _infer_financial_table_regions_from_row_text(
    rows: list[list[dict[str, Any]]],
    page_size: tuple[int, int],
    existing_regions: list[dict[str, Any]],
    line_height: float,
) -> list[dict[str, Any]]:
    """Fallback for detectors that return one wide OCR box per table row.

    The old fallback expected each numeric column to already be a separate OCR
    box. DB-style text detectors can instead return the whole unruled row as
    one box. In that case repeated *amount strings inside the row text* are
    enough to localise the table; cell splitting is performed later on the
    original table crop.
    """
    width, height = page_size
    qualified: list[int] = []
    for row_index, row in enumerate(rows):
        row_text = " ".join(str(item.get("text") or "") for item in row)
        if len(_financial_amount_tokens(row_text)) >= 2:
            qualified.append(row_index)

    groups: list[list[int]] = []
    for index in qualified:
        if groups:
            last = groups[-1][-1]
            gap = min(item["bbox"][1] for item in rows[index]) - max(item["bbox"][3] for item in rows[last])
            if gap <= line_height * 4.8:
                groups[-1].append(index)
                continue
        groups.append([index])

    regions: list[dict[str, Any]] = []
    for group in groups:
        if len(group) < 3:
            continue
        first, last = group[0], group[-1]
        # Pull in up to two nearby header rows (column names / currency units).
        for _ in range(2):
            if first <= 0:
                break
            previous = rows[first - 1]
            gap = min(item["bbox"][1] for item in rows[first]) - max(item["bbox"][3] for item in previous)
            if gap > line_height * 3.0:
                break
            first -= 1
        members = [item for row in rows[first:last + 1] for item in row]
        box = union_bbox([item["bbox"] for item in members], width, height)
        box = clamp_bbox([box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8], width, height)
        if any(
            region.get("type") == "table" and bbox_overlap_ratio(box, region["bbox"]) > 0.60
            for region in existing_regions
        ):
            continue
        regions.append({
            "type": "table",
            "bbox": box,
            "raw": {
                "source": "row_text_financial_table_fallback",
                "qualified_amount_rows": len(group),
                "row_count": last - first + 1,
            },
        })
    return regions


def infer_table_regions_from_text(
    text_elements: list[dict[str, Any]],
    page_size: tuple[int, int],
    existing_regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find financial tables with repeated numeric columns, including unruled ones."""
    import numpy as np

    width, height = page_size
    texts = [item for item in text_elements if item.get("type") == "text"
             and str(item.get("text") or "").strip() and bbox_area(item["bbox"]) > 0]
    rows = group_text_rows(texts)
    if not rows:
        return []
    line_height = float(np.median([bbox_height(item["bbox"]) for item in texts]))
    tolerance = max(8, width * 0.012)
    numeric = re.compile(r"^\(?[+-]?\d[\d., /]*[a-z]?\)?%?$", re.IGNORECASE)
    clusters: list[list[tuple[int, dict[str, Any]]]] = []
    for row_index, row in enumerate(rows):
        for item in row:
            value = str(item["text"]).strip()
            if item["bbox"][0] < width * 0.40 or not numeric.fullmatch(value):
                continue
            right = item["bbox"][2]
            cluster = next((group for group in clusters
                            if abs(right - np.median([entry[1]["bbox"][2] for entry in group])) <= tolerance), None)
            if cluster is None:
                clusters.append([(row_index, item)])
            else:
                cluster.append((row_index, item))
    clusters = [group for group in clusters if len({entry[0] for entry in group}) >= 3]
    if len(clusters) < 2:
        return _infer_financial_table_regions_from_row_text(
            rows, page_size, existing_regions, line_height,
        )
    clusters.sort(key=lambda group: np.median([entry[1]["bbox"][2] for entry in group]))
    anchors = [float(np.median([entry[1]["bbox"][2] for entry in group])) for group in clusters]
    qualified = []
    for index, row in enumerate(rows):
        hits = {column for item in row for column, anchor in enumerate(anchors)
                if abs(item["bbox"][2] - anchor) <= tolerance
                and (numeric.fullmatch(str(item["text"]).strip()) or str(item["text"]).strip() in {"-", "--"})}
        has_description = any(item["bbox"][0] < anchors[0] - width * 0.12 for item in row)
        if len(hits) >= 2 and has_description:
            qualified.append(index)
    groups: list[list[int]] = []
    for index in qualified:
        if groups:
            last = groups[-1][-1]
            gap = min(item["bbox"][1] for item in rows[index]) - max(item["bbox"][3] for item in rows[last])
            if gap <= line_height * 4.5 and not any(
                signature_cue_key(str(item.get("text") or ""))
                for row in rows[last + 1:index] for item in row
            ):
                groups[-1].append(index)
                continue
        groups.append([index])
    regions = []
    for group in groups:
        if len(group) < 3:
            continue
        first, last = group[0], group[-1]
        # Include multi-line column headers but stop at the report title/date.
        for _ in range(3):
            if first <= 0:
                break
            row = rows[first - 1]
            gap = min(item["bbox"][1] for item in rows[first]) - max(item["bbox"][3] for item in row)
            if len(row) < 2 or gap > line_height * 2.8:
                break
            first -= 1
        members = [item for row in rows[first:last + 1] for item in row]
        box = union_bbox([item["bbox"] for item in members], width, height)
        box = clamp_bbox([box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8], width, height)
        if any(region["type"] == "table" and bbox_overlap_ratio(box, region["bbox"]) > 0.6
               for region in existing_regions):
            continue
        local_clusters = [[entry[1] for entry in cluster if first <= entry[0] <= last] for cluster in clusters]
        local_clusters = [cluster for cluster in local_clusters if len(cluster) >= 3]
        if len(local_clusters) < 2:
            continue
        cuts = [box[0]]
        previous_right = max(
            (item["bbox"][2] for item in members
             if item["bbox"][2] < min(entry["bbox"][0] for entry in local_clusters[0]) - tolerance),
            default=box[0],
        )
        for cluster in local_clusters:
            left = min(item["bbox"][0] for item in cluster)
            cuts.append(int(round((previous_right + left) / 2)))
            previous_right = max(item["bbox"][2] for item in cluster)
        cuts.append(box[2])
        if any(b <= a for a, b in zip(cuts, cuts[1:])):
            continue
        regions.append({
            "type": "table", "bbox": box,
            "raw": {"source": "text_layout_table_fallback", "row_count": last - first + 1},
            "column_edges": cuts,
        })
    return regions


class TableCellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[dict[str, int]] = []
        self.row = -1
        self.col = 0
        self.occupied: set[tuple[int, int]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row += 1
            self.col = 0
        elif tag in {"td", "th"}:
            self.row = max(0, self.row)
            attributes = dict(attrs)
            try:
                rowspan = max(1, min(1000, int(attributes.get("rowspan") or 1)))
                colspan = max(1, min(1000, int(attributes.get("colspan") or 1)))
            except ValueError:
                rowspan = colspan = 1
            while (self.row, self.col) in self.occupied:
                self.col += 1
            self.cells.append({"row": self.row, "col": self.col, "rowspan": rowspan, "colspan": colspan})
            for row in range(self.row, self.row + rowspan):
                for col in range(self.col, self.col + colspan):
                    self.occupied.add((row, col))
            self.col += colspan


def table_cells_html(cells: list[dict[str, Any]]) -> str:
    parts = ["<table>"]
    for row in range(max(cell["row"] + cell.get("rowspan", 1) for cell in cells)):
        parts.append("<tr>")
        for cell in sorted((cell for cell in cells if cell["row"] == row), key=lambda cell: cell["col"]):
            attrs = "".join(f' {key}="{cell[key]}"' for key in ("rowspan", "colspan") if cell.get(key, 1) > 1)
            text = escape(str(cell.get("text") or "")).replace("\n", "<br>")
            parts.append(f"<td{attrs}>{text}</td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def is_narrow_table_right_edge_noise(item_bbox: list[int], table_bbox: list[int]) -> bool:
    """Reject narrow OCR fragments from stamps or scan marks at the table edge."""
    table_width = max(1, table_bbox[2] - table_bbox[0])
    return (
        item_bbox[0] >= table_bbox[0] + table_width * 0.95
        and item_bbox[2] - item_bbox[0] <= table_width * 0.04
    )


def fallback_table_cells(
    texts: list[dict[str, Any]], region: dict[str, Any], table_id: str,
) -> list[dict[str, Any]]:
    box = region["bbox"]
    members = [
        item for item in texts
        if item.get("type") == "text"
        and bbox_coverage(item["bbox"], box) >= 0.80
        and not is_narrow_table_right_edge_noise(item["bbox"], box)
    ]
    edges = region.get("column_edges")
    if not members or not edges:
        return []

    visual_rows = []
    for row in group_text_rows(members):
        buckets: list[list[dict[str, Any]]] = [[] for _ in range(len(edges) - 1)]
        for item in row:
            cx, _ = bbox_center(item["bbox"])
            col = len(buckets) - 1
            for candidate, (left, right) in enumerate(zip(edges, edges[1:])):
                if candidate == len(buckets) - 1 or left <= cx < right:
                    col = candidate
                    break
            buckets[col].append(item)
        visual_rows.append({
            "y1": min(item["bbox"][1] for item in row),
            "y2": max(item["bbox"][3] for item in row),
            "buckets": buckets,
        })

    logical_rows: list[list[dict[str, Any]]] = []
    for current in visual_rows:
        if not logical_rows:
            logical_rows.append([current])
            continue

        previous = logical_rows[-1][-1]
        previous_height = max(1, previous["y2"] - previous["y1"])
        current_height = max(1, current["y2"] - current["y1"])
        close_gap = current["y1"] - previous["y2"] <= max(
            4, int(round(min(previous_height, current_height) * 0.30)),
        )
        horizontally_continued = False
        for old_items, new_items in zip(previous["buckets"], current["buckets"]):
            for old_item in old_items:
                for new_item in new_items:
                    old_box = old_item["bbox"]
                    new_box = new_item["bbox"]
                    table_width = max(1, box[2] - box[0])
                    narrow_right_margin_noise = (
                        new_box[0] >= box[0] + table_width * 0.95
                        and new_box[2] - new_box[0] <= table_width * 0.04
                    )
                    if narrow_right_margin_noise:
                        continue
                    overlap = max(0, min(old_box[2], new_box[2]) - max(old_box[0], new_box[0]))
                    min_width = max(1, min(old_box[2] - old_box[0], new_box[2] - new_box[0]))
                    if overlap / min_width >= 0.20:
                        horizontally_continued = True
                        break
                if horizontally_continued:
                    break
            if horizontally_continued:
                break

        code_text = " ".join(
            str(item.get("text") or "").strip()
            for item in current["buckets"][0]
        )
        normalized_code = normalize_financial_identifier_text(code_text, max_digits=3)
        starts_with_code = normalized_code.isdigit() and len(normalized_code) == 3

        def value_hits(profile: dict[str, Any]) -> int:
            return sum(
                any(_looks_like_financial_amount(str(item.get("text") or "")) for item in items)
                for items in profile["buckets"][-2:]
            )

        current_value_hits = value_hits(current)
        previous_value_hits = value_hits(previous)
        starts_with_values = current_value_hits >= 2 or (
            current_value_hits >= 1 and previous_value_hits >= 1
        )
        if close_gap and horizontally_continued and not starts_with_code and not starts_with_values:
            logical_rows[-1].append(current)
        else:
            logical_rows.append([current])

    cells = []
    for row_index, row_parts in enumerate(logical_rows):
        y1 = min(part["y1"] for part in row_parts)
        y2 = max(part["y2"] for part in row_parts)
        for col, (x1, x2) in enumerate(zip(edges, edges[1:])):
            lines = [
                item
                for part in row_parts
                for item in sorted(part["buckets"][col], key=lambda entry: entry["bbox"][0])
            ]
            line_texts = []
            for part in row_parts:
                line_text = " ".join(
                    str(item.get("text") or "").strip()
                    for item in sorted(part["buckets"][col], key=lambda entry: entry["bbox"][0])
                    if str(item.get("text") or "").strip()
                )
                if line_text:
                    line_texts.append(line_text)
            cell_text = "\n".join(line_texts)
            if col == 0:
                cell_text = normalize_financial_identifier_text(cell_text, max_digits=3)
            cells.append({
                "id": f"{table_id}_cell_{len(cells) + 1:03d}",
                "bbox": [x1, y1, x2, y2], "row": row_index, "col": col,
                "text": cell_text,
                "line_count": len(line_texts),
                "lines": lines, "source": "text_layout_cell_fallback",
            })
    return cells


def _looks_like_financial_amount(text: str) -> bool:
    """Loose amount test used only to infer value-column geometry."""
    value = str(text or "").strip()
    if not value:
        return False
    digits = sum(ch.isdigit() for ch in value)
    if digits < 4:
        return False
    allowed = sum(ch.isdigit() or ch in ".,() +-/%–—" for ch in value)
    return allowed / max(1, len(value)) >= 0.72


def _small_integer_token(text: str, max_digits: int = 3) -> str | None:
    value = re.sub(r"[^0-9]", "", str(text or ""))
    raw = str(text or "").strip()
    if not value or len(value) > max_digits:
        return None
    # Reject a noisy description that merely happens to contain a digit.
    non_space = max(1, len(re.sub(r"\\s+", "", raw)))
    if len(value) / non_space < 0.60:
        return None
    return value


def split_table_line_bbox_on_whitespace(
    image: Image.Image,
    bbox: list[int],
) -> list[list[int]]:
    """Split a wide unruled table row into visual column segments.

    Paddle's DB detector can return one long box spanning ``code + description
    + note + current value + prior value``. VietOCR then sees a sentence rather
    than cells. We split only at *large blank vertical gutters*; ordinary spaces
    inside Vietnamese descriptions stay intact.
    """
    import cv2
    import numpy as np

    bbox = clamp_bbox(bbox, *image.size)
    if bbox_area(bbox) <= 0:
        return []
    crop = np.asarray(image.crop(tuple(bbox)).convert("L"), dtype=np.uint8)
    if crop.size == 0:
        return []
    h, w = crop.shape
    if w < max(24, h * 2):
        return [bbox]

    blur = cv2.GaussianBlur(crop, (3, 3), 0)
    _, ink = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Underlines/double-rules under totals can bridge two numeric columns and
    # make the whitespace gutter disappear. Remove only long horizontal strokes
    # before computing the vertical projection; normal glyph strokes are far
    # shorter than this kernel.
    horizontal_kernel = np.ones((1, max(18, w // 12)), dtype=np.uint8)
    horizontal_rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal_kernel)
    projection_ink = cv2.bitwise_and(ink, cv2.bitwise_not(horizontal_rules))
    # A column is active when it contains a small but non-trivial amount of ink.
    active = np.count_nonzero(projection_ink, axis=0) >= max(1, int(round(h * 0.035)))
    indices = np.flatnonzero(active)
    if indices.size == 0:
        return [bbox]

    runs: list[list[int]] = []
    start = previous = int(indices[0])
    for current_raw in indices[1:]:
        current = int(current_raw)
        if current == previous + 1:
            previous = current
            continue
        runs.append([start, previous + 1])
        start = previous = current
    runs.append([start, previous + 1])

    # At 300 DPI, normal word spaces are much smaller than one text-line height.
    # A gap close to a full line height is therefore a strong table-gutter cue.
    min_gap = max(14, int(round(h * 0.90)))
    groups: list[list[int]] = [runs[0][:]]
    for run in runs[1:]:
        gap = run[0] - groups[-1][1]
        if gap >= min_gap:
            groups.append(run[:])
        else:
            groups[-1][1] = run[1]

    out: list[list[int]] = []
    for x1, x2 in groups:
        if x2 - x1 < 3:
            continue
        segment = [bbox[0] + x1, bbox[1], bbox[0] + x2, bbox[3]]
        if bbox_area(segment) > 0:
            out.append(segment)
    return out or [bbox]


def _two_value_column_centers(
    segments: list[dict[str, Any]],
    table_bbox: list[int],
) -> tuple[float, float] | None:
    """Infer the two amount columns using a tiny 1-D two-cluster fit."""
    import numpy as np

    x1, _, x2, _ = table_bbox
    width = max(1, x2 - x1)
    points = []
    for item in segments:
        if not _looks_like_financial_amount(str(item.get("text") or "")):
            continue
        cx, _ = bbox_center(item["bbox"])
        if cx < x1 + width * 0.55:
            continue
        points.append(float(cx))
    if len(points) < 6:
        return None

    values = np.asarray(points, dtype=np.float64)
    c1 = float(np.percentile(values, 25))
    c2 = float(np.percentile(values, 75))
    if abs(c2 - c1) < width * 0.08:
        c1, c2 = float(values.min()), float(values.max())
    for _ in range(12):
        left = values[np.abs(values - c1) <= np.abs(values - c2)]
        right = values[np.abs(values - c1) > np.abs(values - c2)]
        if len(left) < 2 or len(right) < 2:
            return None
        new_c1 = float(np.median(left))
        new_c2 = float(np.median(right))
        if abs(new_c1 - c1) < 0.1 and abs(new_c2 - c2) < 0.1:
            break
        c1, c2 = new_c1, new_c2
    current, previous = sorted((c1, c2))
    if previous - current < width * 0.10:
        return None
    return current, previous


def infer_unruled_financial_column_edges(
    segments: list[dict[str, Any]],
    table_bbox: list[int],
) -> list[int]:
    """Return five semantic columns: code, description, note, current, prior."""
    import numpy as np

    x1, _, x2, _ = table_bbox
    width = max(1, x2 - x1)
    defaults = [
        x1,
        x1 + width * 0.09,
        x1 + width * 0.50,
        x1 + width * 0.64,
        x1 + width * 0.82,
        x2,
    ]
    value_centers = _two_value_column_centers(segments, table_bbox)
    if value_centers is None:
        return [int(round(v)) for v in defaults]
    current_center, previous_center = value_centers

    note_points = []
    for item in segments:
        token = _small_integer_token(str(item.get("text") or ""), max_digits=2)
        if token is None:
            continue
        cx, _ = bbox_center(item["bbox"])
        if x1 + width * 0.42 <= cx <= current_center - width * 0.05:
            note_points.append(cx)
    note_center = float(np.median(note_points)) if len(note_points) >= 2 else x1 + width * 0.56

    # Infer the left code/description split from repeated 3-digit account codes
    # when possible. This is more robust than a fixed percentage when scans
    # contain binder marks or margin noise that enlarge the detected table bbox.
    code_boxes = []
    for item in segments:
        normalized_code = normalize_financial_identifier_text(str(item.get("text") or ""), max_digits=3)
        if not (normalized_code.isdigit() and len(normalized_code) == 3):
            continue
        cx, _ = bbox_center(item["bbox"])
        if cx <= x1 + width * 0.28:
            code_boxes.append(item["bbox"])
    if len(code_boxes) >= 3:
        code_right = float(np.median([box[2] for box in code_boxes]))
        description_lefts = []
        for item in segments:
            text = str(item.get("text") or "")
            if sum(ch.isalpha() for ch in text) < 2:
                continue
            if item["bbox"][0] <= code_right:
                continue
            cx, _ = bbox_center(item["bbox"])
            if cx <= x1 + width * 0.52:
                description_lefts.append(item["bbox"][0])
        if description_lefts:
            desc_left = float(np.percentile(description_lefts, 25))
            code_desc = (code_right + desc_left) / 2.0
        else:
            code_desc = code_right + width * 0.02
        code_desc = max(x1 + width * 0.06, min(x1 + width * 0.18, code_desc))
    else:
        code_desc = x1 + width * 0.09

    desc_note = max(x1 + width * 0.43, min(x1 + width * 0.55, note_center - width * 0.055))
    note_current = (note_center + current_center) / 2.0
    current_previous = (current_center + previous_center) / 2.0
    edges = [x1, code_desc, desc_note, note_current, current_previous, x2]
    # Defensive monotonicity; defaults are safer than malformed geometry.
    if any(b <= a + 4 for a, b in zip(edges, edges[1:])):
        edges = defaults
    return [int(round(v)) for v in edges]


_FINANCIAL_AMOUNT_GLYPHS = "0-9Il|!¡Oo"
_FINANCIAL_GROUPED_AMOUNT_RE = re.compile(
    rf"(?<![0-9A-Za-z])(?P<open>\()?\s*(?P<sign>[-−–—])?\s*"
    rf"(?P<body>[{_FINANCIAL_AMOUNT_GLYPHS}]{{1,3}}"
    rf"(?:[.,][{_FINANCIAL_AMOUNT_GLYPHS}]{{3}}){{1,5}})"
    rf"(?P<close>\))?(?![0-9A-Za-z])"
)
_FINANCIAL_UNGROUPED_AMOUNT_RE = re.compile(
    rf"^\s*(?P<open>\()?\s*(?P<sign>[-−–—])?\s*"
    rf"(?P<body>[{_FINANCIAL_AMOUNT_GLYPHS}]{{4,15}})"
    rf"(?P<close>\))?\s*$"
)


def _normalize_financial_amount_match(match: re.Match[str]) -> str | None:
    """Normalize one already-isolated amount token without absorbing edge noise."""
    body = str(match.group("body") or "")
    mapped_body = body.translate(OCR_DIGIT_MAP)
    digits = re.sub(r"\D", "", mapped_body)
    if len(digits) < 4:
        return None
    first = len(digits) % 3 or 3
    groups = [digits[:first]]
    groups.extend(digits[index:index + 3] for index in range(first, len(digits), 3))
    grouped = ".".join(groups)
    # OCR often drops only one side of accounting parentheses. Either surviving
    # parenthesis is sufficient evidence of a negative amount, but unrelated
    # alphabetic fragments outside this regex are never translated into digits.
    if match.group("open") or match.group("close"):
        return f"({grouped})"
    if match.group("sign"):
        return f"-{grouped}"
    return grouped


def _financial_amount_candidates(text: str) -> list[tuple[str, int, int]]:
    """Return isolated amount candidates as (normalized, digits, groups)."""
    value = str(text or "")
    out: list[tuple[str, int, int]] = []
    for match in _FINANCIAL_GROUPED_AMOUNT_RE.finditer(value):
        normalized = _normalize_financial_amount_match(match)
        if normalized is None:
            continue
        body = str(match.group("body") or "")
        digit_count = len(re.sub(r"\D", "", body.translate(OCR_DIGIT_MAP)))
        group_count = body.count(".") + body.count(",") + 1
        out.append((normalized, digit_count, group_count))
    if out:
        return out
    # Keep support for rare ungrouped values, but only when the WHOLE OCR token
    # is amount-like. This prevents fragments such as "Il 1.001..." from being
    # merged into the monetary value.
    match = _FINANCIAL_UNGROUPED_AMOUNT_RE.fullmatch(value)
    if match is not None:
        normalized = _normalize_financial_amount_match(match)
        if normalized is not None:
            body = str(match.group("body") or "")
            digit_count = len(re.sub(r"\D", "", body.translate(OCR_DIGIT_MAP)))
            out.append((normalized, digit_count, 1))
    return out


def normalize_financial_amount_text(text: str) -> str:
    """Conservative cleanup for one monetary token after OCR."""
    value = str(text or "").strip()
    if not value or "%" in value:
        return value
    if re.fullmatch(r"[-−–—]+", value):
        return "-"
    if value.count("/") >= 2 and not any(sep in value for sep in ".,"):
        return value
    candidates = _financial_amount_candidates(value)
    if not candidates:
        return value
    # Prefer a properly grouped candidate, then the candidate with the most
    # digits. Crucially, characters outside the matched token are ignored.
    return max(candidates, key=lambda item: (item[2], item[1]))[0]


def extract_best_financial_amount(lines: list[dict[str, Any]]) -> str | None:
    """Select one isolated amount token; never concatenate OCR edge fragments."""
    candidates: list[tuple[int, int, int, str]] = []
    for line_index, line in enumerate(lines):
        raw = str(line.get("text") or "").strip()
        if not raw:
            continue
        for normalized, digit_count, group_count in _financial_amount_candidates(raw):
            candidates.append((group_count, digit_count, -line_index, normalized))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[:3])[3]


def apply_common_financial_text_fixes(text: str) -> str:
    corrected = str(text or "")
    for pattern, replacement in COMMON_FINANCIAL_TEXT_FIXES:
        corrected = re.sub(pattern, replacement, corrected, flags=re.IGNORECASE)
    return corrected


def canonicalize_financial_description(text: str) -> str:
    """Repair a label only when its words closely match a canonical BCTC label."""
    value = apply_common_financial_text_fixes(str(text or "").strip())
    if not value:
        return value
    normalized = ascii_upper(value)
    major_prefix = re.match(r"^\s*\d*([AB])[.]?\s", normalized)
    numeric_prefix = None if major_prefix else re.match(r"^\s*[\d.,]+\s", normalized)

    def same_prefix_family(canonical: str) -> bool:
        canonical_normalized = ascii_upper(canonical)
        if numeric_prefix:
            return bool(re.match(r"^\d+[.]\s", canonical_normalized))
        if major_prefix:
            return canonical_normalized.startswith(major_prefix.group(1) + ".")
        return bool(re.match(r"^[IVX]+[.]\s", canonical_normalized))

    best: tuple[float, str] | None = None
    for canonical in CANONICAL_FINANCIAL_DESCRIPTIONS:
        if not same_prefix_family(canonical):
            continue
        score = SequenceMatcher(None, normalized, ascii_upper(canonical)).ratio()
        if best is None or score > best[0]:
            best = (score, canonical)
    if best is None:
        return value
    score, canonical = best
    canonical_normalized = ascii_upper(canonical)
    length_ratio = min(len(normalized), len(canonical_normalized)) / max(
        1, max(len(normalized), len(canonical_normalized))
    )
    if score >= 0.84 and length_ratio >= 0.72:
        return canonical
    return value


def _roman_to_int(value: str) -> int | None:
    token = str(value or "").upper()
    if not token or not re.fullmatch(r"[IVXLCDM]+", token):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(token):
        current = values[character]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def _int_to_roman(value: int) -> str:
    if value <= 0:
        return ""
    pairs = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    out = []
    remaining = int(value)
    for number, token in pairs:
        while remaining >= number:
            out.append(token)
            remaining -= number
    return "".join(out)


def repair_financial_roman_heading_sequence(cells: list[dict[str, Any]]) -> None:
    """Guard top-level Roman headings after OCR second opinions.

    The repair is conservative. It activates only after an explicit major
    section label such as ``A.`` or ``B.`` and only on 3-digit parent codes
    ending in zero. A heading is changed only when its body closely maps to a
    canonical financial label; sequence position is then used as an additional
    consistency check. Unknown headings are left untouched.
    """
    rows: dict[int, dict[str, dict[str, Any]]] = {}
    for cell in cells:
        row = cell.get("row")
        semantic_col = cell.get("semantic_col")
        if isinstance(row, int) and isinstance(semantic_col, str):
            rows.setdefault(row, {})[semantic_col] = cell

    roman_canonicals: list[tuple[int, str, str]] = []
    for canonical in CANONICAL_FINANCIAL_DESCRIPTIONS:
        match = re.match(r"^([IVXLCDM]+)[.]\s+(.+)$", canonical, flags=re.IGNORECASE)
        if not match:
            continue
        number = _roman_to_int(match.group(1))
        if number is not None:
            roman_canonicals.append((number, match.group(2), canonical))

    active_major = False
    expected = 1
    for row in sorted(rows):
        row_cells = rows[row]
        description_cell = row_cells.get("description")
        if description_cell is None:
            continue
        description = str(description_cell.get("text") or "").strip()
        normalized = ascii_upper(description)
        if re.match(r"^[AB][.]\s", normalized):
            active_major = True
            expected = 1
            continue
        if not active_major:
            continue

        code = str(row_cells.get("code", {}).get("text") or "").strip()
        if not (re.fullmatch(r"\d{3}", code) and int(code) % 10 == 0):
            continue
        heading = re.match(r"^([IVXLCDM]+)[.]\s+(.+)$", description, flags=re.IGNORECASE)
        if not heading:
            continue
        actual_number = _roman_to_int(heading.group(1))
        if actual_number is None:
            continue
        body = heading.group(2).strip()
        body_key = ascii_upper(body)

        best: tuple[float, int, str] | None = None
        for target_number, canonical_body, canonical_full in roman_canonicals:
            score = SequenceMatcher(None, body_key, ascii_upper(canonical_body)).ratio()
            if best is None or score > best[0]:
                best = (score, target_number, canonical_full)

        target_number = actual_number
        target_text = description
        if best is not None and best[0] >= 0.90:
            _, canonical_number, canonical_full = best
            # Exact/near-exact semantic body is stronger than a potentially
            # wrong OCR Roman prefix. The expected sequence provides a second
            # signal and is recorded for provenance.
            if canonical_number != actual_number:
                target_number = canonical_number
                target_text = canonical_full
                apply_refined_cell_text(
                    description_cell, target_text, "financial_roman_sequence_guard",
                )
                description_cell["roman_sequence_verification"] = {
                    "actual": _int_to_roman(actual_number),
                    "expected": _int_to_roman(expected),
                    "canonical": _int_to_roman(canonical_number),
                    "body_similarity": round(best[0], 4),
                    "status": "repaired",
                }
            else:
                description_cell["roman_sequence_verification"] = {
                    "actual": _int_to_roman(actual_number),
                    "expected": _int_to_roman(expected),
                    "canonical": _int_to_roman(canonical_number),
                    "body_similarity": round(best[0], 4),
                    "status": "consistent" if actual_number == expected else "sequence_gap_preserved",
                }
        expected = max(expected + 1, target_number + 1)


def extract_best_financial_identifier(
    lines: list[dict[str, Any]],
    max_digits: int,
) -> str | None:
    candidates = []
    for line in lines:
        raw = str(line.get("text") or "").strip()
        normalized = normalize_financial_identifier_text(raw, max_digits=max_digits)
        if normalized.isdigit() and len(normalized) == max_digits:
            candidates.append(normalized)
    return candidates[0] if candidates else None


def financial_description_suspicious(text: str) -> bool:
    value = str(text or "").strip()
    normalized = ascii_upper(value)
    commonly_fixed = apply_common_financial_text_fixes(value)
    return bool(
        commonly_fixed != value
        or canonicalize_financial_description(commonly_fixed) != commonly_fixed
        or re.match(r"^\d{2,}[.,]?\s", normalized)
        or re.search(r"\s(?:TRI|THAI|NGUOI|\d+)\s*$", normalized)
    )


def financial_description_quality(text: str) -> tuple[int, float]:
    value = canonicalize_financial_description(text)
    normalized = ascii_upper(value)
    exact = int(any(normalized == ascii_upper(item) for item in CANONICAL_FINANCIAL_DESCRIPTIONS))
    penalty = 0.0
    penalty += 2.0 if re.match(r"^\d{2,}[.,]?\s", normalized) else 0.0
    penalty += 2.0 if re.search(r"\s(?:TRI|THAI|NGUOI)\s*$", normalized) else 0.0
    penalty += 2.0 if len(value) >= 6 and len(value.split()) < 2 else 0.0
    return exact, -penalty


def apply_refined_cell_text(
    cell: dict[str, Any],
    final_text: str,
    refinement: str,
) -> None:
    """Keep final cell and line text consistent while retaining raw OCR."""
    lines = cell.get("lines") if isinstance(cell.get("lines"), list) else []
    current_text = str(cell.get("text") or "").strip()
    if not current_text:
        current_text = "\n".join(
            str(line.get("text") or "").strip()
            for line in lines
            if isinstance(line, dict) and str(line.get("text") or "").strip()
        )
    if current_text and current_text != final_text:
        cell.setdefault("raw_text", current_text)
    cell["text"] = final_text
    cell["text_refinement"] = refinement
    if len(lines) == 1 and isinstance(lines[0], dict):
        line_text = str(lines[0].get("text") or "").strip()
        if line_text and line_text != final_text:
            lines[0].setdefault("raw_text", line_text)
        lines[0]["text"] = final_text
        lines[0]["text_refinement"] = refinement


def parse_financial_amount(text: str) -> int | None:
    value = str(text or "").strip()
    if not value or value == "-":
        return None
    candidates = _financial_amount_candidates(value)
    if not candidates:
        return None
    normalized = max(candidates, key=lambda item: (item[2], item[1]))[0]
    digits = re.sub(r"\D", "", normalized)
    if len(digits) < 4:
        return None
    negative = normalized.startswith("(") or normalized.startswith("-")
    return -int(digits) if negative else int(digits)


def _paddle_amount_candidate(
    image: Image.Image,
    text_detector: PaddleTextDetector,
) -> tuple[str | None, str, float | None]:
    try:
        raw, confidence = text_detector.recognize_text(image)
    except Exception as exc:
        print(f"[warn] Paddle cell recognition failed: {exc}", flush=True)
        return None, "", None
    candidate = extract_best_financial_amount([{"text": raw}])
    return candidate, raw, confidence


def accounting_cell_equations(
    cells: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]], str]]:
    """Infer only explicit parent/child relationships from table row structure."""
    rows: dict[int, dict[str, dict[str, Any]]] = {}
    for cell in cells:
        row = cell.get("row")
        semantic_col = cell.get("semantic_col")
        if isinstance(row, int) and isinstance(semantic_col, str):
            rows.setdefault(row, {})[semantic_col] = cell
    ordered = [(row, rows[row]) for row in sorted(rows)]
    equations: list[tuple[dict[str, Any], list[dict[str, Any]], str]] = []

    for index, (_, row_cells) in enumerate(ordered):
        code_cell = row_cells.get("code")
        description_cell = row_cells.get("description")
        if not code_cell or not description_cell:
            continue
        code_text = str(code_cell.get("text") or "")
        description = str(description_cell.get("text") or "").strip()
        if not (code_text.isdigit() and len(code_text) == 3):
            continue

        dash_children = []
        for _, candidate_row in ordered[index + 1:]:
            candidate_description = str(
                candidate_row.get("description", {}).get("text") or ""
            ).strip()
            if not candidate_description.startswith("-"):
                break
            dash_children.append(candidate_row)
        if len(dash_children) >= 2:
            equations.append((row_cells, dash_children, "detail_sum"))

        parent_code = int(code_text)
        if parent_code % 10 != 0:
            continue
        direct_children = []
        for _, candidate_row in ordered[index + 1:]:
            candidate_code_text = str(candidate_row.get("code", {}).get("text") or "")
            if not (candidate_code_text.isdigit() and len(candidate_code_text) == 3):
                continue
            candidate_code = int(candidate_code_text)
            if candidate_code <= parent_code:
                continue
            if candidate_code >= parent_code + 10:
                break
            candidate_description = str(
                candidate_row.get("description", {}).get("text") or ""
            ).strip()
            if re.match(r"^\d+[.]\s", candidate_description):
                direct_children.append(candidate_row)
        if direct_children:
            equations.append((row_cells, direct_children, "section_sum"))
    return equations


def verify_accounting_cells_with_paddle(
    cells: list[dict[str, Any]],
    cell_images: dict[str, Image.Image],
    text_detector: PaddleTextDetector,
) -> None:
    """Use equations to request OCR again, never to synthesize a number."""
    paddle_cache: dict[str, tuple[str | None, str, float | None]] = {}
    for parent_row, child_rows, relation in accounting_cell_equations(cells):
        for semantic_col in ("current", "previous"):
            parent = parent_row.get(semantic_col)
            children = [row.get(semantic_col) for row in child_rows]
            if parent is None or any(child is None for child in children):
                continue
            involved = [parent, *children]
            values = [parse_financial_amount(str(cell.get("text") or "")) for cell in involved]
            if any(value is None for value in values):
                continue
            if values[0] == sum(values[1:]):
                continue

            replacements: list[tuple[dict[str, Any], str, str, float | None]] = []
            for cell_index, cell in enumerate(involved):
                cell_id = str(cell.get("id") or "")
                image = cell_images.get(cell_id)
                if image is None:
                    continue
                if cell_id not in paddle_cache:
                    paddle_cache[cell_id] = _paddle_amount_candidate(image, text_detector)
                candidate, raw, confidence = paddle_cache[cell_id]
                candidate_value = parse_financial_amount(candidate or "")
                if candidate_value is None or candidate == cell.get("text"):
                    continue
                trial = list(values)
                trial[cell_index] = candidate_value
                if trial[0] == sum(trial[1:]):
                    replacements.append((cell, candidate, raw, confidence))

            if len(replacements) != 1:
                for cell in involved:
                    cell["accounting_verification"] = {
                        "relation": relation,
                        "status": "mismatch_unresolved",
                    }
                continue
            cell, candidate, raw, confidence = replacements[0]
            old_value = str(cell.get("text") or "")
            cell["raw_text"] = old_value
            cell["text"] = candidate
            cell["text_refinement"] = "paddleocr_accounting_second_opinion"
            cell["second_opinion"] = {
                "recognizer": "paddleocr",
                "raw_text": raw,
                "confidence": confidence,
                "reason": relation,
            }
            for involved_cell in involved:
                involved_cell["accounting_verification"] = {
                    "relation": relation,
                    "status": "matched_after_reocr",
                }


def normalize_financial_identifier_text(text: str, max_digits: int) -> str:
    """Normalize code/note cells only when the OCR token is digit-like."""
    value = str(text or "").strip()
    if max_digits == 3:
        code_with_noise_suffix = re.fullmatch(r"(\d{3})\s+[A-Za-z]{1,2}", value)
        if code_with_noise_suffix:
            return code_with_noise_suffix.group(1)
    mapped = value.translate(str.maketrans({
        "I": "1", "l": "1", "|": "1", "!": "1", "¡": "1",
        "O": "0", "o": "0",
    }))
    compact = re.sub(r"[\s.,:;]+", "", mapped)
    if compact.isdigit() and 1 <= len(compact) <= max_digits:
        return compact
    return value


def refine_suspicious_table_texts(
    original: Image.Image,
    texts: list[dict[str, Any]],
    vietocr: VietOCRRecognizer,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Retry only table lines whose OCR has a strong, testable error signal."""
    refined = [dict(item) for item in texts]
    retry_indices: list[int] = []
    retry_kinds: list[str] = []
    retry_images: list[Image.Image] = []

    for index, item in enumerate(refined):
        value = str(item.get("text") or "").strip()
        normalized = ascii_upper(value)
        kind = ""
        if re.fullmatch(r"0\d{0,2}(?:[.,]\d{3}){2,5}", value):
            kind = "leading_zero_amount"
        elif "TAI SAN DAI HANH" in normalized:
            kind = "long_term_heading"
        elif re.match(r"^[IVX]{1,4}L\s", normalized):
            kind = "roman_heading"
        if not kind or not item.get("bbox"):
            continue
        retry_indices.append(index)
        retry_kinds.append(kind)
        retry_images.append(crop_image_memory(original, item["bbox"], padding=12))

    if retry_images:
        predictions = vietocr.predict_batch(retry_images, batch_size=batch_size)
        for index, kind, prediction in zip(retry_indices, retry_kinds, predictions):
            candidate = prediction[0] if isinstance(prediction, (list, tuple)) else prediction
            candidate = str(candidate or "").strip()
            old_value = str(refined[index].get("text") or "").strip()
            old_normalized = ascii_upper(old_value)
            candidate_normalized = ascii_upper(candidate)
            accept = False
            if kind == "leading_zero_amount":
                candidate_amount = normalize_financial_amount_text(candidate)
                old_digits = re.sub(r"\D", "", old_value)
                candidate_digits = re.sub(r"\D", "", candidate_amount)
                accept = (
                    bool(candidate_digits)
                    and not candidate_digits.startswith("0")
                    and (
                        len(candidate_digits) >= len(old_digits)
                        or candidate_digits == old_digits.lstrip("0")
                    )
                )
                candidate = candidate_amount
            elif kind == "long_term_heading":
                accept = (
                    "TAI SAN DAI HAN" in candidate_normalized
                    and "TAI SAN DAI HANH" not in candidate_normalized
                )
            elif kind == "roman_heading":
                accept = bool(re.match(r"^[IVX]{1,4}\.\s", candidate_normalized))
            if accept and candidate and candidate != old_value:
                refined[index]["raw_text"] = old_value
                refined[index]["text"] = candidate
                refined[index]["text_refinement"] = "vietocr_wide_table_padding"

    for item in refined:
        value = str(item.get("text") or "")
        corrected = apply_common_financial_text_fixes(value)
        if corrected != value:
            item.setdefault("raw_text", value)
            item["text"] = corrected
            item["text_refinement"] = "financial_table_vocabulary"
    return refined


def ocr_unruled_financial_table_cells(
    recognition_crop: Image.Image,
    table_bbox: list[int],
    table_id: str,
    text_detector: PaddleTextDetector,
    vietocr: VietOCRRecognizer,
    vietocr_batch_size: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """OCR an unruled financial table even when PP-Structure returns no cells."""
    line_items = text_detector.detect_lines(recognition_crop)
    crops: list[TextCrop] = []
    counter = 0
    for line_index, line in enumerate(line_items, start=1):
        line_box = clamp_bbox(line["bbox"], *recognition_crop.size)
        if bbox_area(line_box) <= 0:
            continue
        for segment_box in split_table_line_bbox_on_whitespace(recognition_crop, line_box):
            # A few pixels of context are important for thin leading digits
            # (especially ``1``); cropping exactly at the ink boundary can make
            # recognizers drop the first digit. Padding stays well inside the
            # large inter-column gutter used to create the segment.
            ocr_box = clamp_bbox(
                [segment_box[0] - 4, segment_box[1] - 2, segment_box[2] + 4, segment_box[3] + 2],
                *recognition_crop.size,
            )
            segment_image = recognition_crop.crop(tuple(ocr_box))
            if not crop_has_meaningful_ink(segment_image):
                continue
            counter += 1
            abs_box = [
                table_bbox[0] + ocr_box[0],
                table_bbox[1] + ocr_box[1],
                table_bbox[0] + ocr_box[2],
                table_bbox[1] + ocr_box[3],
            ]
            crops.append(TextCrop(
                id=f"{table_id}_fallback_seg_{counter:04d}",
                bbox=abs_box,
                image=segment_image,
                source="unruled_table_whitespace_segment",
                extra={"line_seed": line_index},
            ))
    if not crops:
        return [], {"reason": "no_segments"}

    recognized = recognize_text_crops(crops, vietocr, batch_size=vietocr_batch_size)
    recognized = [item for item in recognized if str(item.get("text") or "").strip()]
    if not recognized:
        return [], {"reason": "no_recognized_segments"}

    rows = group_text_rows(recognized)
    if len(rows) < 4:
        return [], {"reason": "too_few_rows", "row_count": len(rows)}

    edges = infer_unruled_financial_column_edges(recognized, table_bbox)

    # A total row often has long underlines that visually bridge the current and
    # prior-year amounts. If a recognized segment still crosses a learned value
    # boundary, split it at that boundary and OCR the pieces once more. This is
    # geometry-driven, so it also fixes headers such as ``VND | VND`` without
    # relying on a particular OCR string.
    value_boundaries = edges[3:5]
    refined: list[dict[str, Any]] = []
    recrops: list[TextCrop] = []
    for item in recognized:
        cuts = [edge for edge in value_boundaries
                if item["bbox"][0] + 10 < edge < item["bbox"][2] - 10]
        if not cuts:
            refined.append(item)
            continue
        bounds = [item["bbox"][0], *cuts, item["bbox"][2]]
        for part_index, (left, right) in enumerate(zip(bounds, bounds[1:]), start=1):
            local = clamp_bbox(
                [left - table_bbox[0], item["bbox"][1] - table_bbox[1] - 2,
                 right - table_bbox[0], item["bbox"][3] - table_bbox[1] + 2],
                *recognition_crop.size,
            )
            if bbox_area(local) <= 0:
                continue
            part_image = recognition_crop.crop(tuple(local))
            if not crop_has_meaningful_ink(part_image):
                continue
            abs_box = [
                table_bbox[0] + local[0], table_bbox[1] + local[1],
                table_bbox[0] + local[2], table_bbox[1] + local[3],
            ]
            recrops.append(TextCrop(
                id=f"{item['id']}_refine_{part_index}",
                bbox=abs_box,
                image=part_image,
                source="unruled_table_value_boundary_refine",
                extra={"line_seed": item.get("line_seed")},
            ))
    if recrops:
        refined.extend(recognize_text_crops(recrops, vietocr, batch_size=vietocr_batch_size))
        recognized = [item for item in refined if str(item.get("text") or "").strip()]
        rows = group_text_rows(recognized)
        edges = infer_unruled_financial_column_edges(recognized, table_bbox)

    cells: list[dict[str, Any]] = []
    strong_value_rows = 0
    for row_index, row in enumerate(rows):
        row = sorted(row, key=lambda item: item["bbox"][0])
        y1 = min(item["bbox"][1] for item in row)
        y2 = max(item["bbox"][3] for item in row)
        buckets: list[list[dict[str, Any]]] = [[] for _ in range(5)]
        for item in row:
            cx, _ = bbox_center(item["bbox"])
            col = 4
            for candidate, (left, right) in enumerate(zip(edges, edges[1:])):
                if candidate == 4 or left <= cx < right:
                    col = candidate
                    break
            buckets[col].append(item)

        value_hits = sum(
            1 for col in (3, 4)
            if any(_looks_like_financial_amount(str(item.get("text") or "")) for item in buckets[col])
        )
        if value_hits >= 2:
            strong_value_rows += 1

        for col, items in enumerate(buckets):
            text = " ".join(str(item.get("text") or "").strip() for item in items if str(item.get("text") or "").strip())
            if col == 0:
                text = normalize_financial_identifier_text(text, max_digits=3)
                if text.isdigit() and len(text) != 3:
                    text = ""
            elif col == 2:
                text = normalize_financial_identifier_text(text, max_digits=2)
            elif col in (3, 4):
                text = normalize_financial_amount_text(text)
            cell = {
                "id": f"{table_id}_cell_{len(cells) + 1:03d}",
                "bbox": [edges[col], y1, edges[col + 1], y2],
                "row": row_index,
                "col": col,
                "semantic_col": ("code", "description", "note", "current", "previous")[col],
                "text": text,
                "lines": items,
                "source": "unruled_financial_table_whitespace_fallback",
            }
            cells.append(cell)

    # Avoid turning arbitrary prose into a five-column table merely because a
    # layout model produced a false table bbox.
    if strong_value_rows < 3:
        return [], {
            "reason": "insufficient_two_value_rows",
            "row_count": len(rows),
            "strong_value_rows": strong_value_rows,
            "column_edges": edges,
        }
    return cells, {
        "row_count": len(rows),
        "strong_value_rows": strong_value_rows,
        "column_edges": edges,
        "segment_count": len(recognized),
    }


def remove_table_rules(image: Image.Image) -> Image.Image:
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ink = (gray < 180).astype(np.uint8)
    height, width = ink.shape
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(40, width // 8)), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((max(40, height // 8), 1), np.uint8))
    rules = (horizontal | vertical) > 0
    clean = rgb.copy()
    clean[rules] = 255
    return Image.fromarray(clean)


def detect_isolated_financial_dash(image: Image.Image) -> tuple[bool, dict[str, Any]]:
    """Detect a lone accounting dash when text recognition returns no token.

    The detector is deliberately geometry-only: a real placeholder dash is a
    short, thin horizontal component surrounded by mostly blank pixels. Long
    table rules are rejected by width ratio, while page-edge scan/stamp noise is
    ignored. This is used only after amount OCR failed, so it cannot overwrite a
    recognized number.
    """
    import cv2
    import numpy as np

    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    if gray.size == 0:
        return False, {"reason": "empty"}
    height, width = gray.shape
    if width < 12 or height < 8:
        return False, {"reason": "too_small"}

    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(
        blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8,
    )
    min_area = max(3, int(round(width * height * 0.00035)))
    edge_x = max(2, int(round(width * 0.055)))
    edge_y = max(1, int(round(height * 0.06)))
    significant: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for label_id in range(1, count):
        x, y, w, h, area = [int(v) for v in stats[label_id]]
        if area < min_area:
            continue
        cx, cy = [float(v) for v in centroids[label_id]]
        touches_side_edge = x <= edge_x or x + w >= width - edge_x
        touches_vertical_edge = y <= edge_y or y + h >= height - edge_y
        # Margin artefacts are common in scanned reports and must not veto a
        # clean dash elsewhere in the value cell.
        if touches_side_edge or touches_vertical_edge:
            continue
        aspect = w / max(1.0, float(h))
        width_ratio = w / max(1.0, float(width))
        height_ratio = h / max(1.0, float(height))
        component = {
            "bbox": [x, y, x + w, y + h],
            "area": area,
            "aspect": round(aspect, 3),
            "width_ratio": round(width_ratio, 4),
            "height_ratio": round(height_ratio, 4),
            "center": [round(cx, 2), round(cy, 2)],
        }
        significant.append(component)
        if (
            aspect >= 1.8
            and 0.012 <= width_ratio <= 0.18
            and height_ratio <= 0.30
            and 0.18 <= cy / max(1.0, float(height)) <= 0.86
        ):
            candidates.append(component)

    if not candidates:
        return False, {
            "reason": "no_short_horizontal_component",
            "significant_components": len(significant),
        }

    candidate = max(candidates, key=lambda item: (item["aspect"], item["area"]))
    total_area = sum(int(item["area"]) for item in significant)
    dominance = float(candidate["area"]) / max(1.0, float(total_area))
    valid = len(significant) <= 3 and dominance >= 0.34
    return bool(valid), {
        "reason": "accepted" if valid else "too_much_other_ink",
        "candidate": candidate,
        "significant_components": len(significant),
        "candidate_ink_dominance": round(dominance, 4),
    }


def ocr_cells_from_geometry(
    original: Image.Image,
    cells: list[dict[str, Any]],
    text_detector: PaddleTextDetector,
    vietocr: VietOCRRecognizer,
    batch_size: int,
    table_bbox: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Use cell geometry only; rebuild every cell value from cleaned pixels."""
    table_bbox = table_bbox or [0, 0, *original.size]
    routing_occupied = {
        str(cell["id"]): bool(cell.get("lines"))
        for cell in cells
    }
    output = [{**cell, "text": "", "line_count": 0, "lines": []} for cell in cells]
    crops: list[TextCrop] = []
    cell_images: dict[str, Image.Image] = {}

    row_bounds: dict[int, tuple[int, int]] = {}
    row_profiles: list[tuple[int, int, int]] = []
    for row in sorted({cell.get("row") for cell in output if isinstance(cell.get("row"), int)}):
        members = [cell["bbox"] for cell in output if cell.get("row") == row]
        row_profiles.append((row, min(box[1] for box in members), max(box[3] for box in members)))
    for index, (row, y1, y2) in enumerate(row_profiles):
        previous_y2 = row_profiles[index - 1][2] if index > 0 else y1 - 16
        next_y1 = row_profiles[index + 1][1] if index + 1 < len(row_profiles) else y2 + 16
        top_pad = min(8, max(0, (y1 - previous_y2) // 2))
        bottom_pad = min(8, max(0, (next_y1 - y2) // 2))
        row_bounds[row] = (y1 - top_pad, y2 + bottom_pad)

    for cell in output:
        cell_box = clamp_bbox(cell["bbox"], *original.size)
        cell["bbox"] = cell_box
        if bbox_area(cell_box) <= 0:
            continue
        row_y1, row_y2 = row_bounds.get(cell.get("row"), (cell_box[1], cell_box[3]))
        work_box = clamp_bbox(
            [cell_box[0], row_y1, cell_box[2], row_y2],
            *original.size,
        )
        clean = remove_table_rules(crop_image_memory(original, work_box, padding=0))
        cell_images[str(cell["id"])] = clean
        if not crop_has_meaningful_ink(clean):
            continue
        lines = text_detector.detect_lines(clean)
        accepted_lines = []
        for line in lines:
            line_box = clamp_bbox(line["bbox"], *clean.size)
            abs_line_box = [
                work_box[0] + line_box[0],
                work_box[1] + line_box[1],
                work_box[0] + line_box[2],
                work_box[1] + line_box[3],
            ]
            if (
                cell.get("semantic_col") in {"current", "previous"}
                and is_narrow_table_right_edge_noise(abs_line_box, table_bbox)
            ):
                continue
            accepted_lines.append({**line, "bbox": line_box})
        if not accepted_lines:
            if not routing_occupied.get(str(cell["id"]), False):
                continue
            accepted_lines = [{"bbox": [0, 0, *clean.size]}]
        for line_index, line in enumerate(accepted_lines, start=1):
            line_box = clamp_bbox(line["bbox"], *clean.size)
            if bbox_area(line_box) <= 0:
                continue
            ocr_box = clamp_bbox(
                [line_box[0] - 8, line_box[1] - 3, line_box[2] + 8, line_box[3] + 3],
                *clean.size,
            )
            line_image = clean.crop(tuple(ocr_box))
            if not crop_has_meaningful_ink(line_image):
                continue
            crops.append(TextCrop(
                id=f"{cell['id']}_line_{line_index:03d}",
                bbox=[
                    work_box[0] + ocr_box[0], work_box[1] + ocr_box[1],
                    work_box[0] + ocr_box[2], work_box[1] + ocr_box[3],
                ],
                image=line_image,
                source="table_cell_reocr",
                extra={"cell_id": cell["id"]},
            ))

    recognized = recognize_text_crops(crops, vietocr, batch_size=batch_size)
    recognized = refine_suspicious_table_texts(
        original, recognized, vietocr, batch_size,
    )
    by_cell = {cell["id"]: cell for cell in output}
    for line in recognized:
        by_cell[line["cell_id"]]["lines"].append(line)

    for cell in output:
        cell["lines"].sort(key=lambda line: (line["bbox"][1], line["bbox"][0]))
        cell["line_count"] = len(cell["lines"])
        raw_text = "\n".join(
            str(line.get("text") or "").strip()
            for line in cell["lines"]
            if str(line.get("text") or "").strip()
        )
        semantic_col = cell.get("semantic_col")
        if semantic_col == "code":
            text = extract_best_financial_identifier(cell["lines"], max_digits=3)
            if text is None:
                text = normalize_financial_identifier_text(raw_text, max_digits=3)
        elif semantic_col == "note":
            text = extract_best_financial_identifier(cell["lines"], max_digits=2)
            if text is None:
                text = normalize_financial_identifier_text(raw_text, max_digits=2)
        elif semantic_col == "description":
            text = canonicalize_financial_description(raw_text)
            if text != raw_text:
                apply_refined_cell_text(cell, text, "financial_canonical_phrase")
        else:
            text = raw_text
        cell["text"] = text
        cell["source"] = "table_cell_geometry_reocr"

    data_rows = {
        cell["row"]
        for cell in output
        if cell.get("semantic_col") == "code"
        and re.fullmatch(r"\d{3}", str(cell.get("text") or ""))
    }
    for cell in output:
        semantic_col = cell.get("semantic_col")
        if semantic_col not in {"current", "previous"} or cell.get("row") not in data_rows:
            continue
        best = extract_best_financial_amount(cell["lines"])
        raw_tokens = [str(line.get("text") or "").strip() for line in cell["lines"]]
        if best is None and any(re.fullmatch(r"[-−–—]+", token) for token in raw_tokens):
            best = "-"
        if best is None and str(cell["id"]) in cell_images:
            is_dash, dash_evidence = detect_isolated_financial_dash(
                cell_images[str(cell["id"])],
            )
            if is_dash:
                best = "-"
                cell["text_refinement"] = "pixel_dash_recovery"
                cell["dash_evidence"] = dash_evidence
        cell["text"] = best or ""

        joined = " ".join(raw_tokens)
        letters = [character for character in joined if character.isalpha()]
        unmatched_parenthesis = joined.count("(") != joined.count(")")
        suspicious = (
            len([token for token in raw_tokens if token]) > 1
            or any(character not in "IlOo" for character in letters)
            or unmatched_parenthesis
        )
        if suspicious and str(cell["id"]) in cell_images:
            candidate, paddle_raw, confidence = _paddle_amount_candidate(
                cell_images[str(cell["id"])], text_detector,
            )
            if candidate is not None and best is None:
                cell["raw_text"] = joined
                cell["text"] = candidate
                cell["text_refinement"] = "paddleocr_suspicious_amount_second_opinion"
            if paddle_raw:
                cell["second_opinion"] = {
                    "recognizer": "paddleocr",
                    "raw_text": paddle_raw,
                    "confidence": confidence,
                    "reason": "suspicious_amount",
                }

    for cell in output:
        if (
            cell.get("semantic_col") != "description"
            or cell.get("row") not in data_rows
        ):
            continue
        raw_text = str(cell.get("raw_text") or "").strip() or "\n".join(
            str(line.get("raw_text") or line.get("text") or "").strip()
            for line in cell["lines"]
            if str(line.get("raw_text") or line.get("text") or "").strip()
        )
        if not financial_description_suspicious(raw_text):
            continue
        image = cell_images.get(str(cell["id"]))
        if image is None:
            continue
        try:
            paddle_raw, confidence = text_detector.recognize_text(image)
        except Exception as exc:
            print(f"[warn] Paddle description recognition failed: {exc}", flush=True)
            continue
        if not paddle_raw:
            continue
        primary = canonicalize_financial_description(raw_text)
        candidate = canonicalize_financial_description(paddle_raw)
        selected = bool(
            (confidence is None or confidence >= 0.75)
            and financial_description_quality(candidate) > financial_description_quality(primary)
        )
        if selected:
            apply_refined_cell_text(
                cell, candidate, "paddleocr_financial_text_second_opinion",
            )
        cell["second_opinion"] = {
            "recognizer": "paddleocr",
            "raw_text": paddle_raw,
            "confidence": confidence,
            "reason": "suspicious_description",
            "selected": selected,
        }

    repair_financial_roman_heading_sequence(output)
    verify_accounting_cells_with_paddle(output, cell_images, text_detector)
    return output


def process_table_region(
    original: Image.Image,
    table_region: dict[str, Any],
    page_crop_dir: Path,
    root: Path,
    table_id: str,
    table_engine: PaddleStructureEngine,
    text_detector: PaddleTextDetector,
    vietocr: VietOCRRecognizer,
    vietocr_batch_size: int,
    ocr_cells: bool,
    text_elements: list[dict[str, Any]] | None = None,
    checkbox_elements: list[dict[str, Any]] | None = None,
    black_view: Image.Image | None = None,
) -> dict[str, Any]:
    bbox = table_region["bbox"]
    table_crop = crop_image_memory(original, bbox, padding=0)
    table_crop_path = page_crop_dir / "tables" / f"{table_id}.png"
    table_crop_path.parent.mkdir(parents=True, exist_ok=True)
    table_crop.save(table_crop_path)
    record: dict[str, Any] = {
        "id": table_id, "type": "table", "bbox": bbox,
        "crop": relpath(table_crop_path, root),
        "source": table_region.get("raw", {}).get("source", "conditional_table_structure_on_crop"),
    }

    # Routing OCR supplies geometry only. Final cell values always come from
    # fresh crops of the original image, including multiline cells.
    if (
        ocr_cells
        and record["source"] == "text_layout_table_fallback"
        and table_region.get("column_edges")
    ):
        table_texts = [
            item for item in text_elements or []
            if item.get("type") == "text"
            and item.get("bbox")
            and bbox_coverage(item["bbox"], bbox) >= 0.80
            and not is_narrow_table_right_edge_noise(item["bbox"], bbox)
        ]
        table_texts = refine_suspicious_table_texts(
            black_view or original, table_texts, vietocr, vietocr_batch_size,
        )
        column_schema = ["code", "description", "note", "current", "previous"]
        semantic_edges = infer_unruled_financial_column_edges(table_texts, bbox)
        fallback_region = {**table_region, "column_edges": semantic_edges}
        cells = fallback_table_cells(table_texts, fallback_region, table_id)
        if cells:
            for cell in cells:
                cell["semantic_col"] = column_schema[cell["col"]]
            cells = ocr_cells_from_geometry(
                original=black_view or original,
                cells=cells,
                text_detector=text_detector,
                vietocr=vietocr,
                batch_size=vietocr_batch_size,
                table_bbox=bbox,
            )
            table_checkboxes = [
                item for item in checkbox_elements or []
                if bbox_intersects(item["bbox"], bbox)
            ]
            for cell in cells:
                linked_boxes = [
                    item["id"] for item in table_checkboxes
                    if signature_bbox_in_roi(item["bbox"], cell["bbox"])
                ]
                if linked_boxes:
                    cell["checkbox_ids"] = linked_boxes
            record["cells"] = cells
            record["structure_source"] = "text_layout_geometry_cell_reocr"
            record["table_fallback"] = {
                "source": "routing_text_geometry_then_clean_cell_reocr",
                "column_edges": semantic_edges,
                "column_schema": column_schema,
                "row_count": max(cell["row"] for cell in cells) + 1,
            }
            record["html"] = table_cells_html(cells)
            record["html_recognizer"] = "vietocr"
            return record

    table_items = table_engine.analyze(table_crop)
    table_item = next((item for item in table_items if structure_label(item) == "table"), {})
    res = table_item.get("res") if isinstance(table_item.get("res"), dict) else {}
    if res.get("html"):
        record["html"] = res["html"]
        record["html_recognizer"] = "paddleocr"
    if not ocr_cells:
        return record

    table_checkboxes = [item for item in checkbox_elements or [] if bbox_intersects(item["bbox"], bbox)]
    recognition_crop = remove_table_rules(crop_image_memory_masked(
        black_view or original, bbox, [item["bbox"] for item in table_checkboxes], padding=0,
    ))
    raw_cells = extract_table_cells(table_item)
    parser = TableCellParser()
    parser.feed(str(res.get("html") or ""))
    topology_matches = bool(raw_cells) and len(parser.cells) == len(raw_cells)
    crops: list[TextCrop] = []
    cells = []
    # PP-Structure cell coordinates are local to its table region.
    table_box = structure_bbox(table_item) or [0, 0, *table_crop.size]
    for index, cell in enumerate(raw_cells):
        local = clamp_bbox(
            [cell["bbox"][0] + table_box[0], cell["bbox"][1] + table_box[1],
             cell["bbox"][2] + table_box[0], cell["bbox"][3] + table_box[1]],
            *table_crop.size,
        )
        abs_box = [local[0] + bbox[0], local[1] + bbox[1], local[2] + bbox[0], local[3] + bbox[1]]
        cell_id = f"{table_id}_cell_{index + 1:03d}"
        topology = parser.cells[index] if topology_matches else {"row": cell.get("row"), "col": cell.get("col")}
        cells.append({"id": cell_id, "bbox": abs_box, **topology, "text": "", "lines": []})
        linked_boxes = [item["id"] for item in table_checkboxes if signature_bbox_in_roi(item["bbox"], abs_box)]
        if linked_boxes:
            cells[-1]["checkbox_ids"] = linked_boxes
        if bbox_area(local) == 0:
            continue
        cell_crop = recognition_crop.crop(tuple(local))
        if not crop_has_meaningful_ink(cell_crop):
            continue
        page_lines = [item for item in text_elements or [] if item.get("type") == "text"
                      and bbox_intersects(item["bbox"], abs_box)]
        if page_lines and all(bbox_coverage(item["bbox"], abs_box) >= 0.90 for item in page_lines):
            lines = [{"bbox": [item["bbox"][0] - abs_box[0], item["bbox"][1] - abs_box[1],
                               item["bbox"][2] - abs_box[0], item["bbox"][3] - abs_box[1]]} for item in page_lines]
        else:
            lines = text_detector.detect_lines(cell_crop)
        if not lines:
            # Retain a nonblank cell when line detection misses it.
            lines = [{"bbox": [0, 0, *cell_crop.size]}]
        for line_index, line in enumerate(lines, start=1):
            line_box = clamp_bbox(line["bbox"], *cell_crop.size)
            if bbox_area(line_box) == 0:
                continue
            line_image = cell_crop.crop(tuple(line_box))
            if not crop_has_meaningful_ink(line_image):
                continue
            crops.append(TextCrop(
                id=f"{cell_id}_line_{line_index:03d}",
                bbox=[abs_box[0] + line_box[0], abs_box[1] + line_box[1],
                      abs_box[0] + line_box[2], abs_box[1] + line_box[3]],
                image=line_image, source="table_cell_text_detection",
                extra={"cell_id": cell_id},
            ))
    recognized = recognize_text_crops(crops, vietocr, batch_size=vietocr_batch_size)
    by_cell = {cell["id"]: cell for cell in cells}
    for line in recognized:
        by_cell[line["cell_id"]]["lines"].append(line)
    for cell in cells:
        cell["text"] = "\n".join(line["text"] for line in cell["lines"] if line.get("text"))
    if not cells or not any(cell["text"].strip() for cell in cells):
        # First fallback: re-segment the ORIGINAL table crop at large whitespace
        # gutters, then run VietOCR on each visual segment. This is the critical
        # path for unruled BCTC tables where DB text detection returns one wide
        # box per row and PP-Structure exposes no usable cell boxes.
        unruled_cells, unruled_meta = ocr_unruled_financial_table_cells(
            recognition_crop=recognition_crop,
            table_bbox=bbox,
            table_id=table_id,
            text_detector=text_detector,
            vietocr=vietocr,
            vietocr_batch_size=vietocr_batch_size,
        )
        if unruled_cells:
            cells = unruled_cells
            record["structure_source"] = "unruled_financial_table_whitespace_fallback"
            record["table_fallback"] = unruled_meta
        else:
            # Routing text may recover missing cell geometry, but it is never a
            # text provider. Every recovered cell is OCRed again from pixels.
            fallback_region = table_region
            if not table_region.get("column_edges"):
                inferred = infer_table_regions_from_text(text_elements or [], original.size, [])
                match = next((region for region in inferred if bbox_overlap_ratio(region["bbox"], bbox) > 0.7), None)
                if match is not None and match.get("column_edges"):
                    edges = sorted({bbox[0], bbox[2], *(x for x in match["column_edges"] if bbox[0] < x < bbox[2])})
                    fallback_region = {**table_region, "column_edges": edges}
            fallback = fallback_table_cells(text_elements or [], fallback_region, table_id)
            if fallback:
                semantic_schema = ("code", "description", "note", "current", "previous")
                if len(fallback_region.get("column_edges") or []) == len(semantic_schema) + 1:
                    for cell in fallback:
                        cell["semantic_col"] = semantic_schema[cell["col"]]
                cells = ocr_cells_from_geometry(
                    original=black_view or original,
                    cells=fallback,
                    text_detector=text_detector,
                    vietocr=vietocr,
                    batch_size=vietocr_batch_size,
                    table_bbox=bbox,
                )
                record["structure_source"] = "text_layout_geometry_cell_reocr"
                record["table_fallback"] = {
                    "source": "routing_text_geometry_then_clean_cell_reocr",
                    "column_edges": fallback_region.get("column_edges"),
                }
            elif unruled_meta:
                record["table_fallback"] = unruled_meta
    if cells:
        record["cells"] = cells
        if all(isinstance(cell.get("row"), int) and isinstance(cell.get("col"), int) for cell in cells):
            if record.get("html"):
                record["paddle_html"] = record["html"]
            record["html"] = table_cells_html(cells)
            record["html_recognizer"] = "vietocr"
    else:
        record["structure_status"] = "no_cells"
    return record


def absorb_text_elements_into_tables(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove top-level text already owned by a materialized table cell."""
    tables = [
        item for item in elements
        if item.get("type") == "table"
        and item.get("bbox")
        and isinstance(item.get("cells"), list)
        and item["cells"]
    ]
    if not tables:
        return elements

    nested_ids: dict[int, set[str]] = {}
    absorbed_ids: dict[int, list[str]] = {}
    for table in tables:
        nested_ids[id(table)] = {
            str(line["id"])
            for cell in table["cells"]
            if isinstance(cell, dict)
            for line in cell.get("lines", [])
            if isinstance(line, dict) and line.get("id") is not None
        }
        absorbed_ids[id(table)] = []

    absorbed_objects: set[int] = set()
    for text in elements:
        if text.get("type") != "text" or not text.get("bbox"):
            continue
        text_id = str(text.get("id")) if text.get("id") is not None else None
        owner = None
        owner_score = 0.0
        for table in tables:
            coverage = bbox_coverage(text["bbox"], table["bbox"])
            exact_line = text_id is not None and text_id in nested_ids[id(table)]
            if not exact_line and coverage < 0.80:
                continue
            score = coverage + (2.0 if exact_line else 0.0)
            if score > owner_score:
                owner = table
                owner_score = score
        if owner is None:
            continue
        absorbed_objects.add(id(text))
        if text_id is not None:
            absorbed_ids[id(owner)].append(text_id)

    for table in tables:
        table["absorbed_text_ids"] = absorbed_ids[id(table)]
    return [item for item in elements if id(item) not in absorbed_objects]


def process_visual_region(
    original: Image.Image,
    region: dict[str, Any],
    page_crop_dir: Path,
    root: Path,
    visual_id: str,
    logo_cache: LogoCache,
) -> dict[str, Any]:
    bbox = region["bbox"]
    kind = region["type"]
    folder_by_kind = {
        "seal": "seals",
        "red_mark": "red_marks",
    }
    folder = folder_by_kind.get(kind, "images")
    crop_path = page_crop_dir / folder / f"{visual_id}.png"
    crop = save_crop(original, bbox, crop_path)
    raw_meta = region.get("raw") if isinstance(region.get("raw"), dict) else {}
    record = {
        "id": visual_id,
        "type": kind,
        "bbox": bbox,
        "crop": relpath(crop_path, root),
        "source": raw_meta.get("source", "fast_layout_conditional_crop"),
    }
    if kind == "image":
        role, phash = logo_cache.classify(crop, bbox, original.size[0], original.size[1])
        record["image_role"] = role
        record["phash"] = phash
    return record


def page_has_signature_cue(elements: list[dict[str, Any]]) -> bool:
    return any(signature_cue_key(str(item.get("text") or "")) for item in elements if item.get("type") == "text")


def signature_cue_key(text: str) -> str | None:
    compact = re.sub(r"[^A-Z]+", "", ascii_upper(text))
    if "KETOANTRUONG" in compact:
        return "ke_toan_truong"
    if "NGUOILAPBIEU" in compact or "NGUOILAP" in compact:
        return "nguoi_lap_bieu"
    if "TONGGIAMDOC" in compact:
        return "tong_giam_doc"
    if "GIAMDOC" in compact:
        return "giam_doc"
    if "KIEMTOANVIEN" in compact:
        return "kiem_toan_vien"
    if "NGUOIDAIDIEN" in compact:
        return "nguoi_dai_dien"
    return None


def signature_bbox_in_roi(bbox: list[int], roi: list[int]) -> bool:
    cx, cy = bbox_center(bbox)
    return roi[0] <= cx <= roi[2] and roi[1] <= cy <= roi[3]


def build_signature_cue_rois(
    text_elements: list[dict[str, Any]],
    page_size: tuple[int, int],
    seal_bboxes: list[list[int]] | None = None,
) -> list[dict[str, Any]]:
    """Build signer-role search ROIs from OCR text.

    A role ROI is a SEARCH AREA, not a signature bbox.  It is deliberately
    wider than a seal because handwritten signatures often extend several seal
    widths horizontally.  OCR fragments on the same row are also joined before
    role matching so ``KẾ TOÁN`` + ``TRƯỞNG`` can still form one anchor.
    """
    page_width, page_height = page_size

    valid_seals: list[list[int]] = []
    for seal_bbox in seal_bboxes or []:
        seal_bbox = clamp_bbox(seal_bbox, page_width, page_height)
        if bbox_area(seal_bbox) > 0:
            valid_seals.append(seal_bbox)

    if valid_seals:
        seal_sizes = [
            max(1, box[2] - box[0], box[3] - box[1])
            for box in valid_seals
        ]
        context_size = int(max(seal_sizes))
        context_size_source = "max_global_seal_size"
    else:
        context_size = max(
            260,
            int(round(min(page_width, page_height) * 0.16)),
        )
        context_size_source = "safe_page_relative_default"

    anchors: list[dict[str, Any]] = []
    seen_anchor_keys: set[tuple[str, int, int, int, int]] = set()

    def add_anchor(cue: str | None, cue_text: str, cue_bbox: list[int], cue_text_id: str | None) -> None:
        if cue is None:
            return
        cue_bbox = clamp_bbox(cue_bbox, page_width, page_height)
        if bbox_area(cue_bbox) <= 0:
            return
        key = (cue, *cue_bbox)
        if key in seen_anchor_keys:
            return
        seen_anchor_keys.add(key)
        cue_cx, cue_cy = bbox_center(cue_bbox)
        anchors.append({
            "cue": cue,
            "cue_text": cue_text,
            "cue_text_id": cue_text_id,
            "cue_bbox": cue_bbox,
            "cx": float(cue_cx),
            "cy": float(cue_cy),
        })

    text_only = [
        item for item in text_elements
        if item.get("type") == "text" and item.get("bbox")
        and str(item.get("text") or "").strip()
    ]

    # Direct OCR lines.
    for item in text_only:
        cue_text = str(item.get("text") or "")
        add_anchor(
            signature_cue_key(cue_text),
            cue_text,
            item["bbox"],
            item.get("id"),
        )

    # Same-row OCR fragments, e.g. "KẾ TOÁN" + "TRƯỞNG".  Join only
    # LOCAL consecutive fragments; never concatenate an entire signer row
    # because several independent roles often share that row.
    for row in group_text_rows(text_only):
        if len(row) < 2:
            continue
        row = sorted(row, key=lambda item: item["bbox"][0])
        for start_idx in range(len(row)):
            window: list[dict[str, Any]] = [row[start_idx]]
            for end_idx in range(start_idx + 1, min(len(row), start_idx + 3)):
                previous = row[end_idx - 1]
                current = row[end_idx]
                local_height = max(1, min(bbox_height(previous["bbox"]), bbox_height(current["bbox"])))
                gap = current["bbox"][0] - previous["bbox"][2]
                if gap > max(80, int(round(local_height * 3.0))):
                    break
                window.append(current)
                joined_text = " ".join(
                    str(item.get("text") or "").strip() for item in window
                ).strip()
                cue = signature_cue_key(joined_text)
                if cue is None:
                    continue
                joined_bbox = union_bbox(
                    [item["bbox"] for item in window], page_width, page_height
                )
                add_anchor(cue, joined_text, joined_bbox, None)

    if not anchors:
        return []

    anchors.sort(key=lambda anchor: (anchor["cy"], anchor["cx"]))
    role_rois: list[dict[str, Any]] = []

    for anchor in anchors:
        cue_bbox = anchor["cue_bbox"]
        cue_center_x = float(anchor["cx"])
        roi_width = float(context_size)
        roi_height = float(context_size)

        roi_x1 = cue_center_x - roi_width / 2.0
        roi_y1 = float(cue_bbox[3])
        roi_x2 = cue_center_x + roi_width / 2.0
        roi_y2 = roi_y1 + roi_height

        initial_roi = clamp_bbox(
            [roi_x1, roi_y1, roi_x2, roi_y2],
            page_width,
            page_height,
        )
        if bbox_area(initial_roi) <= 0:
            continue

        roi_index = len(role_rois) + 1
        role_rois.append({
            "roi_index": roi_index,
            "cue": anchor["cue"],
            "cue_text": anchor["cue_text"],
            "cue_text_id": anchor["cue_text_id"],
            "cue_bbox": cue_bbox,
            "roi": initial_roi,
            "router_source": "signing_role_search_roi",
            "context_size_px": int(context_size),
            "context_size_source": context_size_source,
            "global_seal_max_size_px": int(context_size) if valid_seals else None,
            "roi_width_px": int(round(roi_width)),
            "roi_height_px": int(round(roi_height)),
        })

    return role_rois

def bboxes_touching_roi(
    bboxes: list[list[int]],
    roi: list[int],
    min_coverage: float = 0.08,
) -> list[list[int]]:
    matches = []
    for bbox in bboxes:
        if (
            bbox_intersects(bbox, roi)
            or bbox_coverage(bbox, roi) >= min_coverage
            or signature_bbox_in_roi(bbox, roi)
        ):
            matches.append(bbox)
    return matches


def expand_roi_by_seals(
    roi: list[int],
    seal_bboxes: list[list[int]],
    page_size: tuple[int, int],
    scale: float = 0.35,
) -> list[int]:
    page_width, page_height = page_size
    if not seal_bboxes:
        w = max(1, roi[2] - roi[0])
        h = max(1, roi[3] - roi[1])
        margin = max(24, int(round(min(w, h) * 0.12)))
        return clamp_bbox([roi[0] - margin, roi[1] - margin, roi[2] + margin, roi[3] + margin], page_width, page_height)

    seal_size = max(max(1, seal[2] - seal[0], seal[3] - seal[1]) for seal in seal_bboxes)
    margin = max(24, int(round(seal_size * scale)))
    return clamp_bbox([roi[0] - margin, roi[1] - margin, roi[2] + margin, roi[3] + margin], page_width, page_height)


def build_seal_context_rois(
    seal_bboxes: list[list[int]],
    page_size: tuple[int, int],
) -> list[dict[str, Any]]:
    """
    Build a dynamic seal-context fallback ROI list.

    0 global seals -> 0 seal-context ROIs
    1 global seal  -> 1 seal-context ROI
    N global seals -> N seal-context ROIs

    These are fallback ROIs and are separate from signing-role ROIs.
    """
    page_width, page_height = page_size
    seal_context_rois: list[dict[str, Any]] = []

    for seal_bbox_raw in seal_bboxes:
        seal_bbox = clamp_bbox(
            seal_bbox_raw,
            page_width,
            page_height,
        )

        if bbox_area(seal_bbox) <= 0:
            continue

        x1, y1, x2, y2 = seal_bbox
        seal_width = max(1, x2 - x1)
        seal_height = max(1, y2 - y1)

        roi = clamp_bbox(
            [
                x1 - seal_width * 1.50,
                y1 - seal_height * 0.80,
                x2 + seal_width * 1.50,
                y2 + seal_height * 0.80,
            ],
            page_width,
            page_height,
        )

        if bbox_area(roi) <= 0:
            continue

        seal_index = len(seal_context_rois) + 1

        seal_context_rois.append({
            "cue": "seal_context",
            "cue_text": None,
            "cue_text_id": None,
            "cue_bbox": seal_bbox,
            "roi": roi,
            "router_source": "one_dynamic_context_roi_per_global_seal",
            "seal_bboxes": [seal_bbox],
            "seal_index": seal_index,
        })

    return seal_context_rois

def detect_blue_signature_in_roi(
    roi_image: Image.Image,
    roi_bbox: list[int],
    page_size: tuple[int, int],
) -> dict[str, Any] | None:
    """
    Recover blue signature ink inside an ROI using stricter blue chroma rules,
    so neutral black printed text does not get merged into the signature box.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return None

    rgb = np.asarray(roi_image.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return None

    rgb_i = rgb.astype(np.int16)
    r = rgb_i[:, :, 0]
    g = rgb_i[:, :, 1]
    b = rgb_i[:, :, 2]

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h = hsv[:, :, 0]
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_b = lab[:, :, 2] - 128.0
    chroma = np.sqrt((lab[:, :, 1] - 128.0) ** 2 + (lab[:, :, 2] - 128.0) ** 2)
    spread = np.max(rgb_i, axis=2) - np.min(rgb_i, axis=2)
    luma = (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)
    blue_dom = b - ((r + g) // 2)

    blue_hue = (h >= 90) & (h <= 140)
    blackish_blue_reject = (
        (spread <= 20)
        | (chroma <= 8.0)
        | (s <= 20)
        | ((abs(r - g) <= 10) & (blue_dom <= 14))
        | ((luma < 150) & (s <= 28) & (blue_dom <= 20))
    )

    height, width = rgb.shape[:2]
    if width <= 0 or height <= 0:
        return None

    strong = (
        (v < 252)
        & blue_hue
        & (lab_b <= -14.0)
        & (blue_dom >= 22)
        & (s >= 45)
        & ~blackish_blue_reject
    )
    weak = (
        (v < 253)
        & blue_hue
        & (lab_b <= -6.0)
        & (blue_dom >= 10)
        & (s >= 18)
        & ~blackish_blue_reject
    )

    blue_mask = connected_ink_mask(strong, weak, min_area=2)
    if not np.any(blue_mask):
        return None

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        blue_mask.astype(np.uint8), connectivity=8
    )

    components: list[dict[str, Any]] = []
    roi_area = max(1, width * height)

    for label_id in range(1, count):
        x = int(stats[label_id, cv2.CC_STAT_LEFT])
        y = int(stats[label_id, cv2.CC_STAT_TOP])
        w = int(stats[label_id, cv2.CC_STAT_WIDTH])
        hh = int(stats[label_id, cv2.CC_STAT_HEIGHT])
        area = int(stats[label_id, cv2.CC_STAT_AREA])

        if area < max(8, int(round(roi_area * 0.00005))):
            continue
        if w < 6 or hh < 3:
            continue
        if area > roi_area * 0.35:
            continue

        components.append({
            "bbox": [x, y, x + w, y + hh],
            "area": area,
        })

    if not components:
        return None

    def comp_score(comp: dict[str, Any]) -> float:
        x1, y1, x2, y2 = comp["bbox"]
        w = x2 - x1
        hh = y2 - y1
        return float(comp["area"]) * (1.0 + min(5.0, w / max(1.0, hh)) * 0.18)

    seed = max(components, key=comp_score)
    seed_box = seed["bbox"]
    _, seed_cy = bbox_center(seed_box)
    seed_h = max(1, bbox_height(seed_box))

    selected = [seed]
    for comp in components:
        if comp is seed:
            continue
        box = comp["bbox"]
        _, cy = bbox_center(box)
        vertical_ok = (
            vertical_overlap_ratio(seed_box, box) >= 0.16
            or abs(cy - seed_cy) <= max(seed_h * 0.55, height * 0.04)
        )
        horizontal_ok = bbox_gap(seed_box, box) <= max(18, int(round(width * 0.12)))
        meaningful = comp["area"] >= 12 or (box[2] - box[0]) >= 10
        if vertical_ok and horizontal_ok and meaningful:
            selected.append(comp)

    local = union_bbox([comp["bbox"] for comp in selected], width, height)
    local_w = local[2] - local[0]
    local_h = local[3] - local[1]
    selected_area = sum(int(comp["area"]) for comp in selected)

    if local_w < max(14, int(round(width * 0.025))):
        return None
    if local_h < 5:
        return None
    if selected_area < max(14, int(round(roi_area * 0.00020))):
        return None

    page_width, page_height = page_size
    pad = max(3, int(round(min(local_w, local_h) * 0.08)))
    bbox = clamp_bbox(
        [
            roi_bbox[0] + local[0] - pad,
            roi_bbox[1] + local[1] - pad,
            roi_bbox[0] + local[2] + pad,
            roi_bbox[1] + local[3] + pad,
        ],
        page_width,
        page_height,
    )

    return {
        "bbox": bbox,
        "score": None,
        "label": "signature",
        "source": "signature_context_roi_blue_strict_chroma",
        "coordinate_space": "original",
        "blue_component_count": len(selected),
        "blue_pixel_area": selected_area,
    }

def detect_signature_candidates_in_roi(
    roi_image: Image.Image,
    roi: list[int],
    page_size: tuple[int, int],
    detector: SignatureDetector,
    threshold: float,
) -> list[dict[str, Any]]:
    """Return RGB and red-removed YOLOS candidates in ORIGINAL coordinates.

    This function intentionally does not decide whether a candidate is a real
    signature.  Context/checkbox/text validation happens later.
    """
    candidates: list[dict[str, Any]] = []

    def convert(items: list[dict[str, Any]], source: str) -> None:
        for item in items:
            local = clamp_bbox(item.get("bbox", []), *roi_image.size)
            if bbox_area(local) <= 0:
                continue
            box = clamp_bbox(
                [
                    local[0] + roi[0],
                    local[1] + roi[1],
                    local[2] + roi[0],
                    local[3] + roi[1],
                ],
                *page_size,
            )
            if bbox_area(box) <= 0:
                continue
            candidates.append({
                **item,
                "bbox": box,
                "source": source,
                "coordinate_space": "original",
            })

    convert(
        detector.detect(roi_image, threshold=threshold),
        "signature_context_roi_rgb",
    )
    neutral_view = remove_red_ink(roi_image)
    convert(
        detector.detect(neutral_view, threshold=threshold),
        "signature_context_roi_red_removed",
    )
    return candidates


def signature_candidate_rejection_reason(
    candidate: dict[str, Any],
    page_size: tuple[int, int],
    text_elements: list[dict[str, Any]],
    checkbox_bboxes: list[list[int]] | None = None,
    role_roi: list[int] | None = None,
) -> str | None:
    """Return why a contextual signature candidate is unsafe, else ``None``.

    The checks are deliberately conservative: final signature crops should be
    high precision.  Raw full-page proposals never call this function as final
    outputs; they are only search hints.
    """
    page_width, page_height = page_size
    bbox = clamp_bbox(candidate.get("bbox", []), page_width, page_height)
    if bbox_area(bbox) <= 0:
        return "empty_bbox"

    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    area = bbox_area(bbox)

    # Tiny detector fragments are useful as hints but not as final signatures.
    min_width = max(18, int(round(page_width * 0.008)))
    min_height = max(6, int(round(page_height * 0.003)))
    min_area = max(150, int(round(page_width * page_height * 0.00008)))
    if width < min_width or height < min_height or area < min_area:
        return "tiny_fragment"

    # A specialized checkbox detector wins when the signature candidate is
    # mostly the checkbox itself.  Do not reject a large genuine signature just
    # because it happens to pass across a small checkbox.
    for checkbox in checkbox_bboxes or []:
        checkbox = clamp_bbox(checkbox, page_width, page_height)
        if bbox_area(checkbox) <= 0:
            continue
        candidate_in_checkbox = bbox_coverage(bbox, checkbox)
        checkbox_in_candidate = bbox_coverage(checkbox, bbox)
        if candidate_in_checkbox >= 0.45 or (
            checkbox_in_candidate >= 0.92
            and area <= bbox_area(checkbox) * 2.2
        ):
            return "checkbox_overlap"

    # Reject candidates that are essentially one printed OCR line.  Large boxes
    # containing both a title and handwriting are not accepted here merely due
    # to title overlap; role ROIs begin below titles to avoid that situation.
    for text in text_elements:
        if text.get("type") != "text" or not text.get("bbox"):
            continue
        token = str(text.get("text") or "").strip()
        if len(token) < 2:
            continue
        tb = clamp_bbox(text["bbox"], page_width, page_height)
        if bbox_area(tb) <= 0:
            continue
        candidate_covered = bbox_coverage(bbox, tb)
        text_covered = bbox_coverage(tb, bbox)
        if (
            candidate_covered >= 0.72
            and text_covered >= 0.55
            and height <= max(12, bbox_height(tb) * 1.7)
        ):
            return "printed_text"

    # Role candidates must actually remain in the signer search area.  Seal-only
    # fallback passes role_roi=None and is validated by seal context instead.
    if role_roi is not None:
        role_roi = clamp_bbox(role_roi, page_width, page_height)
        if not (
            bbox_intersects(bbox, role_roi)
            or signature_bbox_in_roi(bbox, role_roi)
            or bbox_coverage(bbox, role_roi) >= 0.45
        ):
            return "outside_role_roi"

    return None


def validate_signature_candidate(
    candidate: dict[str, Any],
    page_size: tuple[int, int],
    text_elements: list[dict[str, Any]],
    checkbox_bboxes: list[list[int]] | None = None,
    role_roi: list[int] | None = None,
) -> bool:
    return signature_candidate_rejection_reason(
        candidate,
        page_size,
        text_elements,
        checkbox_bboxes=checkbox_bboxes,
        role_roi=role_roi,
    ) is None


def signature_candidate_quality(candidate: dict[str, Any], page_size: tuple[int, int]) -> float:
    """Ranking score used only after a candidate has passed validation."""
    page_area = max(1, page_size[0] * page_size[1])
    score = float(candidate.get("score") or 0.0)
    area_ratio = min(1.0, bbox_area(candidate["bbox"]) / max(1.0, page_area * 0.02))
    source = str(candidate.get("source") or "")
    source_bonus = 0.0
    if source == "signature_context_roi_rgb":
        source_bonus = 0.10
    elif source == "signature_context_roi_red_removed":
        source_bonus = 0.09
    elif source.startswith("signature_context_roi_blue"):
        source_bonus = 0.05
    if candidate.get("cue") and candidate.get("cue") != "seal_context":
        source_bonus += 0.06
    if candidate.get("seal_bboxes"):
        source_bonus += 0.03
    return score + area_ratio * 0.16 + source_bonus


def deduplicate_signature_candidates(
    candidates: list[dict[str, Any]],
    page_size: tuple[int, int],
) -> list[dict[str, Any]]:
    """Keep one best contextual detection for each physical signature."""
    ordered = sorted(
        candidates,
        key=lambda item: signature_candidate_quality(item, page_size),
        reverse=True,
    )
    kept: list[dict[str, Any]] = []
    for item in ordered:
        bbox = item["bbox"]
        duplicate = False
        for old in kept:
            old_box = old["bbox"]
            same_role = (
                item.get("cue") == old.get("cue")
                and item.get("cue") not in {None, "seal_context"}
            )
            if (
                bbox_iou(bbox, old_box) >= 0.28
                or bbox_overlap_ratio(bbox, old_box) >= 0.72
                or (same_role and bbox_gap(bbox, old_box) <= 18)
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return sorted(kept, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def recover_contextual_signatures(
    original: Image.Image,
    signature_detector: SignatureDetector | None,
    text_elements: list[dict[str, Any]],
    detections: list[dict[str, Any]],
    seal_bboxes: list[list[int]],
    scale: ScaleInfo,
    roi_threshold: float,
    checkbox_bboxes: list[list[int]] | None = None,
) -> list[dict[str, Any]]:
    """Recover VERIFIED signatures from signer/seal context.

    ``detections`` are raw full-page YOLOS PROPOSALS.  They are never returned
    directly.  They may only enlarge/localize a contextual ROI.  Every output
    is detected again inside a role or seal context and then validated against
    final checkbox/text information.
    """
    if signature_detector is None:
        return []

    page_size = original.size
    page_width, page_height = page_size
    checkbox_bboxes = checkbox_bboxes or []

    def proposal_bbox(item: dict[str, Any]) -> list[int]:
        if item.get("coordinate_space") == "original":
            return clamp_bbox(item["bbox"], page_width, page_height)
        return map_bbox_to_original(item["bbox"], scale)

    raw_proposals: list[dict[str, Any]] = []
    for item in detections:
        bbox = proposal_bbox(item)
        if bbox_area(bbox) <= 0:
            continue
        # A full-page proposal that is basically a final checkbox is a bad hint.
        if any(
            bbox_coverage(bbox, cb) >= 0.45
            or (bbox_coverage(cb, bbox) >= 0.92 and bbox_area(bbox) <= bbox_area(cb) * 2.2)
            for cb in checkbox_bboxes
        ):
            continue
        raw_proposals.append({**item, "bbox": bbox, "coordinate_space": "original"})

    final_candidates: list[dict[str, Any]] = []

    def attach_context(
        candidate: dict[str, Any],
        roi_info: dict[str, Any],
        search_roi: list[int],
        seals_in_roi: list[list[int]],
        proposal_hints: list[list[int]],
    ) -> dict[str, Any]:
        candidate = dict(candidate)
        candidate["roi"] = search_roi
        candidate["cue"] = roi_info.get("cue")
        candidate["cue_text"] = roi_info.get("cue_text")
        candidate["cue_text_id"] = roi_info.get("cue_text_id")
        candidate["router_source"] = roi_info.get("router_source")
        candidate["raw_proposal_hints"] = proposal_hints
        for meta_key in (
            "roi_index",
            "context_size_px",
            "context_size_source",
            "global_seal_max_size_px",
            "roi_width_px",
            "roi_height_px",
            "seal_index",
        ):
            if meta_key in roi_info:
                candidate[meta_key] = roi_info[meta_key]
        if seals_in_roi:
            candidate["seal_bboxes"] = seals_in_roi
            candidate["overlap"] = {
                "seal_bboxes": seals_in_roi,
                "has_overlap": any(
                    bbox_intersects(candidate["bbox"], seal)
                    for seal in seals_in_roi
                ),
                "blue_pixel_fallback": str(candidate.get("source", "")).startswith(
                    "signature_context_roi_blue"
                ),
            }
        return candidate

    def recover_from_roi(
        roi_info: dict[str, Any],
        search_roi: list[int],
        seals_in_roi: list[list[int]],
        proposal_hints: list[list[int]],
        role_validation_roi: list[int] | None,
    ) -> list[dict[str, Any]]:
        roi_image = crop_image_memory(original, search_roi, padding=0)
        candidates = detect_signature_candidates_in_roi(
            roi_image,
            search_roi,
            page_size,
            signature_detector,
            roi_threshold,
        )

        accepted: list[dict[str, Any]] = []
        for raw_candidate in candidates:
            candidate = attach_context(
                raw_candidate, roi_info, search_roi, seals_in_roi, proposal_hints,
            )
            reason = signature_candidate_rejection_reason(
                candidate,
                page_size,
                text_elements,
                checkbox_bboxes=checkbox_bboxes,
                role_roi=role_validation_roi,
            )
            candidate["validation"] = {
                "accepted": reason is None,
                "rejection_reason": reason,
            }
            if reason is None:
                accepted.append(candidate)

        # Strict blue chroma is both a rescue path and extent evidence. YOLOS
        # often recognizes only the dense body of a signature and clips long,
        # thin pen strokes. Expand an overlapping YOLOS box to the full blue
        # component union while retaining the detector result and confidence.
        blue = detect_blue_signature_in_roi(roi_image, search_roi, page_size)
        if blue is not None:
            blue = attach_context(
                blue, roi_info, search_roi, seals_in_roi, proposal_hints,
            )
            reason = signature_candidate_rejection_reason(
                blue,
                page_size,
                text_elements,
                checkbox_bboxes=checkbox_bboxes,
                role_roi=role_validation_roi,
            )
            blue["validation"] = {
                "accepted": reason is None,
                "rejection_reason": reason,
            }
            if reason is None and accepted:
                blue_bbox = blue["bbox"]
                matching = [
                    candidate for candidate in accepted
                    if bbox_intersects(candidate["bbox"], blue_bbox)
                    and bbox_coverage(candidate["bbox"], blue_bbox) >= 0.35
                ]
                if matching:
                    candidate = max(
                        matching,
                        key=lambda item: bbox_coverage(item["bbox"], blue_bbox),
                    )
                    detector_bbox = list(candidate["bbox"])
                    detector_height = max(1, bbox_height(detector_bbox))
                    vertical_margin = max(6, int(round(detector_height * 0.20)))
                    applied_ink_extent = [
                        blue_bbox[0],
                        max(blue_bbox[1], detector_bbox[1] - vertical_margin),
                        blue_bbox[2],
                        min(blue_bbox[3], detector_bbox[3] + vertical_margin),
                    ]
                    expanded_bbox = union_bbox(
                        [detector_bbox, applied_ink_extent], page_width, page_height,
                    )
                    candidate["detector_bbox"] = detector_bbox
                    candidate["ink_extent_bbox"] = blue_bbox
                    candidate["applied_ink_extent_bbox"] = applied_ink_extent
                    candidate["ink_extent_source"] = blue["source"]
                    candidate["bbox_expanded_by_ink"] = expanded_bbox != detector_bbox
                    candidate["bbox"] = expanded_bbox
                    for key in ("blue_component_count", "blue_pixel_area"):
                        if key in blue:
                            candidate[key] = blue[key]
            elif reason is None:
                accepted.append(blue)
        return accepted

    # ------------------------------------------------------------------
    # A. ROLE-BASED SEARCH.  Raw full-page proposals are hints only and NEVER
    #    suppress this recovery pass.
    # ------------------------------------------------------------------
    role_rois = build_signature_cue_rois(
        text_elements, page_size, seal_bboxes=seal_bboxes,
    )
    for roi_info in role_rois:
        role_roi = roi_info["roi"]
        role_seals = [
            seal for seal in seal_bboxes
            if bbox_intersects(seal, role_roi)
            or bbox_gap(seal, role_roi) <= 80
        ]
        role_hints = [
            item["bbox"] for item in raw_proposals
            if bbox_intersects(item["bbox"], role_roi)
            or bbox_coverage(item["bbox"], role_roi) >= 0.20
            or bbox_gap(item["bbox"], role_roi) <= 60
        ]

        merge_boxes = [role_roi] + role_seals + role_hints
        search_roi = union_bbox(merge_boxes, page_width, page_height)
        search_roi = expand_roi_by_seals(
            search_roi,
            role_seals,
            page_size,
            scale=0.30,
        )
        routed = {
            **roi_info,
            "router_source": "signing_role_context_recovery",
        }
        final_candidates.extend(recover_from_roi(
            routed,
            search_roi,
            role_seals,
            role_hints,
            role_roi,
        ))

    # ------------------------------------------------------------------
    # B. SEAL-CONTEXT SEARCH.  Always available as a fallback/second route so
    #    OCR failure on the role title does not lose seal-overlapped signatures.
    #    Dedup later removes duplicates with role-based results.
    # ------------------------------------------------------------------
    for roi_info in build_seal_context_rois(seal_bboxes, page_size):
        seal_bbox = roi_info["seal_bboxes"][0]
        seal_roi = roi_info["roi"]
        seal_hints = [
            item["bbox"] for item in raw_proposals
            if bbox_intersects(item["bbox"], seal_roi)
            or bbox_gap(item["bbox"], seal_bbox) <= 120
        ]
        search_roi = union_bbox(
            [seal_roi] + seal_hints,
            page_width,
            page_height,
        )
        final_candidates.extend(recover_from_roi(
            roi_info,
            search_roi,
            roi_info["seal_bboxes"],
            seal_hints,
            None,
        ))

    # Raw proposals are intentionally absent here.  Only candidates detected
    # again in contextual ROIs can become final signatures.
    return deduplicate_signature_candidates(final_candidates, page_size)


def detect_signature_boxes(
    detection_image: Image.Image,
    tiny_detector: SignatureDetector | None,
    fallback_detector: SignatureDetector | None,
    elements: list[dict[str, Any]],
    has_seal: bool,
) -> tuple[list[dict[str, Any]], SignatureDetector | None]:
    if tiny_detector is None:
        return [], None

    detections = tiny_detector.detect(detection_image)
    used_detector = tiny_detector
    if not detections and fallback_detector is not None and (has_seal or page_has_signature_cue(elements)):
        detections = fallback_detector.detect(detection_image)
        used_detector = fallback_detector
    return detections, used_detector


def materialize_signature_elements(
    original: Image.Image,
    detections: list[dict[str, Any]],
    used_detector: SignatureDetector | None,
    scale: ScaleInfo,
    page_crop_dir: Path,
    root: Path,
) -> list[dict[str, Any]]:
    signatures = []
    if used_detector is None and not detections:
        return signatures

    for idx, sig in enumerate(detections, start=1):
        if sig.get("coordinate_space") == "original":
            bbox = clamp_bbox(sig["bbox"], original.size[0], original.size[1])
        else:
            bbox = map_bbox_to_original(sig["bbox"], scale)
        sig_id = f"signature_{idx:03d}"
        crop_path = page_crop_dir / "signatures" / f"{sig_id}.png"
        rgb_crop = save_crop(original, bbox, crop_path, padding=6)
        ink_path = page_crop_dir / "signatures" / f"{sig_id}_ink.png"
        make_signature_ink_view(rgb_crop).save(ink_path)
        record = {
            "id": sig_id,
            "type": "signature",
            "bbox": bbox,
            "crop": relpath(crop_path, root),
            "confidence": sig.get("score"),
            "ink_view_crop": relpath(ink_path, root),
            "detector": used_detector.model_name if used_detector and sig.get("score") is not None else sig.get("source", "signature_fallback"),
            "source": sig.get("source", "signature_detection_lowres"),
        }
        for key in (
            "roi",
            "cue",
            "cue_text",
            "cue_text_id",
            "router_source",
            "seal_bboxes",
            "overlap",
            "blue_component_count",
            "blue_pixel_area",
            "detector_bbox",
            "ink_extent_bbox",
            "applied_ink_extent_bbox",
            "ink_extent_source",
            "bbox_expanded_by_ink",
            "initial_roi_size_px",
            "initial_roi_size_source",
            "global_seal_max_size_px",
            "roi_index",
            "context_size_px",
            "context_size_source",
            "roi_width_px",
            "roi_height_px",
            "seal_index",
            "raw_proposal_hints",
            "validation",
        ):
            if key in sig:
                record[key] = sig[key]
        signatures.append(record)
    return signatures


def process_seal_signature_crops(
    original: Image.Image,
    visual_regions: list[dict[str, Any]],
    signatures: list[dict[str, Any]],
    page_crop_dir: Path,
    root: Path,
    near_gap_px: int = 120,
) -> list[dict[str, Any]]:
    seal_regions = [region for region in visual_regions if region["type"] == "seal"]
    if not seal_regions or not signatures:
        return []

    records = []
    page_width, page_height = original.size
    counter = 0
    for seal in seal_regions:
        seal_bbox = seal["bbox"]
        for signature in signatures:
            sig_bbox = signature["bbox"]
            if bbox_gap(seal_bbox, sig_bbox) > near_gap_px:
                continue

            counter += 1
            crop_id = f"seal_signature_{counter:03d}"
            merged_bbox = union_bbox([seal_bbox, sig_bbox], page_width, page_height)
            crop_path = page_crop_dir / "seal_signature" / f"{crop_id}.png"
            save_crop(original, merged_bbox, crop_path, padding=12)
            records.append(
                {
                    "id": crop_id,
                    "type": "seal_signature_region",
                    "bbox": merged_bbox,
                    "crop": relpath(crop_path, root),
                    "seal_bbox": seal_bbox,
                    "signature_bbox": sig_bbox,
                    "signature_id": signature["id"],
                    "source": "seal_signature_near_or_overlap_crop",
                }
            )
    return records


def process_page(
    page_path: Path,
    pages_root: Path,
    root: Path,
    output_root: Path,
    crops_root: Path,
    structure_engine: PaddleStructureEngine,
    table_engine: PaddleStructureEngine,
    text_detector: PaddleTextDetector,
    vietocr: VietOCRRecognizer,
    checkbox_detector: CheckboxDetectorONNX,
    signature_detector: SignatureDetector | None,
    signature_fallback_detector: SignatureDetector | None,
    logo_cache: LogoCache,
    detection_long_side: int,
    signature_roi_threshold: float,
    vietocr_batch_size: int,
    ocr_table_cells: bool,
    save_text_crops: bool,
    save_color_debug_crops: bool,
    symbol_arg: str | None,
    year_arg: str | None,
    progress: Any = None,
) -> dict[str, Any]:
    arguments = locals().copy()
    symbol, year = infer_symbol_year(page_path, pages_root, symbol_arg, year_arg)
    crops_base = crops_root.resolve()
    final_crop_dir = (crops_base / symbol / year / page_path.stem).resolve()
    final_crop_dir.relative_to(crops_base)
    crops_base.mkdir(parents=True, exist_ok=True)
    final_json = output_json_path(output_root, page_path, pages_root, symbol_arg, year_arg)
    start = time.perf_counter()
    # Build and validate a complete replacement before touching previous output.
    with TemporaryDirectory(prefix=".ocr-stage-", dir=crops_base) as directory:
        stage = Path(directory)
        arguments["crops_root"] = stage / "crops"
        arguments["output_root"] = stage / "json"
        result = _process_page(**arguments)
        staged_crops = stage / "crops" / symbol / year / page_path.stem
        source_prefix = relpath(staged_crops, root) + "/"
        target_prefix = relpath(final_crop_dir, root) + "/"

        def final_paths(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: final_paths(item) for key, item in value.items()}
            if isinstance(value, list):
                return [final_paths(item) for item in value]
            if isinstance(value, str) and value.startswith(source_prefix):
                return target_prefix + value[len(source_prefix):]
            return value

        result = final_paths(result)
        result["provenance"]["page_seconds"] = round(time.perf_counter() - start, 3)
        staged_json = stage / "complete.json"
        write_json_atomic(staged_json, result)
        final_crop_dir.parent.mkdir(parents=True, exist_ok=True)
        final_json.parent.mkdir(parents=True, exist_ok=True)
        backup = stage / "previous_crops"
        had_previous = final_crop_dir.exists()
        if had_previous:
            os.replace(final_crop_dir, backup)
        installed = False
        try:
            os.replace(staged_crops, final_crop_dir)
            installed = True
            write_json_atomic(final_json, result)
        except BaseException:
            if installed:
                os.replace(final_crop_dir, staged_crops)
            if had_previous:
                os.replace(backup, final_crop_dir)
            raise
    return result


def _process_page(
    page_path: Path,
    pages_root: Path,
    root: Path,
    output_root: Path,
    crops_root: Path,
    structure_engine: PaddleStructureEngine,
    table_engine: PaddleStructureEngine,
    text_detector: PaddleTextDetector,
    vietocr: VietOCRRecognizer,
    checkbox_detector: CheckboxDetectorONNX,
    signature_detector: SignatureDetector | None,
    signature_fallback_detector: SignatureDetector | None,
    logo_cache: LogoCache,
    detection_long_side: int,
    signature_roi_threshold: float,
    vietocr_batch_size: int,
    ocr_table_cells: bool,
    save_text_crops: bool,
    save_color_debug_crops: bool,
    symbol_arg: str | None,
    year_arg: str | None,
    progress: Any = None,
) -> dict[str, Any]:
    symbol, year = infer_symbol_year(page_path, pages_root, symbol_arg, year_arg)
    page_id = page_path.stem
    page_num = page_number(page_path)
    page_crop_dir = crops_root / symbol / year / page_id

    page_crop_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(page_path) as opened:
        original = opened.convert("RGB")
    detection_image, scale = make_detection_view(original, detection_long_side)

    if progress is not None:
        progress.set_postfix_str(f"{page_path.name} layout")
    visual_regions = detect_visual_regions(detection_image, scale, structure_engine)
    layout_table_bboxes = confirmed_table_bboxes(visual_regions)
    red_seal_regions = detect_red_seal_regions(detection_image, scale, visual_regions)
    if red_seal_regions:
        visual_regions.extend(red_seal_regions)
    has_seal = any(region["type"] == "seal" for region in visual_regions)
    seal_bboxes = [region["bbox"] for region in visual_regions if region["type"] == "seal"]
    red_ink_bboxes = [
        region["bbox"] for region in visual_regions
        if region["type"] in {"seal", "red_mark"}
    ]
    if progress is not None:
        progress.set_postfix_str(f"{page_path.name} checkbox")
    # Checkbox detector output is final after its own ONNX + geometry + NMS
    # gates.  Materialize it before OCR so the same boxes can be excluded from
    # text crops and later veto signature false positives.
    checkbox_detections = checkbox_detector.detect(original)
    checkbox_elements = materialize_checkbox_elements(
        original=original,
        checkboxes=checkbox_detections,
        page_crop_dir=page_crop_dir,
        root=root,
    )
    checkbox_boxes = [item["bbox"] for item in checkbox_elements]

    if progress is not None:
        progress.set_postfix_str(f"{page_path.name} signature-proposals")
    # Full-page YOLOS output is RAW proposal data only.  It can guide later
    # contextual ROI recovery but is never materialized directly.
    raw_signature_proposals, _ = detect_signature_boxes(
        detection_image=detection_image,
        tiny_detector=signature_detector,
        fallback_detector=None,
        elements=[],
        has_seal=has_seal,
    )

    # OCR preprocessing trusts confirmed seal boxes only.  Raw signature
    # proposals are intentionally excluded because signatures may be black and
    # a false-positive proposal must not alter OCR preprocessing.
    overlap_bboxes = seal_bboxes
    color_views: dict[str, Image.Image] = {}
    if red_ink_bboxes:
        black_text_view = original.copy()
        for box in red_ink_bboxes:
            local = local_color_views(original, box, red_ink_bboxes, [])
            black_text_view.paste(local["black"], (box[0], box[1]))
        color_views["black"] = black_text_view
        del black_text_view

    if progress is not None:
        progress.set_postfix_str(f"{page_path.name} text-det")
    text_crops = collect_text_crops(
        original=original,
        black_view=color_views.get("black"),
        detection_image=detection_image,
        scale=scale,
        text_detector=text_detector,
        overlap_bboxes=overlap_bboxes,
        ignore_bboxes=[*checkbox_boxes, *layout_table_bboxes],
    )
    if progress is not None:
        progress.set_postfix_str(f"{page_path.name} vietocr {len(text_crops)} lines")
    routing_text_elements = recognize_text_crops(
        text_crops,
        vietocr,
        batch_size=vietocr_batch_size,
        save_dir=page_crop_dir / "text" if save_text_crops else None,
        root=root if save_text_crops else None,
    )
    del text_crops

    # Link nearby labels (e.g. Có/Không) to the already-final checkbox objects.
    link_checkbox_labels(
        text_elements=routing_text_elements,
        checkbox_elements=checkbox_elements,
        page_width=original.size[0],
    )

    if progress is not None:
        progress.set_postfix_str(f"{page_path.name} signature-context")
    final_signature_detections = recover_contextual_signatures(
        original=original,
        signature_detector=signature_detector,
        text_elements=routing_text_elements,
        detections=raw_signature_proposals,
        seal_bboxes=seal_bboxes,
        scale=scale,
        roi_threshold=signature_roi_threshold,
        checkbox_bboxes=checkbox_boxes,
    )

    # Optional heavier signature model is also contextual.  It never gets a
    # direct full-page-to-final path.
    sig_detector = signature_detector
    if (
        not final_signature_detections
        and signature_fallback_detector is not None
    ):
        if progress is not None:
            progress.set_postfix_str(f"{page_path.name} signature-context-fallback")
        final_signature_detections = recover_contextual_signatures(
            original=original,
            signature_detector=signature_fallback_detector,
            text_elements=routing_text_elements,
            detections=raw_signature_proposals,
            seal_bboxes=seal_bboxes,
            scale=scale,
            roi_threshold=signature_roi_threshold,
            checkbox_bboxes=checkbox_boxes,
        )
        if final_signature_detections:
            sig_detector = signature_fallback_detector

    visual_regions.extend(infer_table_regions_from_text(
        routing_text_elements, original.size, visual_regions,
    ))
    elements = routing_text_outside_tables(routing_text_elements, visual_regions)
    elements.extend(checkbox_elements)

    table_index = 0
    visual_index = 0
    for region in visual_regions:
        if region["type"] == "table":
            table_index += 1
            if progress is not None:
                progress.set_postfix_str(f"{page_path.name} table {table_index}")
            elements.append(
                process_table_region(
                    original=original,
                    table_region=region,
                    page_crop_dir=page_crop_dir,
                    root=root,
                    table_id=f"table_{table_index:03d}",
                    table_engine=table_engine,
                    text_detector=text_detector,
                    vietocr=vietocr,
                    vietocr_batch_size=vietocr_batch_size,
                    ocr_cells=ocr_table_cells,
                    text_elements=routing_text_elements,
                    checkbox_elements=checkbox_elements,
                    black_view=color_views.get("black"),
                )
            )
        else:
            visual_index += 1
            has_seal = has_seal or region["type"] == "seal"
            elements.append(
                process_visual_region(
                    original=original,
                    region=region,
                    page_crop_dir=page_crop_dir,
                    root=root,
                    visual_id=f"{region['type']}_{visual_index:03d}",
                    logo_cache=logo_cache,
                )
            )

    color_views.clear()

    signature_elements = materialize_signature_elements(
        original=original,
        detections=final_signature_detections,
        used_detector=sig_detector,
        scale=scale,
        page_crop_dir=page_crop_dir,
        root=root,
    )
    elements.extend(signature_elements)
    seal_signature_elements = process_seal_signature_crops(
        original=original,
        visual_regions=visual_regions,
        signatures=signature_elements,
        page_crop_dir=page_crop_dir,
        root=root,
    )
    if save_color_debug_crops:
        # Save one independent folder of {black, red, blue}.png for EVERY seal,
        # EVERY signature, and EVERY combined seal-signature region. The normal
        # RGB object crops remain separately under seals/, signatures/ and
        # seal_signature/.
        debug_targets = [
            item for item in elements
            if item.get("type") in {"seal", "signature"}
        ] + list(seal_signature_elements)

        if debug_targets:
            red_boxes = [item["bbox"] for item in elements if item.get("type") in {"seal", "red_mark"}]
            blue_boxes = [item["bbox"] for item in signature_elements]
            view_folder_by_type = {
                "seal": "seals",
                "signature": "signatures",
                "seal_signature_region": "seal_signature",
            }
            for item in debug_targets:
                family = view_folder_by_type.get(str(item.get("type")))
                if family is None:
                    continue
                object_view_dir = page_crop_dir / "color_views" / family / str(item["id"])
                box = item["bbox"]
                padded = clamp_bbox([box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8], *original.size)
                views = local_color_views(original, padded, red_boxes, blue_boxes)
                item.update(
                    save_color_separation_crops(
                        views=views,
                        bbox=[0, 0, padded[2] - padded[0], padded[3] - padded[1]],
                        output_dir=object_view_dir,
                        root=root,
                        padding=0,
                    )
                )
    elements.extend(seal_signature_elements)
    # Finalize table ownership only after every page element has been added.
    # This guarantees OCR lines represented in table.cells are not serialized
    # a second time as top-level text elements.
    elements = absorb_text_elements_into_tables(elements)
    validate_referenced_crops(elements, root)

    result = {
        "page": {
            "symbol": symbol,
            "year": year,
            "page": page_num,
            "image": relpath(page_path, root),
            "width": original.size[0],
            "height": original.size[1],
            "dpi": 300,
        },
        "elements": elements,
        "provenance": {
            "architecture": "multi_resolution_conditional_fast_first",
            "detection_view": {
                "width": detection_image.size[0],
                "height": detection_image.size[1],
                "scale_x_to_original": scale.sx,
                "scale_y_to_original": scale.sy,
            },
            "layout_engine": "PaddleOCR PP-Structure filtered to table/image/seal",
            "text_detector": "PaddleOCR detector on low-resolution page view",
            "text_recognizer": "VietOCR batched in RAM",
            "checkbox_detector": checkbox_detector.repo_id,
            "checkbox_detector_source": "onnxruntime_huggingface",
            "checkbox_detection": getattr(checkbox_detector, "last_stats", {}),
            "signature_detector": signature_detector.model_name if signature_detector else None,
            "signature_full_page_threshold": signature_detector.threshold if signature_detector else None,
            "signature_context_roi_threshold": signature_roi_threshold if signature_detector else None,
            "signature_fallback_detector": signature_fallback_detector.model_name if signature_fallback_detector else None,
            "signature_full_page_proposal_count": len(raw_signature_proposals),
            "signature_final_count": len(final_signature_detections),
            "signature_policy": "full_page_proposals_context_recovery_validation_final_only",
            "checkbox_policy": "detector_geometry_nms_final_no_ocr_veto",
            "seal_policy": "ppstructure_plus_always_on_red_second_pass_iou_dedup",
            "notes": [
                "Detection runs on a resized page view; recognition and final object crops come from the original 300-DPI page.",
                "PP-Structure seal detection is always followed by detect_red_seal_regions(); red second-pass candidates are deduplicated against existing seals by IoU and can add missed seals on the same page.",
                "Checkbox detector output is final after ONNX confidence/NMS and frame-geometry validation; checkbox boxes are excluded from text OCR and are hard vetoes for signature candidates.",
                "Full-page YOLOS signature detections are raw proposals only. They are never materialized directly and never suppress contextual recovery.",
                "OCR preprocessing uses confirmed seal and rejected-red-mark boxes; raw signature proposals never alter the black view because handwriting may be blue or black.",
                "Signer roles are recovered from OCR text, including local same-row fragment joining such as KẾ TOÁN + TRƯỞNG.",
                "Role ROIs are deliberately wider than seal boxes so long handwritten signatures are not clipped; raw full-page proposals may only enlarge/localize these ROIs.",
                "Every role ROI is searched again with YOLOS on RGB and on a red-removed view; blue-chroma recovery is only a final rescue path for blue handwriting, not an assumption about signature color.",
                "Seal-context ROIs provide a second contextual route when role OCR is absent or garbled; duplicates with role-based detections are removed later.",
                "Signature candidates are rejected when they are mostly a final checkbox, essentially one printed OCR line, an implausibly tiny fragment, or spatially outside their role context.",
                "Only contextual candidates that pass validation and deduplication become final signatures and are materialized as signature_XXX.png / signature_XXX_ink.png.",
                "remove_red_ink is used to suppress red seal ink while retaining black/blue handwriting; black signatures rely on RGB/red-removed YOLOS rather than blue-color fallback.",
                "Text-layout routing OCR is used only to infer unruled-table geometry. Every inferred cell is cropped from a red-ink-cleaned 300-DPI view, stripped of table rules, filtered for edge fragments, then passed through Paddle TextDet plus VietOCR.",
                "Amount cells select one syntactically valid monetary token instead of concatenating OCR lines; unmatched parentheses retain negative sign evidence.",
                "Suspicious cells receive a conditional PaddleOCR recognition pass. Accounting relationships trigger re-OCR and may select a recognizer result, but never synthesize a value from arithmetic.",
                "Text elements owned by materialized table cells are removed from the page-level elements list and recorded once under their table; absorbed_text_ids preserves explicit ownership without duplicating OCR records.",
                "Seal-signature combined crops are created only after both seal and signature objects are final, when their boxes overlap or are near each other.",
                "Color debug views are diagnostic only: red visualizes seal ink and blue visualizes blue ink inside final signature regions; a valid black signature can therefore have an empty blue view.",
                "Each page is staged and validated before replacing previous crops and JSON; failed attempts preserve prior output.",
            ],
        },
    }

    write_json_atomic(output_json_path(output_root, page_path, pages_root, symbol_arg, year_arg), result)
    return result


def iter_pages(
    pages_root: Path,
    symbol: str | None,
    year: str | None,
    from_page: int | None,
    to_page: int | None,
) -> Iterable[Path]:
    search_root = pages_root
    if symbol and (pages_root / symbol).is_dir():
        search_root = pages_root / symbol
        if year and (search_root / year).is_dir():
            search_root /= year
    page_paths = search_root.rglob("page_*.png")
    for page_path in sorted(page_paths, key=lambda p: (p.parent.parent.name, p.parent.name, page_number(p))):
        number = page_number(page_path)
        inferred_symbol, inferred_year = infer_symbol_year(page_path, pages_root, symbol, year)
        if symbol and inferred_symbol.upper() != symbol.upper():
            continue
        if year and inferred_year != year:
            continue
        if from_page is not None and number < from_page:
            continue
        if to_page is not None and number > to_page:
            continue
        yield page_path


def has_complete_output(path: Path, root: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("page"), dict) or not isinstance(data.get("elements"), list):
            return False
        validate_referenced_crops(data["elements"], root)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fast OCR pipeline: low-res detection, 300-DPI recognition crops, batched VietOCR, conditional table/seal/signature/image."
    )
    parser.add_argument("--pages-root", type=Path, default=Path("pages"))
    parser.add_argument("--out-root", type=Path, default=Path("ocr_json"))
    parser.add_argument("--crops-root", type=Path, default=Path("ocr_crops"))
    parser.add_argument("--symbol", help="Only process one ticker, for example ADC.")
    parser.add_argument("--year", help="Only process one year, for example 2021.")
    parser.add_argument("--device", default="cuda:0", help="VietOCR/PyTorch device, for example cuda:0 or cpu.")
    parser.add_argument("--vietocr-config", default="vgg_transformer")
    parser.add_argument("--vietocr-batch-size", type=int, default=64)
    parser.add_argument("--detection-long-side", type=int, default=1600)
    parser.add_argument("--paddle-det-limit", type=int, default=1600)
    parser.add_argument("--paddle-det-thresh", type=float, default=0.30)
    parser.add_argument("--paddle-box-thresh", type=float, default=0.50)
    parser.add_argument("--checkbox-model", default="wendys-llc/checkbox-detector")
    parser.add_argument("--checkbox-threshold", type=float, default=0.50)
    parser.add_argument("--checkbox-model-path", type=Path, help="Local ONNX file for offline/reproducible runs.")
    parser.add_argument("--checkbox-output-format", choices=("auto", "yolo", "end2end"), default="auto")
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--checkbox-magnify", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--checkbox-input-size", type=int, default=1024)
    parser.add_argument("--checkbox-source-dpi", type=int, default=300)
    parser.add_argument("--checkbox-model-dpi", type=int, default=72)
    parser.add_argument("--checkbox-magnify-dpi", type=int, default=144, help="Second checkbox inference pass for small boxes; 144 DPI is recommended for 300-DPI scans.")
    parser.add_argument("--checkbox-geometry-fallback", action="store_true", help="Opt in to OpenCV square fallback. Disabled by default because strict ONNX+geometry filtering is much cleaner on financial statements.")
    parser.add_argument("--signature-threshold", type=float, default=0.70)
    parser.add_argument("--signature-roi-threshold", type=float, default=0.28)
    parser.add_argument("--signature-model", default="mdefrance/yolos-base-signature-detection")
    parser.add_argument("--signature-fallback-model", default="")
    parser.add_argument("--from-page", type=int, help="Start from this page number.")
    parser.add_argument("--to-page", type=int, help="Stop at this page number.")
    parser.add_argument("--limit", type=int, help="Maximum number of pages to process in this run.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip pages that already have final JSON output.")
    parser.add_argument("--overwrite", action="store_true", help="Reprocess pages even if JSON already exists.")
    parser.add_argument("--no-signature", action="store_true", help="Disable signature detection for faster runs.")
    parser.add_argument("--no-table-cell-ocr", action="store_true", help="Keep table bbox/HTML only; do not OCR every cell.")
    parser.add_argument("--save-text-crops", action="store_true", help="Also save text line crops. Slower and uses more disk.")
    parser.add_argument("--save-color-debug-crops", dest="save_color_debug_crops", action="store_true", help="Save black/red/blue folders for every seal, signature and seal-signature region (enabled by default).")
    parser.add_argument("--no-color-debug-crops", dest="save_color_debug_crops", action="store_false", help="Disable black/red/blue object-view folders.")
    parser.set_defaults(save_color_debug_crops=True)
    parser.add_argument("--debug-errors", action="store_true", help="Print full traceback for failed pages.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on the first failed page.")
    parser.add_argument("--progress-json", action="store_true", help="Emit progress events for a notebook progress bar.")
    args = parser.parse_args()
    for name in ("vietocr_batch_size", "detection_long_side", "paddle_det_limit", "checkbox_input_size",
                 "checkbox_source_dpi", "checkbox_model_dpi", "checkbox_magnify_dpi", "cpu_threads"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("checkbox_threshold", "signature_threshold", "signature_roi_threshold"):
        if not 0 < getattr(args, name) <= 1:
            parser.error(f"--{name.replace('_', '-')} must be in (0, 1]")

    root = Path.cwd()
    pages = list(iter_pages(args.pages_root, args.symbol, args.year, args.from_page, args.to_page))
    if not pages:
        parser.error(f"No matching page_*.png files under {args.pages_root}")
    skipped = 0
    if args.skip_existing and not args.overwrite:
        pending = []
        for page in pages:
            path = output_json_path(args.out_root, page, args.pages_root, args.symbol, args.year)
            if has_complete_output(path, root):
                skipped += 1
            else:
                pending.append(page)
        pages = pending
    if args.limit is not None:
        if args.limit <= 0:
            parser.error("--limit must be positive")
        pages = pages[:args.limit]
    if not pages:
        write_json_atomic(args.out_root / "_last_run_manifest.json", {
            "processed": 0, "skipped": skipped, "failed": 0, "interrupted": False,
            "status": "nothing_to_do", "pages_root": str(args.pages_root),
        })
        print(f"[done] processed=0 skipped={skipped} failed=0 (models not loaded)", flush=True)
        return
    import cv2
    cv2.setNumThreads(args.cpu_threads)
    print("[init] Paddle PPStructure layout engine", flush=True)
    structure_engine = PaddleStructureEngine(lang="en", use_seal_recognition=False)
    print("[init] Paddle PPStructure table engine", flush=True)
    table_engine = PaddleStructureEngine(lang="en", use_seal_recognition=False, table_only=True, lazy=True)
    print("[init] Paddle text detector", flush=True)
    text_detector = PaddleTextDetector(
        lang="vi",
        det_limit_side_len=args.paddle_det_limit,
        det_db_thresh=args.paddle_det_thresh,
        det_db_box_thresh=args.paddle_box_thresh,
    )
    print("[init] VietOCR", flush=True)
    vietocr = VietOCRRecognizer(device=args.device, config_name=args.vietocr_config)
    print("[init] Checkbox detector ONNX", flush=True)
    checkbox_detector = CheckboxDetectorONNX(
        repo_id=args.checkbox_model,
        threshold=args.checkbox_threshold,
        input_size=args.checkbox_input_size,
        source_dpi=args.checkbox_source_dpi,
        model_dpi=args.checkbox_model_dpi,
        magnify_dpi=args.checkbox_magnify_dpi,
        enable_geometry_fallback=args.checkbox_geometry_fallback,
        model_path=args.checkbox_model_path,
        output_format=args.checkbox_output_format,
        threads=args.cpu_threads,
        magnify_mode=args.checkbox_magnify,
    )
    logo_cache = LogoCache()

    signature_detector = None
    signature_fallback_detector = None
    if not args.no_signature:
        print("[init] Signature detector", flush=True)
        signature_detector = SignatureDetector(
            model_name=args.signature_model,
            threshold=args.signature_threshold,
            device=args.device,
        )
        if args.signature_fallback_model:
            print("[init] Signature fallback detector", flush=True)
            signature_fallback_detector = SignatureDetector(
                model_name=args.signature_fallback_model,
                threshold=args.signature_threshold,
                device=args.device,
            )

    processed = 0
    failed_count = 0
    print(f"[init] Found {len(pages)} page(s) to consider", flush=True)

    progress = JsonProgress(pages) if args.progress_json else tqdm(pages, desc="OCR pages", unit="page", dynamic_ncols=True, leave=True, mininterval=1.0)
    interrupted = False
    for page_path in progress:
        json_path = output_json_path(args.out_root, page_path, args.pages_root, args.symbol, args.year)
        if args.skip_existing and not args.overwrite and has_complete_output(json_path, root):
            skipped += 1
            progress.set_postfix_str(f"skip {page_path.name}")
            continue

        progress.set_postfix_str(page_path.name)
        try:
            process_page(
                page_path=page_path,
                pages_root=args.pages_root,
                root=root,
                output_root=args.out_root,
                crops_root=args.crops_root,
                structure_engine=structure_engine,
                table_engine=table_engine,
                text_detector=text_detector,
                vietocr=vietocr,
                checkbox_detector=checkbox_detector,
                signature_detector=signature_detector,
                signature_fallback_detector=signature_fallback_detector,
                logo_cache=logo_cache,
                detection_long_side=args.detection_long_side,
                signature_roi_threshold=args.signature_roi_threshold,
                vietocr_batch_size=args.vietocr_batch_size,
                ocr_table_cells=not args.no_table_cell_ocr,
                save_text_crops=args.save_text_crops,
                save_color_debug_crops=args.save_color_debug_crops,
                symbol_arg=args.symbol,
                year_arg=args.year,
                progress=progress,
            )
            processed += 1
            progress.set_postfix_str(f"ok {page_path.name}")
        except KeyboardInterrupt:
            print("[stop] Interrupted by user. Finished pages are safe on disk.", flush=True)
            interrupted = True
            break
        except Exception as exc:
            error = traceback.format_exc()
            failed_count += 1
            failure = {"page": str(page_path), "error": repr(exc), "traceback": error}
            failure_path = args.out_root / "_failures.jsonl"
            failure_path.parent.mkdir(parents=True, exist_ok=True)
            with failure_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(failure, ensure_ascii=False) + "\n")
            progress.set_postfix_str(f"fail={failed_count} {page_path.name}")
            if args.debug_errors:
                print(error, file=sys.stderr, flush=True)
            if args.fail_fast:
                break

    progress.close()

    manifest = {
        "processed": processed,
        "skipped": skipped,
        "failed": failed_count,
        "interrupted": interrupted,
        "pages_root": str(args.pages_root),
        "out_root": str(args.out_root),
        "crops_root": str(args.crops_root),
        "symbol": args.symbol,
        "year": args.year,
        "from_page": args.from_page,
        "to_page": args.to_page,
        "limit": args.limit,
        "skip_existing": args.skip_existing,
        "overwrite": args.overwrite,
        "detection_long_side": args.detection_long_side,
        "vietocr_batch_size": args.vietocr_batch_size,
        "checkbox_model": args.checkbox_model,
        "checkbox_threshold": args.checkbox_threshold,
        "checkbox_input_size": args.checkbox_input_size,
        "checkbox_source_dpi": args.checkbox_source_dpi,
        "checkbox_model_dpi": args.checkbox_model_dpi,
        "checkbox_magnify_dpi": args.checkbox_magnify_dpi,
        "checkbox_magnify_mode": args.checkbox_magnify,
        "checkbox_output_format": checkbox_detector.output_format,
        "checkbox_model_path": checkbox_detector.model_path,
        "cpu_threads": args.cpu_threads,
        "checkbox_geometry_fallback_enabled": args.checkbox_geometry_fallback,
        "signature_enabled": not args.no_signature,
        "signature_model": args.signature_model if not args.no_signature else None,
        "signature_threshold": args.signature_threshold,
        "signature_roi_threshold": args.signature_roi_threshold,
        "signature_fallback_model": args.signature_fallback_model or None,
        "table_cell_ocr_enabled": not args.no_table_cell_ocr,
        "text_crop_archive_enabled": args.save_text_crops,
        "color_debug_crop_archive_enabled": args.save_color_debug_crops,
        "logo_cache_size": len(logo_cache.values),
    }
    write_json_atomic(args.out_root / "_last_run_manifest.json", manifest)
    print(f"[done] processed={processed} skipped={skipped} failed={failed_count}", flush=True)
    if interrupted:
        raise SystemExit(130)
    if failed_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
