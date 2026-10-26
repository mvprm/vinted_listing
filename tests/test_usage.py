from datetime import datetime

import pytest

from fakes import make_settings, usage
from vinted_listing.usage import BudgetGuard, UsageLedger, estimate_cost

NOW = datetime(2026, 10, 26, 12, 0, 0)


def test_estimate_cost_per_model(tmp_path):
    s = make_settings(tmp_path)
    u = usage(input_tokens=1_000_000, output_tokens=100_000, cache_read=0, searches=5)
    assert estimate_cost(u, "claude-opus-5-5", s) == pytest.approx(4.0 + 2.0 + 0.05)
    assert estimate_cost(u, "claude-haiku-5-5", s) == pytest.approx(0.10 + 0.05 + 0.05)


def test_unknown_model_priced_at_opus_rates(tmp_path):
    s = make_settings(tmp_path)
    u = usage(input_tokens=1_000_000, output_tokens=0)
    assert estimate_cost(u, "claude-mystery-9", s) == pytest.approx(4.0)


def test_budget_guard_blocks_when_month_spent(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path, monthly_budget_usd=1.0))
    with ledger.item_scope("a"):
        ledger.record("research", "claude-opus-5-5", usage(input_tokens=300_000), now=NOW)
    reason = BudgetGuard(ledger, ledger.settings).blocked_reason(NOW)
    assert reason == "monthly budget reached ($1.20 of $1.00)"


def test_budget_guard_daily_item_limit(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path, daily_item_limit=2))
    for item in ("a", "b", "b"):
        with ledger.item_scope(item):
            ledger.record("describe", "claude-haiku-5-5", usage(), now=NOW)
    assert ledger.items_started_today(NOW) == 2
    assert BudgetGuard(ledger, ledger.settings).blocked_reason(NOW) == "daily item limit reached (2)"


def test_budget_guard_allows_when_under_limits(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path))
    assert BudgetGuard(ledger, ledger.settings).blocked_reason(NOW) is None


def test_month_cost_ignores_previous_month(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path))
    with ledger.item_scope("a"):
        ledger.record("research", "claude-opus-5-5", usage(input_tokens=1_000_000, output_tokens=0),
                      now=datetime(2026, 9, 30, 23, 59))
        ledger.record("research", "claude-opus-5-5", usage(input_tokens=500_000, output_tokens=0), now=NOW)
    assert ledger.month_cost_usd(NOW) == pytest.approx(2.0)
    assert ledger.items_started_today(NOW) == 1


def test_month_summary_and_fallback_flag(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path, monthly_budget_usd=10.0))
    with ledger.item_scope("a"):
        ledger.record("research", "claude-sonnet-5-5", usage(searches=3, fetches=2, fallback=True), now=NOW)
    summary = ledger.month_summary(NOW)
    assert summary["items"] == 1 and summary["searches"] == 3 and summary["fetches"] == 2
    assert summary["approximate"] is True and summary["budget_usd"] == 10.0
    assert summary["remaining_usd"] == pytest.approx(10.0 - summary["cost_usd"])


def test_item_totals(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path))
    with ledger.item_scope("a"):
        ledger.record("research", "claude-opus-5-5", usage(input_tokens=1_000_000, output_tokens=0, searches=2,
                                                           fetches=1), now=NOW)
    assert ledger.item_totals("a") == {"cost_usd": pytest.approx(4.02), "searches": 2, "fetches": 1}
    assert ledger.item_totals("missing") == {"cost_usd": 0.0, "searches": 0, "fetches": 0}


def test_item_cost(tmp_path):
    ledger = UsageLedger(tmp_path / "s.db", make_settings(tmp_path))
    with ledger.item_scope("a"):
        ledger.record("describe", "claude-opus-5-5", usage(input_tokens=1_000_000, output_tokens=0), now=NOW)
    with ledger.item_scope("b"):
        ledger.record("describe", "claude-opus-5-5", usage(input_tokens=500_000, output_tokens=0), now=NOW)
    assert ledger.item_cost_usd("a") == pytest.approx(4.0)
