import base64
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pillow_heif
import pytest
from PIL import Image

from fakes import make_settings, usage
from vinted_listing.llm import VINTED_DOMAINS, ClaudeClient, LLMError, LLMRefusal, extract_urls, image_block
from vinted_listing.models import ListingDraft, VisualDescription
from vinted_listing.usage import UsageLedger


def text_block(text="t", citations=None):
    return SimpleNamespace(type="text", text=text, citations=citations)


def search_result(urls):
    return SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url=u) for u in urls])


def search_error(code):
    return SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(error_code=code))


def fetch_result(url):
    return SimpleNamespace(type="web_fetch_tool_result", content=SimpleNamespace(type="web_fetch_result", url=url))


def response(stop_reason="end_turn", content=None, searches=0, fetches=0, parsed=None):
    return SimpleNamespace(stop_reason=stop_reason, content=content or [text_block("done")],
                           usage=usage(searches=searches, fetches=fetches), parsed_output=parsed,
                           stop_details=None)


def done(**kw):
    return response(**kw)


def paused(searches=0, fetches=0):
    return response(stop_reason="pause_turn", content=[text_block("partial")], searches=searches, fetches=fetches)


class FakeMessagesAPI:
    """Stands in for `client.beta.messages`: returns canned responses in order, records kwargs."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.kwargs = []

    @property
    def calls(self):
        return len(self.kwargs)

    def _next(self, kw):
        self.kwargs.append(kw)
        return self.responses.pop(0)

    def parse(self, **kw):
        return self._next(kw)

    def create(self, **kw):
        return self._next(kw)

    @contextmanager
    def stream(self, **kw):
        resp = self._next(kw)
        yield SimpleNamespace(get_final_message=lambda: resp)


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "s.db", make_settings(tmp_path))


def a_description():
    return VisualDescription(item_type="jeans", colours=["blue"], visible_text=[], pattern=None,
                             notable_features=[], condition_notes=[])


def test_image_block_downscales_and_reads_heic(tmp_path):
    pillow_heif.register_heif_opener()
    p = tmp_path / "big.heic"
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(p, format="HEIF")
    blk = image_block(p, max_edge=1092)
    img = Image.open(io.BytesIO(base64.b64decode(blk["source"]["data"])))
    assert blk["type"] == "image" and blk["source"]["media_type"] == "image/jpeg" and max(img.size) == 1092


def test_extract_urls_cited_and_retrieved():
    blocks = [search_result(["https://a", "https://b"]), search_error("max_uses_exceeded"), fetch_result("https://c"),
              text_block(citations=[{"url": "https://a"}]), text_block(citations=None)]
    assert extract_urls(blocks) == (["https://a"], ["https://a", "https://b", "https://c"])


def test_extract_urls_reads_citation_objects():
    blocks = [text_block(citations=[SimpleNamespace(url="https://x"), SimpleNamespace(url=None)])]
    assert extract_urls(blocks) == (["https://x"], [])


def test_fallback_sent_only_for_models_that_support_it(tmp_path, ledger):
    api = FakeMessagesAPI([done(parsed=a_description()), done(parsed="draft")])
    c = ClaudeClient(make_settings(tmp_path), ledger, api=api)  # mixed profile
    c.parse(VisualDescription, "s", [], stage="describe", effort="low")
    c.parse(ListingDraft, "s", [], stage="listing", effort="medium")
    assert api.kwargs[0]["model"] == "claude-haiku-5-5" and "fallbacks" not in api.kwargs[0]
    assert "betas" not in api.kwargs[0]
    assert api.kwargs[1]["model"] == "claude-sonnet-5-5" and api.kwargs[1]["fallbacks"] == "default"
    assert api.kwargs[1]["betas"] == ["server-side-fallback-2026-07-01"]
    assert api.kwargs[0]["output_config"]["effort"] == "low"
    assert api.kwargs[0]["output_format"] is VisualDescription


def test_parse_records_usage_in_ledger(tmp_path, ledger):
    api = FakeMessagesAPI([done(parsed=a_description())])
    with ledger.item_scope("levis"):
        ClaudeClient(make_settings(tmp_path), ledger, api=api).parse(
            VisualDescription, "s", [], stage="describe", effort="low")
    assert ledger.month_summary()["calls"] == 1


def test_refusal_raises(tmp_path, ledger):
    api = FakeMessagesAPI([done(stop_reason="refusal")])
    with pytest.raises(LLMRefusal):
        ClaudeClient(make_settings(tmp_path), ledger, api=api).parse(
            VisualDescription, "s", [], stage="describe", effort="low")


def test_missing_parsed_output_raises(tmp_path, ledger):
    api = FakeMessagesAPI([done(stop_reason="max_tokens", parsed=None)])
    with pytest.raises(LLMError, match="max_tokens"):
        ClaudeClient(make_settings(tmp_path), ledger, api=api).parse(
            VisualDescription, "s", [], stage="describe", effort="low")


def test_research_tools_and_result(tmp_path, ledger):
    content = [search_result(["https://a"]), text_block("Levi's 501", citations=[{"url": "https://a"}])]
    api = FakeMessagesAPI([done(content=content, searches=1)])
    r = ClaudeClient(make_settings(tmp_path), ledger, api=api).research(
        "s", [{"type": "text", "text": "q"}], stage="research", effort="medium", max_searches=4, max_fetches=3)
    tools = {t["name"]: t for t in api.kwargs[0]["tools"]}
    assert tools["web_search"] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}
    assert tools["web_fetch"]["type"] == "web_fetch_20260209" and tools["web_fetch"]["max_uses"] == 3
    assert tools["web_fetch"]["citations"] == {"enabled": True} and tools["web_fetch"]["max_content_tokens"] == 8000
    assert set(VINTED_DOMAINS) == set(tools["web_fetch"]["blocked_domains"]) and "vinted.pt" in VINTED_DOMAINS
    assert (r.text, r.cited_urls, r.retrieved_urls, r.searches, r.fetches) == ("Levi's 501", ["https://a"], ["https://a"], 1, 0)


def test_research_without_fetch_budget_omits_fetch_tool(tmp_path, ledger):
    api = FakeMessagesAPI([done()])
    ClaudeClient(make_settings(tmp_path), ledger, api=api).research(
        "s", [], stage="comparables", effort="medium", max_searches=2, max_fetches=0)
    assert [t["name"] for t in api.kwargs[0]["tools"]] == ["web_search"]


def test_research_resumes_pause_turn_with_assistant_content(tmp_path, ledger):
    api = FakeMessagesAPI([paused(searches=1), done()])
    r = ClaudeClient(make_settings(tmp_path), ledger, api=api).research(
        "s", [{"type": "text", "text": "q"}], stage="research", effort="medium", max_searches=6, max_fetches=4)
    second = api.kwargs[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]  # no extra "Continue" user message
    assert r.text == "partial\ndone" and r.searches == 1


def test_research_stops_resuming_at_search_cap(tmp_path, ledger):
    api = FakeMessagesAPI([paused(searches=4), paused(searches=3), done()])  # cap 6
    r = ClaudeClient(make_settings(tmp_path, max_searches_per_item=6), ledger, api=api).research(
        "s", [], stage="research", effort="medium", max_searches=6, max_fetches=4)
    assert api.calls == 2 and r.searches == 7


def test_research_resumes_at_most_three_times(tmp_path, ledger):
    api = FakeMessagesAPI([paused(), paused(), paused(), paused(), done()])
    ClaudeClient(make_settings(tmp_path), ledger, api=api).research(
        "s", [], stage="research", effort="medium", max_searches=6, max_fetches=4)
    assert api.calls == 4
