from vinted_listing.models import Condition, ListingDraft, ParcelSize, ProductHint


def test_condition_values():
    assert [c.value for c in Condition] == ["New with tags", "New without tags", "Very good", "Good", "Satisfactory"]


def test_parcel_size_values():
    assert [p.value for p in ParcelSize] == ["Small", "Medium", "Large"]


def test_product_hint_defaults():
    h = ProductHint(free_text="levis 501")
    assert h.brand is None and h.research is None and h.force_image_match is False and h.warnings == []


def test_listing_draft_parses_enum_values():
    d = ListingDraft.model_validate({"title": "t", "description": "d", "category_path": ["Homem"], "material": None,
                                     "condition": "Very good", "colours": ["blue"], "parcel_size": "Small"})
    assert d.condition is Condition.VERY_GOOD and d.parcel_size is ParcelSize.SMALL
