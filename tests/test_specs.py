from fakes import FakeLLM, make_settings, write_photo
from vinted_listing.models import (
    Attribute,
    ProductHint,
    ProductSpecs,
    ResearchResult,
    SourcedValue,
    VisualDescription,
)
from vinted_listing.specs import (
    enforce_sources,
    needs_web_research,
    parse_specs,
    research_comparables,
    research_specs,
    specs_from_hint,
)


def hint(**kw):
    return ProductHint(free_text=kw.pop("free_text", "levis 501"), **kw)


def desc(**kw):
    base = dict(item_type="jeans", colours=["blue"], visible_text=[], pattern=None, notable_features=[],
                condition_notes=[])
    return VisualDescription(**{**base, **kw})


def sv(value, source="web", url=None, confidence="high"):
    return SourcedValue(value=value, source=source, source_url=url, confidence=confidence)


def specs_with(**kw):
    base = dict(product_name=sv("Levi's 501", source="hint"), brand=None, model=None, attributes=[],
                retail_price_eur=None, second_hand_prices_eur=[], match_confidence="high")
    return ProductSpecs(**{**base, **kw})


def research(text="notes", cited=(), retrieved=(), searches=0, fetches=0):
    return ResearchResult(text=text, cited_urls=list(cited), retrieved_urls=list(retrieved), searches=searches,
                          fetches=fetches)


def test_invented_url_is_demoted_but_hint_values_are_not():
    specs = specs_with(brand=sv("Levi's", source="hint"),
                       model=sv("501", url="https://made-up"),
                       attributes=[Attribute(name="material", value=sv("cotton", url="https://real"))])
    clean, demoted = enforce_sources(specs, ["https://real"])
    assert clean.brand.confidence == "high" and clean.model.source == "inferred" and clean.model.confidence == "low"
    assert clean.model.source_url is None
    assert clean.attributes[0].value.confidence == "high" and demoted == ["model"]


def test_enforce_sources_names_attributes_and_comparables():
    specs = specs_with(attributes=[Attribute(name="fit", value=sv("slim", url="https://x"))],
                       second_hand_prices_eur=[sv("15", url="https://real"), sv("90", url=None)],
                       retail_price_eur=sv("99.95", url="https://real"))
    clean, demoted = enforce_sources(specs, ["https://real"])
    assert demoted == ["attributes.fit", "second_hand_prices_eur[1]"]
    assert clean.retail_price_eur.confidence == "high" and clean.second_hand_prices_eur[0].source == "web"


def test_needs_web_research_rules():
    assert needs_web_research(hint(brand="Levi's", model="501", price_eur=20.0)) is False
    assert needs_web_research(hint(brand="Levi's", model="501")) is True  # no price -> comparables needed
    assert needs_web_research(hint(brand="Levi's", model="501", price_eur=20.0, research=True)) is True
    assert needs_web_research(hint(research=False)) is False


def test_specs_from_hint_marks_hint_sources():
    s = specs_from_hint(hint(brand="Levi's", model="501", composition="100% cotton", colour="indigo",
                             retail_price_eur=99.95), desc(item_type="jeans"))
    assert s.brand.source == "hint" and s.product_name.value == "Levi's 501 jeans" and s.second_hand_prices_eur == []
    assert {a.name: a.value.value for a in s.attributes} == {"composition": "100% cotton", "colour": "indigo"}
    assert s.retail_price_eur.value == "99.95" and s.match_confidence == "high"


def test_specs_research_targets_brand_site_first(tmp_path):
    cover = write_photo(tmp_path / "in" / "IMG_1.jpg")
    llm = FakeLLM(research_results=[research(searches=1)])
    research_specs([cover], hint(brand="Levi's"), desc(), llm, make_settings(tmp_path))
    call = llm.calls[0]
    assert call["stage"] == "research" and call["max_searches"] == 4 and call["max_fetches"] == 3
    text = "".join(b["text"] for b in call["content"] if b["type"] == "text")
    assert "official website" in call["system"] and "Levi's" in text
    assert sum(b["type"] == "image" for b in call["content"]) == 1


def test_specs_research_uses_brand_seen_in_photos(tmp_path):
    cover = write_photo(tmp_path / "in" / "IMG_1.jpg")
    llm = FakeLLM()
    research_specs([cover], hint(), desc(visible_text=["PATAGONIA"]), llm, make_settings(tmp_path))
    assert "PATAGONIA" in str(llm.calls[0]["content"])


def test_comparables_skipped_when_hint_has_price(tmp_path):
    llm = FakeLLM()
    assert research_comparables(hint(price_eur=15.0), desc(), research(), llm, make_settings(tmp_path)) is None
    assert llm.calls == []


def test_comparables_get_remaining_cap(tmp_path):
    llm = FakeLLM(research_results=[research(text="comps")])
    s = make_settings(tmp_path, max_searches_per_item=6, max_fetches_per_item=4)
    research_comparables(hint(), desc(), research(searches=5, fetches=3), llm, s)
    call = llm.calls[0]
    assert call["stage"] == "comparables" and call["max_searches"] == 1 and call["max_fetches"] == 1
    assert "second-hand" in call["system"]


def test_comparables_skipped_with_warning_when_cap_used(tmp_path):
    llm = FakeLLM()
    warnings = []
    s = make_settings(tmp_path, max_searches_per_item=6)
    assert research_comparables(hint(), desc(), research(searches=6), llm, s, warnings=warnings) is None
    assert llm.calls == [] and warnings == ["comparables skipped: search cap reached"]


def test_parse_receives_all_research_and_urls(tmp_path):
    llm = FakeLLM(parse_results={ProductSpecs: specs_with()})
    parse_specs(hint(), desc(), [research(text="A", retrieved=["https://r"]), research(text="B", cited=["https://c"])],
                llm, make_settings(tmp_path))
    call = llm.calls[0]
    content = str(call["content"])
    assert all(s in content for s in ["A", "B", "https://r", "https://c"])
    assert call["stage"] == "parse" and call["output_type"] is ProductSpecs and 'source="hint"' in call["system"]
