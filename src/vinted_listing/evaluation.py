"""Golden-set evaluation: score listings against expected values, compare two runs side by side."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from vinted_listing.config import STAGES, Settings
from vinted_listing.models import Listing
from vinted_listing.pipeline import BudgetDeferred, Deps, StepError, build_package, find_hint_file, list_photos

FIELDS = ("brand", "size", "category", "colours", "price")
REPORT_COLUMNS = ["item", "status", *FIELDS, *(f"model_{s}" for s in STAGES), "cost_usd", "searches", "title",
                  "description"]


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def _as_list(value) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def score_item(listing: Listing, expected: dict) -> dict[str, bool]:
    scores: dict[str, bool] = {}
    if "brand" in expected:
        scores["brand"] = _norm(listing.brand) == _norm(expected["brand"])
    if "size" in expected:
        scores["size"] = _norm(listing.size) == _norm(expected["size"])
    if "category" in expected:
        path = _norm(" > ".join(listing.category_path))
        scores["category"] = any(_norm(k) in path for k in _as_list(expected["category"]))
    if "colours" in expected:
        scores["colours"] = bool({_norm(c) for c in listing.colours} & {_norm(c) for c in _as_list(expected["colours"])})
    if "price_range" in expected:
        low, high = expected["price_range"]
        scores["price"] = listing.price_eur is not None and low <= listing.price_eur <= high
    return scores


def run_eval(golden_dir: Path, settings: Settings, deps: Deps) -> Path:
    out_root = settings.runs_dir / f"eval-{settings.model_profile}-{datetime.now():%Y%m%d-%H%M%S}"
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for folder in sorted(p for p in golden_dir.iterdir() if p.is_dir()):
        photos = list_photos(folder)
        if not photos:
            continue
        hint_file, hint_warnings = find_hint_file(folder)
        expected_file = folder / "expected.json"
        expected = json.loads(expected_file.read_text(encoding="utf-8")) if expected_file.exists() else {}
        before = deps.ledger.item_totals(folder.name)
        row = {"item": folder.name, **{f"model_{s}": settings.model_for(s) for s in STAGES}}
        try:
            result = build_package(folder.name, photos, hint_file, out_root / folder.name, settings, deps,
                                   extra_warnings=hint_warnings)
        except StepError as exc:
            row["status"] = f"failed: {exc.step}: {exc.cause}"
        except BudgetDeferred as exc:
            row["status"] = f"deferred: {exc.reason}"
        else:
            row["status"] = "ok"
            row["title"] = result.listing.title
            row["description"] = result.listing.description
            for field, ok in score_item(result.listing, expected).items():
                row[field] = "1" if ok else "0"
        after = deps.ledger.item_totals(folder.name)
        row["cost_usd"] = f"{after['cost_usd'] - before['cost_usd']:.4f}"
        row["searches"] = str(after["searches"] - before["searches"])
        rows.append(row)

    report = out_root / "report.csv"
    with report.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in REPORT_COLUMNS})
    return report


def _read(report: Path) -> list[dict]:
    with report.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _summary(label: str, rows: list[dict]) -> list[str]:
    lines = [f"{label}: {len(rows)} items, {sum(r['status'] == 'ok' for r in rows)} ok"]
    for field in FIELDS:
        scored = [r[field] for r in rows if r.get(field) in ("0", "1")]
        if scored:
            lines.append(f"  {field}: {scored.count('1')}/{len(scored)}")
    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd")]
    average = sum(costs) / len(costs) if costs else 0.0
    lines.append(f"  average cost per item: ${average:.2f} (total ${sum(costs):.2f})")
    return lines


def compare_reports(a: Path, b: Path) -> str:
    rows_a, rows_b = _read(a), _read(b)
    label_a, label_b = a.parent.name, b.parent.name
    lines = [*_summary(label_a, rows_a), "", *_summary(label_b, rows_b), ""]
    by_item_b = {r["item"]: r for r in rows_b}
    items = [r["item"] for r in rows_a] + [i for i in by_item_b if i not in {r["item"] for r in rows_a}]
    by_item_a = {r["item"]: r for r in rows_a}
    for item in items:
        lines.append(f"=== {item} ===")
        for label, row in ((label_a, by_item_a.get(item)), (label_b, by_item_b.get(item))):
            if row is None:
                lines.append(f"[{label}] (not in this run)")
            elif row["status"] != "ok":
                lines.append(f"[{label}] {row['status']}")
            else:
                lines += [f"[{label}] {row['title']}", row["description"]]
            lines.append("")
    return "\n".join(lines)
