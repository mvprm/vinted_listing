"""Draft the listing with Claude, finalize it deterministically, and render plain text for the phone."""

from __future__ import annotations

from pathlib import Path

from vinted_listing.config import Settings
from vinted_listing.llm import LLMClient, image_block
from vinted_listing.models import (
    Listing,
    ListingDraft,
    PriceSuggestion,
    ProductHint,
    ProductSpecs,
    SourcedValue,
    VisualDescription,
)

MAX_COLOURS = 2


def _draft_system(language: str) -> str:
    return (
        f"You write Vinted listings for second-hand items. Write the title and description in {language}. "
        "The title is short and searchable: item type, brand, model and size when known. "
        "The description is honest and friendly, a few short sentences: what the item is, material, fit, "
        "and any wear or damage listed in the notes. Do not invent facts that are not in the input. "
        "Propose a Vinted category path (from broad to specific), up to two colours and the material. "
        "Judge the condition from the photo and the notes. Pick the parcel size for shipping. "
        "Never mention a price or ask buyers to make offers."
    )


def draft_listing(
    photos: list[Path],
    hint: ProductHint,
    desc: VisualDescription,
    specs: ProductSpecs,
    llm: LLMClient,
    settings: Settings,
) -> ListingDraft:
    content = [image_block(p, settings.llm_image_edge) for p in photos[: settings.draft_max_photos]]
    hint_json = hint.model_dump_json(indent=2, exclude={"warnings", "price_eur", "retail_price_eur", "research",
                                                        "force_image_match"}, exclude_none=True)
    content.append({"type": "text", "text": (
        f"Seller hint:\n{hint_json}\n\nWhat the photos show (all photos):\n{desc.model_dump_json(indent=2)}\n\n"
        f"Product specs:\n{specs.model_dump_json(indent=2)}"
    )})
    return llm.parse(ListingDraft, _draft_system(settings.listing_language), content,
                     stage="listing", effort=settings.effort_listing)


def _cut_title(title: str, limit: int | None) -> tuple[str, bool]:
    if limit is None or len(title) <= limit:
        return title, False
    cut = title[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,-–") if " " in title[: limit + 1] else ""
    return (cut or title[:limit]), True


def _spec_values(specs: ProductSpecs) -> list[SourcedValue]:
    values = [specs.product_name, specs.brand, specs.model, specs.retail_price_eur]
    values += [a.value for a in specs.attributes] + list(specs.second_hand_prices_eur)
    return [v for v in values if v is not None]


def finalize_listing(
    draft: ListingDraft,
    hint: ProductHint,
    specs: ProductSpecs,
    price: PriceSuggestion,
    demoted: list[str],
    settings: Settings,
) -> Listing:
    review: list[str] = []
    if price.method != "hint":
        review.append("price")
    if hint.condition is None:
        review.append("condition")

    if hint.brand:
        brand = hint.brand
    else:
        brand = specs.brand.value if specs.brand else None
        if specs.brand is None or specs.brand.confidence != "high":
            review.append("brand")
    if hint.size is None:
        review.append("size")

    review += demoted
    if specs.match_confidence == "low" and not hint.category:
        review.append("category")

    title, was_cut = _cut_title(draft.title, settings.title_max_chars)
    if was_cut:
        review.append("title")

    return Listing(
        title=title,
        description=draft.description,
        category_path=[hint.category] if hint.category else draft.category_path,
        material=hint.composition or draft.material,
        condition=hint.condition or draft.condition,
        colours=([hint.colour] if hint.colour else draft.colours)[:MAX_COLOURS],
        parcel_size=draft.parcel_size,
        brand=brand,
        size=hint.size,
        price_eur=price.price_eur,
        price_rationale=price.rationale,
        needs_review=list(dict.fromkeys(review)),
        sources=list(dict.fromkeys(v.source_url for v in _spec_values(specs) if v.source_url)),
    )


def _price_text(price: float) -> str:
    return f"{price:.0f}" if price == int(price) else f"{price:.2f}"


def render_listing_txt(listing: Listing, warnings: list[str]) -> str:
    lines: list[str] = []
    if listing.needs_review or warnings:
        lines.append("CHECK BEFORE POSTING:")
        lines += [f"- {item}" for item in [*listing.needs_review, *warnings]]
        lines.append("")

    price_value = (f"{_price_text(listing.price_eur)}\n{listing.price_rationale}"
                   if listing.price_eur is not None else f"set manually\n{listing.price_rationale}")
    fields = [
        ("TITLE", listing.title),
        ("DESCRIPTION", listing.description),
        ("CATEGORY", " > ".join(listing.category_path)),
        ("BRAND", listing.brand),
        ("SIZE", listing.size),
        ("CONDITION", listing.condition.value),
        ("COLOURS", ", ".join(listing.colours)),
        ("MATERIAL", listing.material),
        ("PARCEL SIZE", listing.parcel_size.value),
        ("PRICE (EUR)", price_value),
    ]
    for label, value in fields:
        lines += [label, value if value else "-", ""]

    lines.append("SOURCES")
    lines += listing.sources or ["-"]
    return "\n".join(lines) + "\n"
