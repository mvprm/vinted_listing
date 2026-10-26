import csv
import json

from fakes import FakeRemover, make_settings, write_photo
from test_pipeline_build import fake_llm
from vinted_listing.evaluation import compare_reports, run_eval, score_item
from vinted_listing.models import Condition, Listing, ParcelSize
from vinted_listing.pipeline import Deps
from vinted_listing.usage import BudgetGuard, UsageLedger


def listing(**kw):
    base = dict(title="t", description="d", category_path=["Homem"], material=None, condition=Condition.GOOD,
                colours=["blue"], parcel_size=ParcelSize.SMALL, brand=None, size=None, price_eur=None,
                price_rationale="", needs_review=[], sources=[])
    return Listing(**{**base, **kw})


def test_score_item():
    assert score_item(listing(brand="Levi's", size="W32 L32", category_path=["Homem", "Calças", "Jeans"], price_eur=18),
                      {"brand": "levi's", "size": "w32 l32", "category": ["jeans"], "price_range": [15, 25]}) == \
           {"brand": True, "size": True, "category": True, "price": True}


def test_score_item_misses_and_unscored_keys():
    scores = score_item(listing(brand=None, size="W 32", colours=["Navy"], price_eur=None),
                        {"brand": "Zara", "size": "w32", "colours": ["navy", "white"], "price_range": [5, 10]})
    assert scores == {"brand": False, "size": False, "colours": True, "price": False}


def test_run_eval_writes_report(tmp_path):
    golden = tmp_path / "golden"
    write_photo(golden / "levis" / "IMG_1.jpg")
    (golden / "levis" / "hint.txt").write_text("brand: Levi's\nsize: W32\nprice: 20", encoding="utf-8")
    (golden / "levis" / "expected.json").write_text(json.dumps({"brand": "Levi's", "size": "w32"}), encoding="utf-8")
    write_photo(golden / "toy" / "IMG_1.jpg")
    (golden / "notes").mkdir()  # no photos: skipped
    settings = make_settings(tmp_path)
    ledger = UsageLedger(settings.db_path, settings)
    deps = Deps(llm=fake_llm(), remover=FakeRemover(), ledger=ledger, budget=BudgetGuard(ledger, settings))

    report = run_eval(golden, settings, deps)

    assert report.name == "report.csv" and report.parent.name.startswith("eval-mixed-")
    rows = list(csv.DictReader(report.open(encoding="utf-8")))
    assert [r["item"] for r in rows] == ["levis", "toy"]
    assert rows[0]["status"] == "ok" and rows[0]["brand"] == "1" and rows[0]["size"] == "1" and rows[0]["category"] == ""
    assert rows[0]["model_listing"] == "claude-sonnet-5-5" and rows[0]["title"] == "Calças Levi's 501"
    assert (report.parent / "levis" / "listing.txt").exists()


def write_report(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["item", "status", "brand", "size", "category", "colours", "price", "cost_usd", "searches", "title",
              "description"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def test_compare_reports(tmp_path):
    a = tmp_path / "eval-opus-1" / "report.csv"
    b = tmp_path / "eval-mixed-1" / "report.csv"
    write_report(a, [{"item": "levis", "status": "ok", "brand": "1", "size": "1", "cost_usd": "0.70",
                      "title": "Opus title", "description": "Opus text"},
                     {"item": "toy", "status": "ok", "brand": "0", "cost_usd": "0.50", "title": "Opus toy"}])
    write_report(b, [{"item": "levis", "status": "ok", "brand": "1", "size": "0", "cost_usd": "0.30",
                      "title": "Mixed title", "description": "Mixed text"},
                     {"item": "toy", "status": "failed: research", "cost_usd": "0.05"}])
    text = compare_reports(a, b)
    assert "eval-opus-1" in text and "eval-mixed-1" in text
    assert "brand: 1/2" in text and "size: 0/1" in text
    assert "average cost per item: $0.60" in text and "average cost per item: $0.17" in text
    assert text.index("Opus title") < text.index("Mixed title") and "failed: research" in text
