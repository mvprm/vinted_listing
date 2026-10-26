"""Spec research (brand site first), second-hand comparables, parsing into ProductSpecs, source checks."""

from __future__ import annotations

from pathlib import Path

from vinted_listing.config import Settings
from vinted_listing.llm import LLMClient, image_block
from vinted_listing.models import (
    Attribute,
    ProductHint,
    ProductSpecs,
    ResearchResult,
    SourcedValue,
    VisualDescription,
)

SPECS_SEARCHES, SPECS_FETCHES = 4, 3
COMPARABLES_SEARCHES, COMPARABLES_FETCHES = 3, 2
IDENTITY_CHARS = 3000  # how much of the spec research the comparables call sees

SPECS_SYSTEM = (
    "You identify a second-hand product precisely and find its official specifications.\n"
    "When the brand is known: first search the brand's official website for this exact product and fetch its "
    "product page. Use retailers only if no official page exists. Do not use second-hand marketplaces.\n"
    "When the brand is unknown: identify the product from the photo, the description and the seller's notes.\n"
    "Report the exact product/model name, composition and other useful attributes, and the official retail "
    "price in EUR. Cite the page each fact comes from. Say plainly when you are unsure."
)

COMPARABLES_SYSTEM = (
    "You find what this item sells for second-hand. Find 3 to 5 second-hand listings or sold prices in EUR for "
    "the same or a very similar item in similar condition, from second-hand marketplaces and resellers. "
    "Cite each listing and state each price in EUR. Do not invent prices; report fewer if you find fewer."
)

PARSE_SYSTEM = (
    "Turn the research notes into ProductSpecs. Every value carries its source:\n"
    '- a value copied from the seller hint: source="hint", source_url null;\n'
    '- a value read from text visible in the photos: source="photo", source_url null;\n'
    '- a value found in the research: source="web", and source_url must be one of the listed URLs;\n'
    '- anything else: source="inferred".\n'
    "second_hand_prices_eur values are plain numbers in EUR as strings (e.g. \"15.50\"). "
    "match_confidence says how sure you are the research found this exact product."
)


def needs_web_research(hint: ProductHint) -> bool:
    if hint.research is not None:
        return hint.research
    return not (hint.brand and hint.model and hint.price_eur is not None)


def _hint_value(value: str) -> SourcedValue:
    return SourcedValue(value=value, source="hint", source_url=None, confidence="high")


def specs_from_hint(hint: ProductHint, desc: VisualDescription) -> ProductSpecs:
    name = " ".join(part for part in (hint.brand, hint.model, desc.item_type) if part)
    attributes = [Attribute(name=key, value=_hint_value(getattr(hint, key)))
                  for key in ("composition", "colour") if getattr(hint, key)]
    retail = _hint_value(f"{hint.retail_price_eur:g}") if hint.retail_price_eur is not None else None
    return ProductSpecs(
        product_name=_hint_value(name or hint.free_text),
        brand=_hint_value(hint.brand) if hint.brand else None,
        model=_hint_value(hint.model) if hint.model else None,
        attributes=attributes,
        retail_price_eur=retail,
        second_hand_prices_eur=[],
        match_confidence="high",
    )


def _hint_lines(hint: ProductHint) -> str:
    fields = ["brand", "model", "size", "composition", "colour", "category", "condition", "notes"]
    lines = [f"- folder name: {hint.free_text}"]
    for key in fields:
        value = getattr(hint, key)
        if value is not None:
            lines.append(f"- {key}: {value.value if hasattr(value, 'value') else value}")
    if hint.retail_price_eur is not None:
        lines.append(f"- retail price on tag (EUR): {hint.retail_price_eur:g}")
    return "\n".join(lines)


def _desc_lines(desc: VisualDescription) -> str:
    return desc.model_dump_json(indent=2)


