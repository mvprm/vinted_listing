from fakes import FakeLLM, make_settings, write_photo
from vinted_listing.listing import draft_listing, finalize_listing, render_listing_txt
from vinted_listing.models import (
    Attribute,
    Condition,
    Listing,
    ListingDraft,
    ParcelSize,
    PriceSuggestion,
    ProductHint,
    ProductSpecs,
    SourcedValue,
    VisualDescription,
)


def hint(**kw):
    return ProductHint(free_text="levis 501", **kw)


def draft(**kw):
    base = dict(title="Calças Levi's 501", description="Jeans clássicas.", category_path=["Homem", "Calças"],
                material="denim", condition=Condition.GOOD, colours=["blue"], parcel_size=ParcelSize.SMALL)
    return ListingDraft(**{**base, **kw})


def sv(value, source="web", url="https://real", confidence="high"):
    return SourcedValue(value=value, source=source, source_url=url, confidence=confidence)


def specs(match="high", brand=None, **kw):
    base = dict(product_name=sv("Levi's 501", url="https://real"), brand=brand, model=None, attributes=[],
                retail_price_eur=None, second_hand_prices_eur=[], match_confidence=match)
    return ProductSpecs(**{**base, **kw})


def price_suggestion(method="comparables", price_eur=13.0, rationale="median of 3 comparables"):
    return PriceSuggestion(price_eur=price_eur, method=method, rationale=rationale)


def desc():
    return VisualDescription(item_type="jeans", colours=["blue"], visible_text=[], pattern=None,
                             notable_features=[], condition_notes=["faded knees"])


def listing(**kw):
    base = dict(title="Calças Levi's 501", description="Jeans clássicas.", category_path=["Homem", "Calças"],
                material="denim", condition=Condition.GOOD, colours=["blue"], parcel_size=ParcelSize.SMALL,
                brand="Levi's", size="W32", price_eur=13.0, price_rationale="median", needs_review=[],
                sources=["https://real"])
    return Listing(**{**base, **kw})


def test_hint_overrides_and_review_flags(tmp_path):
    out = finalize_listing(draft(condition=Condition.GOOD, colours=["blue", "white", "red"], material="denim"),
                           hint(condition=Condition.VERY_GOOD, composition="100% cotton", brand="Levi's", size="W32"),
                           specs(match="high"), price_suggestion(method="comparables", price_eur=13.0),
                           demoted=["model"], settings=make_settings(tmp_path))
    assert out.condition == Condition.VERY_GOOD and out.colours == ["blue", "white"] and out.material == "100% cotton"
    assert set(out.needs_review) == {"price", "model"}
    assert out.price_eur == 13.0 and out.price_rationale == "median of 3 comparables"


def test_hint_colour_and_category_override(tmp_path):
    out = finalize_listing(draft(), hint(colour="indigo", category="Homem > Jeans", brand="Levi's", size="M",
                                         condition=Condition.GOOD),
                           specs(match="low"), price_suggestion(method="hint"), [], make_settings(tmp_path))
    assert out.colours == ["indigo"] and out.category_path == ["Homem > Jeans"] and out.needs_review == []


def test_hint_price_not_flagged(tmp_path):
    out = finalize_listing(draft(), hint(brand="Levi's", size="M", condition=Condition.GOOD), specs(),
                           price_suggestion(method="hint", price_eur=20.0), [], make_settings(tmp_path))
    assert "price" not in out.needs_review


def test_missing_size_condition_and_weak_brand_flagged(tmp_path):
    out = finalize_listing(draft(), hint(), specs(brand=sv("Levi's", confidence="medium")),
                           price_suggestion(), [], make_settings(tmp_path))
    assert out.brand == "Levi's" and out.size is None
    assert set(out.needs_review) == {"price", "condition", "brand", "size"}


def test_high_confidence_spec_brand_not_flagged(tmp_path):
    out = finalize_listing(draft(), hint(size="M"), specs(brand=sv("Levi's", confidence="high")),
                           price_suggestion(), [], make_settings(tmp_path))
    assert "brand" not in out.needs_review


def test_low_match_flags_category(tmp_path):
    out = finalize_listing(draft(), hint(), specs(match="low"), price_suggestion(), [], make_settings(tmp_path))
    assert "category" in out.needs_review


def test_title_cut_when_limit_set(tmp_path):
    out = finalize_listing(draft(title="Calças Levi's 501 originais azuis"), hint(), specs(), price_suggestion(), [],
                           make_settings(tmp_path, title_max_chars=20))
    assert len(out.title) <= 20 and out.title == "Calças Levi's 501" and "title" in out.needs_review


def test_sources_are_unique_spec_urls(tmp_path):
    s = specs(brand=sv("Levi's", url="https://a"),
              attributes=[Attribute(name="fit", value=sv("slim", url="https://a"))],
              second_hand_prices_eur=[sv("15", url="https://b"), sv("90", source="inferred", url=None)])
    out = finalize_listing(draft(), hint(), s, price_suggestion(), [], make_settings(tmp_path))
    assert out.sources == ["https://real", "https://a", "https://b"]


def test_render_order_and_review_block():
    txt = render_listing_txt(listing(needs_review=["price"], price_eur=None),
                             warnings=["comparables skipped: search cap reached"])
    assert txt.startswith("CHECK BEFORE POSTING:") and "set manually" in txt
    assert "- price" in txt and "- comparables skipped: search cap reached" in txt
    labels = [l for l in txt.splitlines() if l in {"TITLE", "DESCRIPTION", "CATEGORY", "PRICE (EUR)", "SOURCES"}]
    assert labels == ["TITLE", "DESCRIPTION", "CATEGORY", "PRICE (EUR)", "SOURCES"]
    assert "Homem > Calças" in txt and "#" not in txt and "**" not in txt


def test_render_without_review_block():
    txt = render_listing_txt(listing(material=None, colours=[]), warnings=[])
    lines = txt.splitlines()
    assert lines[0] == "TITLE"
    assert lines[lines.index("MATERIAL") + 1] == "-" and lines[lines.index("COLOURS") + 1] == "-"
    assert lines[lines.index("PRICE (EUR)") + 1] == "13" and lines[lines.index("PRICE (EUR)") + 2] == "median"


def test_draft_listing_prompt(tmp_path):
    cover = write_photo(tmp_path / "in" / "IMG_1.jpg")
    other = write_photo(tmp_path / "in" / "IMG_2.jpg")
    llm = FakeLLM(parse_results={ListingDraft: draft()})
    result = draft_listing([cover, other], hint(notes="small stain on sleeve"), desc(), specs(), llm,
                           make_settings(tmp_path))
    call = llm.calls[0]
    text = "".join(b["text"] for b in call["content"] if b["type"] == "text")
    assert result.title == "Calças Levi's 501" and call["stage"] == "listing" and call["effort"] == "medium"
    assert sum(b["type"] == "image" for b in call["content"]) == 1
    assert "pt-PT" in call["system"] and "price" in call["system"].lower()
    assert "small stain on sleeve" in text and "faded knees" in text
