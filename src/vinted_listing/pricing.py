"""Deterministic price suggestion: hint price, else comparables, else retail price, else none."""

from __future__ import annotations

import re
import statistics
from decimal import ROUND_HALF_UP, Decimal

from vinted_listing.models import Condition, PriceSuggestion, ProductHint, ProductSpecs, SourcedValue

# Starting values; tune them against the prices you actually ask (acceptance run).
COMPARABLES_FACTOR = {
    Condition.NEW_WITH_TAGS: 1.15,
    Condition.NEW_WITHOUT_TAGS: 1.05,
    Condition.VERY_GOOD: 1.0,
    Condition.GOOD: 0.85,
    Condition.SATISFACTORY: 0.7,
}
RETAIL_FACTOR = {
    Condition.NEW_WITH_TAGS: 0.6,
    Condition.NEW_WITHOUT_TAGS: 0.5,
    Condition.VERY_GOOD: 0.4,
    Condition.GOOD: 0.3,
    Condition.SATISFACTORY: 0.2,
}
MIN_PRICE = 1.0
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _number(value: str) -> float | None:
    match = _NUMBER.search(value)
    return float(match.group().replace(",", ".")) if match else None


def _trusted_number(value: SourcedValue | None) -> float | None:
    if value is None or value.source == "inferred":
        return None
    return _number(value.value)


def round_price(raw: float) -> float:
    """Nearest 0.50 below 10, nearest whole euro from 10 up (halves round up), never below 1."""
    step = Decimal("0.5") if raw < 10 else Decimal("1")
    rounded = (Decimal(str(raw)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return max(MIN_PRICE, float(rounded))


def _fmt(value: float) -> str:
    return f"{value:g}"


def suggest_price(hint: ProductHint, specs: ProductSpecs, condition: Condition) -> PriceSuggestion:
    if hint.price_eur is not None:
        return PriceSuggestion(price_eur=hint.price_eur, method="hint", rationale="from hint.txt")

    comps = [n for v in specs.second_hand_prices_eur if v.source == "web" and (n := _number(v.value)) is not None]
    if comps:
        median = statistics.median(comps)
        factor = COMPARABLES_FACTOR[condition]
        raw = median * factor
        price = round_price(raw)
        noun = "comparable" if len(comps) == 1 else "comparables"
        listed = ", ".join(_fmt(c) for c in comps)
        rationale = (f"median of {len(comps)} {noun} ({listed}) × {_fmt(factor)} for {condition.value} "
                     f"= {_fmt(round(raw, 2))} → {_fmt(price)}")
        return PriceSuggestion(price_eur=price, method="comparables", rationale=rationale)

    if hint.retail_price_eur is not None:
        retail, origin = hint.retail_price_eur, "hint.txt"
    else:
        retail, origin = _trusted_number(specs.retail_price_eur), "research"
    if retail is not None:
        factor = RETAIL_FACTOR[condition]
        raw = retail * factor
        price = round_price(raw)
        rationale = (f"retail {_fmt(retail)} (from {origin}) × {_fmt(factor)} for {condition.value} "
                     f"= {_fmt(round(raw, 2))} → {_fmt(price)}")
        return PriceSuggestion(price_eur=price, method="retail", rationale=rationale)

    return PriceSuggestion(price_eur=None, method="none", rationale="no comparables or retail price found; set manually")