def research_specs(
    photos: list[Path], hint: ProductHint, desc: VisualDescription, llm: LLMClient, settings: Settings
) -> ResearchResult:
    brand = hint.brand or (", ".join(desc.visible_text) if desc.visible_text else None)
    brand_line = f"Brand: {brand} (search its official website first)." if brand else "Brand: unknown."
    content = [image_block(p, settings.llm_image_edge) for p in photos[: settings.draft_max_photos]]
    content.append({"type": "text", "text": (
        f"{brand_line}\n\nSeller hint:\n{_hint_lines(hint)}\n\nWhat the photos show:\n{_desc_lines(desc)}"
    )})
    return llm.research(
        SPECS_SYSTEM, content, stage="research", effort=settings.effort_research,
        max_searches=min(SPECS_SEARCHES, settings.max_searches_per_item),
        max_fetches=min(SPECS_FETCHES, settings.max_fetches_per_item),
    )


def research_comparables(
    hint: ProductHint,
    desc: VisualDescription,
    specs_research: ResearchResult,
    llm: LLMClient,
    settings: Settings,
    warnings: list[str] | None = None,
) -> ResearchResult | None:
    if hint.price_eur is not None:
        return None
    searches_left = settings.max_searches_per_item - specs_research.searches
    fetches_left = settings.max_fetches_per_item - specs_research.fetches
    if searches_left <= 0:
        if warnings is not None:
            warnings.append("comparables skipped: search cap reached")
        return None
    condition = hint.condition.value if hint.condition else "see the description"
    content = [{"type": "text", "text": (
        f"Seller hint:\n{_hint_lines(hint)}\n\nCondition: {condition}\n\nWhat the photos show:\n{_desc_lines(desc)}"
        f"\n\nProduct identification so far:\n{specs_research.text[:IDENTITY_CHARS] or '(none)'}"
    )}]
    return llm.research(
        COMPARABLES_SYSTEM, content, stage="comparables", effort=settings.effort_research,
        max_searches=min(COMPARABLES_SEARCHES, searches_left),
        max_fetches=max(0, min(COMPARABLES_FETCHES, fetches_left)),
    )


def parse_specs(
    hint: ProductHint, desc: VisualDescription, results: list[ResearchResult], llm: LLMClient, settings: Settings
) -> ProductSpecs:
    urls = list(dict.fromkeys(u for r in results for u in (*r.retrieved_urls, *r.cited_urls)))
    notes = "\n\n".join(f"Research notes {i}:\n{r.text or '(empty)'}" for i, r in enumerate(results, start=1))
    url_list = "\n".join(f"- {u}" for u in urls) or "(none)"
    text = (f"Seller hint:\n{_hint_lines(hint)}\n\nWhat the photos show:\n{_desc_lines(desc)}\n\n{notes}\n\n"
            f"URLs you may use as source_url:\n{url_list}")
    return llm.parse(ProductSpecs, PARSE_SYSTEM, [{"type": "text", "text": text}],
                     stage="parse", effort=settings.effort_parse)


def _demote(value: SourcedValue) -> SourcedValue:
    return value.model_copy(update={"source": "inferred", "source_url": None, "confidence": "low"})


def enforce_sources(specs: ProductSpecs, allowed_urls: list[str]) -> tuple[ProductSpecs, list[str]]:
    """Demote every web value whose URL was never retrieved or cited; return the demoted field names."""
    allowed = set(allowed_urls)
    demoted: list[str] = []

    def check(value: SourcedValue | None, name: str) -> SourcedValue | None:
        if value is None or value.source != "web" or value.source_url in allowed:
            return value
        demoted.append(name)
        return _demote(value)

    update = {
        "product_name": check(specs.product_name, "product_name"),
        "brand": check(specs.brand, "brand"),
        "model": check(specs.model, "model"),
        "attributes": [a.model_copy(update={"value": check(a.value, f"attributes.{a.name}")})
                       for a in specs.attributes],
        "retail_price_eur": check(specs.retail_price_eur, "retail_price_eur"),
        "second_hand_prices_eur": [check(v, f"second_hand_prices_eur[{i}]")
                                   for i, v in enumerate(specs.second_hand_prices_eur)],
    }
    return specs.model_copy(update=update), demoted
