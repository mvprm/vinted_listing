"""Describe the item from its original photos (type, colours, visible text, wear)."""

from __future__ import annotations

from pathlib import Path

from vinted_listing.config import Settings
from vinted_listing.llm import LLMClient, image_block
from vinted_listing.models import VisualDescription

DESCRIBE_SYSTEM = (
    "You describe second-hand items from photos for a resale listing. Describe only what is visible. "
    "Transcribe any logo or printed text verbatim into visible_text. Never guess a brand from style. "
    "Note visible wear, stains or damage in condition_notes; leave it empty if you see none. "
    "Use plain English; colours are everyday colour names."
)


def describe_item(photos: list[Path], llm: LLMClient, settings: Settings) -> VisualDescription:
    selected = photos[: settings.describe_max_photos]
    content = [image_block(p, settings.llm_image_edge) for p in selected]
    content.append({"type": "text", "text": f"Describe this item. There are {len(selected)} photos; the first is the cover."})
    return llm.parse(VisualDescription, DESCRIBE_SYSTEM, content, stage="describe", effort=settings.effort_describe)
