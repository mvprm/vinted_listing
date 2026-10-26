"""Data models shared by the pipeline. No length constraints: LLM schemas stay simple."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel

Source = Literal["hint", "photo", "web", "inferred"]
Confidence = Literal["high", "medium", "low"]


class Condition(str, Enum):
    NEW_WITH_TAGS = "New with tags"
    NEW_WITHOUT_TAGS = "New without tags"
    VERY_GOOD = "Very good"
    GOOD = "Good"
    SATISFACTORY = "Satisfactory"


class ParcelSize(str, Enum):
    SMALL = "Small"
    MEDIUM = "Medium"
    LARGE = "Large"


class ProductHint(BaseModel):
    free_text: str
    brand: str | None = None
    model: str | None = None
    size: str | None = None
    composition: str | None = None
    colour: str | None = None
    category: str | None = None
    notes: str | None = None
    condition: Condition | None = None
    price_eur: float | None = None
    retail_price_eur: float | None = None
    research: bool | None = None
    force_image_match: bool = False
    warnings: list[str] = []


class VisualDescription(BaseModel):
    item_type: str
    colours: list[str]
    visible_text: list[str]
    pattern: str | None
    notable_features: list[str]
    condition_notes: list[str]


class SourcedValue(BaseModel):
    value: str
    source: Source
    source_url: str | None
    confidence: Confidence


class Attribute(BaseModel):
    name: str
    value: SourcedValue


class ProductSpecs(BaseModel):
    product_name: SourcedValue
    brand: SourcedValue | None
    model: SourcedValue | None
    attributes: list[Attribute]
    retail_price_eur: SourcedValue | None
    second_hand_prices_eur: list[SourcedValue]
    match_confidence: Confidence


class ResearchResult(BaseModel):
    text: str
    cited_urls: list[str]
    retrieved_urls: list[str]
    searches: int
    fetches: int


class PriceSuggestion(BaseModel):
    price_eur: float | None
    method: Literal["hint", "comparables", "retail", "none"]
    rationale: str


class ListingDraft(BaseModel):
    title: str
    description: str
    category_path: list[str]
    material: str | None
    condition: Condition
    colours: list[str]
    parcel_size: ParcelSize


class Listing(ListingDraft):
    brand: str | None
    size: str | None
    price_eur: float | None
    price_rationale: str
    needs_review: list[str]
    sources: list[str]
