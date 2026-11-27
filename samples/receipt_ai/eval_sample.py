"""Sketch of how DULL scores receipt readings against ground truth.

Synthetic data only. The real ground truth and the held-out receipts are
private and never leave the developer's PC.

Metrics (per receipt, then aggregated):
  - no_edit:  the user would not have to touch anything (merchant, date,
              total and every item right)
  - edits:    how many fixes the user would need (wrong / missing / extra
              rows, wrong total, wrong merchant)
  - item F1:  row matching by (name, amount)
  - silent errors: wrong values the app did NOT warn about (the worst kind)

Rules that keep the evaluation honest:
  - dev / held-out split is fixed before tuning; tuning only looks at dev
  - the held-out set is scored once per frozen app version, never tuned on
  - a confidence interval (Wilson) is reported, so small gains are not
    mistaken for real ones

Run:  python eval_sample.py
"""

from __future__ import annotations

import math
from collections import Counter


def item_scores(truth: list[tuple[str, int]], pred: list[tuple[str, int]]) -> tuple[int, int, int]:
    """Return (matched, missing, extra) using multiset matching."""
    t, p = Counter(truth), Counter(pred)
    matched = sum((t & p).values())
    return matched, sum(t.values()) - matched, sum(p.values()) - matched


def score_receipt(truth: dict, pred: dict) -> dict:
    matched, missing, extra = item_scores(truth["items"], pred["items"])
    wrong_total = truth["total"] != pred["total"]
    wrong_merchant = truth["merchant"] != pred["merchant"]
    edits = missing + extra + int(wrong_total) + int(wrong_merchant)
    # A silent error is a wrong value with no warning attached to it.
    silent = 0 if pred.get("warned") else int(edits > 0)
    return {
        "no_edit": edits == 0,
        "edits": edits,
        "matched": matched,
        "truth_rows": len(truth["items"]),
        "pred_rows": len(pred["items"]),
        "silent": silent,
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def summarize(name: str, rows: list[dict]) -> None:
    n = len(rows)
    k = sum(r["no_edit"] for r in rows)
    lo, hi = wilson(k, n)
    m = sum(r["matched"] for r in rows)
    prec = m / max(sum(r["pred_rows"] for r in rows), 1)
    rec = m / max(sum(r["truth_rows"] for r in rows), 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-9)
    print(f"[{name}] n={n} no-edit={k / n:.0%} (95% CI {lo:.0%}-{hi:.0%}) "
          f"edits/receipt={sum(r['edits'] for r in rows) / n:.2f} item F1={f1:.2f} "
          f"silent errors={sum(r['silent'] for r in rows)}")


# Synthetic ground truth and predictions (fictional shops and items).
DEV = [
    ({"merchant": "店A", "total": 300, "items": [("パン", 200), ("牛乳", 100)]},
     {"merchant": "店A", "total": 300, "items": [("パン", 200), ("牛乳", 100)]}),
    ({"merchant": "店B", "total": 500, "items": [("ノート", 500)]},
     {"merchant": "店B", "total": 500, "items": [("ノート", 500)]}),
    ({"merchant": "店C", "total": 760, "items": [("定食", 680), ("小鉢", 80)]},
     {"merchant": "店C", "total": 760, "items": [("定食", 80), ("小鉢", 680)], "warned": True}),
]
HELD_OUT = [
    ({"merchant": "店D", "total": 420, "items": [("お茶", 120), ("おにぎり", 300)]},
     {"merchant": "店D", "total": 420, "items": [("お茶", 120), ("おにぎり", 300)]}),
    ({"merchant": "店E", "total": 990, "items": [("洗剤", 990)]},
     {"merchant": "店F", "total": 990, "items": [("洗剤", 990)]}),
]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    summarize("dev", [score_receipt(t, p) for t, p in DEV])
    # In the real workflow this line runs once per frozen version, after tuning.
    summarize("held-out", [score_receipt(t, p) for t, p in HELD_OUT])
