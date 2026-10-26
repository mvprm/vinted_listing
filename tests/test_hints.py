import pytest

from vinted_listing.hints import parse_hint
from vinted_listing.models import Condition


def test_folder_name_only():
    h = parse_hint("levis-501_w32", None)
    assert h.free_text == "levis 501 w32" and h.brand is None and h.warnings == []


def test_full_hint_file():
    h = parse_hint("x", "Brand: Levi's\nsize: W32 L32\ncomposition: 100% cotton\ncolour: indigo\n"
                        "condition: very good\nprice: 12,50 €\nretail_price: 99.95\nmatch: image")
    assert (h.brand, h.size, h.composition, h.colour) == ("Levi's", "W32 L32", "100% cotton", "indigo")
    assert (h.condition, h.price_eur, h.retail_price_eur, h.force_image_match) == (Condition.VERY_GOOD, 12.5, 99.95, True)


def test_research_flag():
    assert parse_hint("x", "research: No").research is False and parse_hint("x", None).research is None
    assert parse_hint("x", "research: YES").research is True


def test_blank_template_values_are_ignored():
    h = parse_hint("x", "brand:\nmodel:\nsize: M\nprice:\n")
    assert h.size == "M" and h.brand is None and h.price_eur is None and h.warnings == []


def test_bad_values_become_warnings():
    h = parse_hint("x", "condition: like new\nprice: cheap\nseason: winter")
    assert h.condition is None and h.price_eur is None and len(h.warnings) == 3


@pytest.mark.parametrize("raw, expected", [("12", 12.0), ("12.5", 12.5), ("12,50", 12.5), ("12€", 12.0),
                                           ("12 EUR", 12.0), ("12,50 €", 12.5)])
def test_price_formats(raw, expected):
    assert parse_hint("x", f"price: {raw}").price_eur == expected


def test_bom_crlf_and_value_with_colon():
    h = parse_hint("x", "﻿BRAND: Zara\r\nnotes: stain: left sleeve\r\n")
    assert h.brand == "Zara" and h.notes == "stain: left sleeve" and h.warnings == []


def test_invalid_research_and_match_values_warn():
    h = parse_hint("x", "research: maybe\nmatch: text")
    assert h.research is None and h.force_image_match is False and len(h.warnings) == 2


def test_line_without_colon_warns():
    assert len(parse_hint("x", "just some words").warnings) == 1
