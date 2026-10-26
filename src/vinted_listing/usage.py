"""Usage ledger (sqlite) with cost estimates, and the budget guard that defers new items."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from vinted_listing.config import MODEL_PRICES, OPUS, Settings

CACHE_WRITE_FACTOR = 1.25  # 5-minute cache writes cost 1.25x input

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    ts TEXT NOT NULL,
    item TEXT NOT NULL,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    searches INTEGER NOT NULL,
    fetches INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    fallback INTEGER NOT NULL
)
"""


def _count(value) -> int:
    return int(value or 0)


def server_tool_counts(usage) -> tuple[int, int]:
    tools = getattr(usage, "server_tool_use", None)
    return _count(getattr(tools, "web_search_requests", 0)), _count(getattr(tools, "web_fetch_requests", 0))


def used_fallback(usage) -> bool:
    return any(getattr(it, "type", None) == "fallback_message" for it in getattr(usage, "iterations", None) or [])


def estimate_cost(usage, model: str, settings: Settings) -> float:
    """USD at list prices. A model missing from MODEL_PRICES is priced at Opus rates."""
    price = MODEL_PRICES.get(model, MODEL_PRICES[OPUS])
    searches, _ = server_tool_counts(usage)
    return (
        _count(getattr(usage, "input_tokens", 0)) * price.input
        + _count(getattr(usage, "cache_creation_input_tokens", 0)) * price.input * CACHE_WRITE_FACTOR
        + _count(getattr(usage, "cache_read_input_tokens", 0)) * price.cache_read
        + _count(getattr(usage, "output_tokens", 0)) * price.output
    ) / 1_000_000 + searches * settings.price_per_search


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class UsageLedger:
    """One row per API response. Times are local, so "today" and "this month" match the user's calendar."""

    def __init__(self, db_path: Path, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings
        self._item = ""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return closing(sqlite3.connect(self.db_path))  # type: ignore[return-value]

    @contextmanager
    def item_scope(self, name: str) -> Iterator[None]:
        previous, self._item = self._item, name
        try:
            yield
        finally:
            self._item = previous

    def record(self, stage: str, model: str, usage, now: datetime | None = None) -> None:
        now = now or datetime.now()
        known_model = model in MODEL_PRICES
        approximate = used_fallback(usage) or not known_model
        searches, fetches = server_tool_counts(usage)
        row = (
            now.isoformat(timespec="seconds"), self._item, stage, model,
            _count(getattr(usage, "input_tokens", 0)), _count(getattr(usage, "output_tokens", 0)),
            _count(getattr(usage, "cache_read_input_tokens", 0)), searches, fetches,
            estimate_cost(usage, model, self.settings), int(approximate),
        )
        with self._connect() as conn, conn:
            conn.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)

    def _scalar(self, sql: str, *args):
        with self._connect() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def month_cost_usd(self, now: datetime | None = None) -> float:
        start = _month_start(now or datetime.now()).isoformat()
        return float(self._scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE ts >= ?", start))

    def items_started_today(self, now: datetime | None = None) -> int:
        start = _day_start(now or datetime.now()).isoformat()
        return int(self._scalar("SELECT COUNT(DISTINCT item) FROM usage WHERE ts >= ?", start))

    def item_cost_usd(self, item: str) -> float:
        return float(self._scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE item = ?", item))

    def month_summary(self, now: datetime | None = None) -> dict:
        start = _month_start(now or datetime.now()).isoformat()
        with self._connect() as conn:
            cost, items, searches, fetches, calls, approx = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COUNT(DISTINCT item), COALESCE(SUM(searches),0),"
                " COALESCE(SUM(fetches),0), COUNT(*), COALESCE(MAX(fallback),0) FROM usage WHERE ts >= ?",
                (start,),
            ).fetchone()
        budget = self.settings.monthly_budget_usd
        return {
            "cost_usd": float(cost), "items": int(items), "searches": int(searches), "fetches": int(fetches),
            "calls": int(calls), "approximate": bool(approx), "budget_usd": budget, "remaining_usd": budget - float(cost),
        }


class BudgetGuard:
    def __init__(self, ledger: UsageLedger, settings: Settings) -> None:
        self.ledger = ledger
        self.settings = settings

    def blocked_reason(self, now: datetime | None = None) -> str | None:
        """Why a new item may not start right now, or None when it may."""
        cost = self.ledger.month_cost_usd(now)
        if cost >= self.settings.monthly_budget_usd:
            return f"monthly budget reached (${cost:.2f} of ${self.settings.monthly_budget_usd:.2f})"
        if self.ledger.items_started_today(now) >= self.settings.daily_item_limit:
            return f"daily item limit reached ({self.settings.daily_item_limit})"
        return None
