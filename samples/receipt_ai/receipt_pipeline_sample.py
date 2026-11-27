"""Simplified, self-contained sketch of DULL's receipt pipeline.

This is NOT the app's code. It re-implements the ideas in a few hundred lines
so they can be read and run without the app:

  1. OCR lines (text + box in pixels) from two engines
  2. upright correction: detect a sideways photo from line shapes, turn it,
     then level a small tilt (deskew)
  3. build the AI input: normalized coordinates, short line ids (M1, P1, ...)
  4. validate the AI answer against the OCR text (evidence ids, amounts,
     arithmetic, the "yen sign read as 4" pattern)

All data below is synthetic. The AI call itself is replaced by a fixed answer;
the real prompt is not part of this repository.

Run:  python receipt_pipeline_sample.py
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. OCR lines
# ---------------------------------------------------------------------------

@dataclass
class OcrLine:
    text: str
    # Axis-aligned box in image pixels: left, top, right, bottom.
    box: tuple[float, float, float, float]

    @property
    def aspect(self) -> float:
        l, t, r, b = self.box
        return (r - l) / max(b - t, 1e-6)


# ---------------------------------------------------------------------------
# 2. Upright correction
# ---------------------------------------------------------------------------

SIDEWAYS_ASPECT = 1.0  # receipt lines are wide; below 1.0 they run vertically


def is_sideways(lines: list[OcrLine]) -> bool:
    """A photo taken sideways makes text lines tall and narrow."""
    if not lines:
        return False
    return statistics.median(l.aspect for l in lines) < SIDEWAYS_ASPECT


def turn_counterclockwise(line: OcrLine, image_w: float) -> OcrLine:
    """Rotate a box 90 degrees counter-clockwise in a (w x h) image -> (h x w).

    Text that runs downward means the receipt's top points right, so a
    counter-clockwise turn makes it upright: (x, y) -> (y, w - x).
    """
    l, t, r, b = line.box
    return OcrLine(line.text, (t, image_w - r, b, image_w - l))


def estimate_tilt(lines: list[OcrLine]) -> float:
    """Tilt in radians from the drift of line centers along long lines.

    Real engines give rotated quads; this sketch fakes tilt with the slope
    between consecutive line centers on the same row band, which is enough to
    show the idea: measure a median angle, then rotate by its negative.
    """
    angles = []
    rows = sorted(lines, key=lambda l: l.box[1])
    for a, b in zip(rows, rows[1:]):
        ax, ay = (a.box[0] + a.box[2]) / 2, (a.box[1] + a.box[3]) / 2
        bx, by = (b.box[0] + b.box[2]) / 2, (b.box[1] + b.box[3]) / 2
        if abs(by - ay) < (a.box[3] - a.box[1]) * 0.6 and bx != ax:
            angles.append(math.atan2(by - ay, bx - ax))
    return statistics.median(angles) if angles else 0.0


def deskew(lines: list[OcrLine], angle: float) -> list[OcrLine]:
    """Rotate box centers by -angle around the origin, keeping box sizes."""
    if abs(angle) < 1e-3:
        return lines
    c, s = math.cos(-angle), math.sin(-angle)
    out = []
    for ln in lines:
        l, t, r, b = ln.box
        cx, cy, w, h = (l + r) / 2, (t + b) / 2, r - l, b - t
        nx, ny = cx * c - cy * s, cx * s + cy * c
        out.append(OcrLine(ln.text, (nx - w / 2, ny - h / 2, nx + w / 2, ny + h / 2)))
    return out


def make_upright(lines: list[OcrLine], image_w: float, image_h: float) -> tuple[list[OcrLine], bool]:
    """Turn sideways readings upright; deskew only readings that were turned.

    (The app found that deskewing already-upright receipts made some worse,
    so it only levels the turned ones.)
    """
    if not is_sideways(lines):
        return lines, False
    turned = [turn_counterclockwise(l, image_w) for l in lines]
    return deskew(turned, estimate_tilt(turned)), True


# ---------------------------------------------------------------------------
# 3. AI input
# ---------------------------------------------------------------------------

def build_ai_input(readings: dict[str, list[OcrLine]]) -> dict:
    """Place lines of all engines in one shared frame, scaled to 0..1000.

    Each engine gets a prefix (M = engine 1, P = engine 2) so the AI can cite
    the exact line it took a value from. Lines are sorted top-to-bottom.
    """
    all_lines = [l for ls in readings.values() for l in ls]
    left = min(l.box[0] for l in all_lines)
    top = min(l.box[1] for l in all_lines)
    span_x = max(l.box[2] for l in all_lines) - left or 1
    span_y = max(l.box[3] for l in all_lines) - top or 1

    def q(v: float, origin: float, span: float) -> int:
        return round((v - origin) / span * 1000)

    request: dict = {}
    for prefix, lines in readings.items():
        placed = []
        for i, ln in enumerate(sorted(lines, key=lambda l: (l.box[1], l.box[0])), 1):
            l, t, r, b = ln.box
            placed.append({
                "id": f"{prefix}{i}",
                "text": ln.text,
                "box": [q(l, left, span_x), q(t, top, span_y), q(r, left, span_x), q(b, top, span_y)],
            })
        request[prefix] = placed
    return request


# ---------------------------------------------------------------------------
# 4. Validation of the AI answer
# ---------------------------------------------------------------------------

AMOUNT_RE = re.compile(r"(¥|\\)?\s*([0-9][0-9,]*)")


def amounts_in(text: str) -> list[tuple[int, bool]]:
    """All numbers in a line, with a flag for a printed yen sign."""
    out = []
    for m in AMOUNT_RE.finditer(text):
        digits = m.group(2).replace(",", "")
        if digits:
            out.append((int(digits), m.group(1) is not None))
    return out


def leading4_alternative(value: int) -> int | None:
    """OCR often reads a yen sign as '4': 4380 may really be 380."""
    s = str(value)
    return int(s[1:]) if len(s) >= 2 and s[0] == "4" else None


@dataclass
class Validation:
    items: list[dict]
    total: int | None
    warnings: list[str] = field(default_factory=list)


def validate(answer: dict, ai_input: dict) -> Validation:
    texts = {ln["id"]: ln["text"] for lines in ai_input.values() for ln in lines}
    warnings: list[str] = []
    items: list[dict] = []

    for row in answer.get("items", []):
        ev = [i for i in row.get("evidence", []) if i in texts]
        if not ev:
            # The AI cited no real line: keep the row but never trust it silently.
            warnings.append(f"no_evidence:{row.get('name', '')}")
        elif not any(row["amount"] == v for i in ev for v, _ in amounts_in(texts[i])):
            warnings.append(f"amount_not_in_text:{row.get('name', '')}")
        items.append({**row, "evidence": ev})

    total = answer.get("total")
    item_sum = sum(r["amount"] for r in items)
    if total is not None and items and item_sum != total:
        # Try the yen->4 fix on items whose evidence line has no yen sign.
        fixed = False
        for r in items:
            alt = leading4_alternative(r["amount"])
            if alt is not None and item_sum - r["amount"] + alt == total:
                warnings.append(f"leading4_fixed:{r['name']}:{r['amount']}->{alt}")
                r["amount"] = alt
                fixed = True
                break
        if not fixed:
            warnings.append("total_mismatch")
    # Names are never replaced by the validator; a name not found in any OCR
    # line is only flagged for the user to check.
    for r in items:
        if r.get("name") and not any(r["name"] in t for t in texts.values()):
            warnings.append(f"name_not_in_ocr:{r['name']}")
    return Validation(items, total, warnings)


# ---------------------------------------------------------------------------
# Demo with synthetic data
# ---------------------------------------------------------------------------

def demo() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp932
    # A fictional receipt photographed sideways (text runs downward).
    # Image is 3000 wide x 4000 high; each line is a tall, narrow box.
    image_w, image_h = 3000, 4000
    raw = [
        ("サンプル商店", 2600), ("りんご 4120", 2400), ("ノート ¥250", 2200),
        ("お茶 98", 2000), ("合計 ¥468", 1800),
    ]
    engine_m = [OcrLine(t, (x, 500, x + 80, 500 + 60 * len(t))) for t, x in raw]
    engine_p = [OcrLine(t.replace("4120", "¥120"), (x + 3, 502, x + 83, 502 + 60 * len(t))) for t, x in raw]

    upright_m, turned_m = make_upright(engine_m, image_w, image_h)
    upright_p, turned_p = make_upright(engine_p, image_w, image_h)
    print(f"sideways detected: M={turned_m} P={turned_p}")

    ai_input = build_ai_input({"M": upright_m, "P": upright_p})
    print("AI input (first 3 M lines):")
    print(json.dumps(ai_input["M"][:3], ensure_ascii=False))

    # Stand-in for the AI answer (structured JSON). It copied 4120 from M.
    answer = {
        "merchant": "サンプル商店",
        "items": [
            {"name": "りんご", "amount": 4120, "evidence": ["M2"]},
            {"name": "ノート", "amount": 250, "evidence": ["M3", "P3"]},
            {"name": "お茶", "amount": 98, "evidence": ["M4"]},
        ],
        "total": 468,
    }
    result = validate(answer, ai_input)
    print("validated items:", [(r["name"], r["amount"]) for r in result.items])
    print("warnings:", result.warnings)


if __name__ == "__main__":
    demo()
