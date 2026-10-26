import pytest

from vinted_listing.models import Condition, PriceSuggestion, ProductHint, ProductSpecs, SourcedValue
from vinted_listing.pricing import round_price, suggest_price


def hint(**kw):
    return ProductHint(free_text="x", **kw)


def web(value):
    return SourcedValue(value=value, source="web", source_url="https://real", confidence="high")


def inferred(value):
    return SourcedValue(value=value, source="inferred", source_url=None, confidence="low")


def specs_with(comps=(), retail=None):
    return ProductSpecs(product_name=SourcedValue(value="x", source="hint", source_url=None, confidence="high"),
                        brand=None, model=None, attributes=[], retail_price_eur=retail,
                        second_hand_prices_eur=list(comps), match_confidence="high")


def test_hint_price_wins():
    assert suggest_price(hint(price_eur=9.99), specs_with([web("50")]), Condition.GOOD) == \
        PriceSuggestion(price_eur=9.99, method="hint", rationale="from hint.txt")


def test_median_of_web_comparables_with_condition_factor():
    s = specs_with(comps=[web("12"), web("15"), web("18"), inferred("90")])  # inferred comp ignored
    p = suggest_price(hint(), s, Condition.GOOD)
    assert (p.price_eur, p.method) == (13.0, "comparables") and "3 comparables" in p.rationale
    assert p.rationale == "median of 3 comparables (12, 15, 18) × 0.85 for Good = 12.75 → 13"


def test_comparable_values_with_currency_text_are_parsed():
    p = suggest_price(hint(), specs_with([web("€15,50"), web("about 20 EUR"), web("n/a")]), Condition.VERY_GOOD)
    assert p.price_eur == 18.0 and "2 comparables" in p.rationale


def test_retail_fallback_prefers_hint_retail():
    p = suggest_price(hint(retail_price_eur=40.0), specs_with(retail=web("60")), Condition.VERY_GOOD)
    assert (p.price_eur, p.method) == (16.0, "retail")


def test_retail_from_specs_when_hint_has_none():
    p = suggest_price(hint(), specs_with(retail=web("60")), Condition.NEW_WITH_TAGS)
    assert (p.price_eur, p.method) == (36.0, "retail")


def test_inferred_retail_is_ignored():
    assert suggest_price(hint(), specs_with(retail=inferred("60")), Condition.GOOD).method == "none"


@pytest.mark.parametrize("raw, expected", [(7.2, 7.0), (7.25, 7.5), (7.8, 8.0), (9.74, 9.5), (12.5, 13.0),
                                           (0.3, 1.0), (10.4, 10.0)])
def test_small_prices_round_to_half_euro_and_floor_at_one(raw, expected):
    assert round_price(raw) == expected


def test_no_data_gives_none():
    p = suggest_price(hint(), specs_with(), Condition.GOOD)
    assert p.price_eur is None and p.method == "none" and "set manually" in p.rationale
