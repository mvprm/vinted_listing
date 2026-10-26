from fakes import FakeLLM, make_settings, write_photo
from vinted_listing.describe import describe_item
from vinted_listing.models import VisualDescription


def a_description():
    return VisualDescription(item_type="jeans", colours=["blue"], visible_text=[], pattern=None,
                             notable_features=[], condition_notes=[])


def test_describe_sends_at_most_max_photos_cover_first(tmp_path):
    photos = [write_photo(tmp_path / "in" / f"IMG_{i}.jpg", colour=(i * 20, 0, 0)) for i in range(8)]
    llm = FakeLLM(parse_results={VisualDescription: a_description()})
    result = describe_item(photos, llm, make_settings(tmp_path, describe_max_photos=4))
    call = llm.calls[0]
    assert result.item_type == "jeans"
    assert sum(b["type"] == "image" for b in call["content"]) == 4 and call["stage"] == "describe"
    assert call["content"][0]["type"] == "image" and call["effort"] == "low"
    assert "Never guess a brand" in call["system"]
