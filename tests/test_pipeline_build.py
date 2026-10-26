import base64
import io
import json

import pytest
from PIL import Image

from fakes import FakeLLM, FakeRemover, make_settings, write_photo
from vinted_listing.models import (
    Condition,
    ListingDraft,
    ParcelSize,
    ProductSpecs,
    ResearchResult,
    SourcedValue,
    VisualDescription,
)
from vinted_listing.pipeline import (
    BudgetDeferred,
    Deps,
    StepError,
    build_package,
    find_hint_file,
    list_photos,
)
from vinted_listing.usage import BudgetGuard, UsageLedger


def a_description():
    return VisualDescription(item_type="jeans", colours=["blue"], visible_text=[], pattern=None,
                             notable_features=[], condition_notes=[])


def a_specs():
    return ProductSpecs(
        product_name=SourcedValue(value="Levi's 501", source="web", source_url="https://levi.com/501", confidence="high"),
        brand=SourcedValue(value="Levi's", source="web", source_url="https://levi.com/501", confidence="high"),
        model=SourcedValue(value="501", source="web", source_url="https://made-up", confidence="high"),
        attributes=[], retail_price_eur=None,
        second_hand_prices_eur=[SourcedValue(value="15", source="web", source_url="https://olx.pt/1", confidence="high")],
        match_confidence="high")


def a_draft():
    return ListingDraft(title="Calças Levi's 501", description="Jeans clássicas.", category_path=["Homem", "Jeans"],
                        material="denim", condition=Condition.GOOD, colours=["blue"], parcel_size=ParcelSize.SMALL)


def fake_llm(**kw):
    return FakeLLM(
        parse_results={VisualDescription: a_description(), ProductSpecs: a_specs(), ListingDraft: a_draft()},
        research_results=[ResearchResult(text="specs", cited_urls=[], retrieved_urls=["https://levi.com/501"],
                                         searches=2, fetches=1),
                          ResearchResult(text="comps", cited_urls=["https://olx.pt/1"], retrieved_urls=[],
                                         searches=2, fetches=0)],
        **kw)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def deps_for(settings, llm=None, remover=None):
    ledger = UsageLedger(settings.db_path, settings)
    return Deps(llm=llm or fake_llm(), remover=remover or FakeRemover(), ledger=ledger,
                budget=BudgetGuard(ledger, settings))


def photos(tmp_path, n, colour=(120, 80, 60)):
    return [write_photo(tmp_path / "item" / f"IMG_{i}.jpg", colour=colour) for i in range(1, n + 1)]


def hint_file(tmp_path, text):
    p = tmp_path / "item" / "hint.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def stages(llm):
    return [c["stage"] for c in llm.calls]


def test_build_package_writes_outputs(tmp_path, settings):
    r = build_package("levis-501", photos(tmp_path, 2), hint_file(tmp_path, "brand: Levi's\nprice: 20"),
                      tmp_path / "o", settings, deps_for(settings))
    assert {p.name for p in (tmp_path / "o").iterdir()} == {"01.jpg", "02.jpg", "listing.txt", "listing.json"}
    assert r.listing.price_eur == 20 and "price" not in r.listing.needs_review
    assert json.loads((tmp_path / "o" / "listing.json").read_text(encoding="utf-8"))["title"] == "Calças Levi's 501"
    assert (tmp_path / "o" / "listing.txt").read_text(encoding="utf-8").splitlines()[0] == "CHECK BEFORE POSTING:"


def test_full_research_path_demotes_unretrieved_urls(tmp_path, settings):
    llm = fake_llm()
    r = build_package("levis-501", photos(tmp_path, 1), None, tmp_path / "o", settings, deps_for(settings, llm))
    assert stages(llm) == ["describe", "research", "comparables", "parse", "listing"]
    assert "model" in r.listing.needs_review  # https://made-up was never retrieved
    assert r.listing.price_eur == 13.0  # one comparable (15) x 0.85 for Good


def test_comparables_not_called_when_price_in_hint(tmp_path, settings):
    llm = fake_llm()
    build_package("x", photos(tmp_path, 1), hint_file(tmp_path, "brand: Levi's\nprice: 20"), tmp_path / "o",
                  settings, deps_for(settings, llm))
    assert "research" in stages(llm) and "comparables" not in stages(llm)


def test_research_skipped_when_hint_complete(tmp_path, settings):
    llm = fake_llm()
    r = build_package("x", photos(tmp_path, 1), hint_file(tmp_path, "brand: Levi's\nmodel: 501\nprice: 20"),
                      tmp_path / "o", settings, deps_for(settings, llm))
    assert stages(llm) == ["describe", "listing"]
    assert r.listing.brand == "Levi's" and r.listing.sources == []


def test_failure_names_step(tmp_path, settings):
    with pytest.raises(StepError) as e:
        build_package("toy", photos(tmp_path, 1), None, tmp_path / "o", settings,
                      deps_for(settings, fake_llm(raise_on="research")))
    assert e.value.step == "research" and isinstance(e.value.cause, RuntimeError)


def test_budget_blocked_raises_deferred_before_any_llm_call(tmp_path):
    settings = make_settings(tmp_path, monthly_budget_usd=0.0)
    llm, remover = fake_llm(), FakeRemover()
    with pytest.raises(BudgetDeferred, match="monthly budget"):
        build_package("x", photos(tmp_path, 1), None, tmp_path / "o", settings, deps_for(settings, llm, remover))
    assert llm.calls == [] and remover.calls == 0


def test_second_build_reuses_cached_photos(tmp_path, settings):
    remover = FakeRemover()
    items = photos(tmp_path, 2)
    build_package("x", items, None, tmp_path / "o1", settings, deps_for(settings, remover=remover))
    build_package("x", items, None, tmp_path / "o2", settings, deps_for(settings, remover=remover))
    assert remover.calls == 2 and (tmp_path / "o2" / "02.jpg").exists()


def test_llm_steps_receive_originals_not_cache(tmp_path, settings):
    llm = fake_llm()
    build_package("x", photos(tmp_path, 1, colour=(120, 80, 60)), None, tmp_path / "o", settings,
                  deps_for(settings, llm))
    for call in (c for c in llm.calls if c["stage"] in ("describe", "listing")):
        block = next(b for b in call["content"] if b["type"] == "image")
        img = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
        r, g, b = img.getpixel((5, 5))  # left half: processed copy would be the grey background
        assert abs(r - 120) < 10 and abs(g - 80) < 10 and abs(b - 60) < 10


def test_cost_is_this_runs_ledger_total(tmp_path, settings):
    deps = deps_for(settings)
    with deps.ledger.item_scope("x"):  # an earlier run of the same item
        from fakes import usage
        deps.ledger.record("describe", "claude-opus-5-5", usage(input_tokens=1_000_000, output_tokens=0))
    r = build_package("x", photos(tmp_path, 1), None, tmp_path / "o", settings, deps)
    assert r.cost_usd == 0.0  # FakeLLM records nothing; earlier $4 is not this run's


def test_rebuild_into_same_folder_leaves_no_stale_photos(tmp_path, settings):
    out = tmp_path / "o"
    build_package("x", photos(tmp_path, 3), None, out, settings, deps_for(settings))
    single = [write_photo(tmp_path / "item2" / "IMG_1.jpg")]
    build_package("x", single, None, out, settings, deps_for(settings))
    assert sorted(p.name for p in out.iterdir()) == ["01.jpg", "listing.json", "listing.txt"]


def test_extra_warnings_reach_listing_txt(tmp_path, settings):
    r = build_package("x", photos(tmp_path, 1), None, tmp_path / "o", settings, deps_for(settings),
                      extra_warnings=["several .txt files; name the hint hint.txt"])
    assert "several .txt files; name the hint hint.txt" in r.warnings
    assert "- several .txt files" in (tmp_path / "o" / "listing.txt").read_text(encoding="utf-8")


def test_list_photos_and_find_hint(tmp_path):
    folder = tmp_path / "item"
    for name in ("b.JPG", "a.heic", "clip.mp4", "desktop.ini", "error.txt", "Note.txt"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x")
    assert [p.name for p in list_photos(folder)] == ["a.heic", "b.JPG"]
    assert find_hint_file(folder) == (folder / "Note.txt", [])
    (folder / "HINT.txt").write_text("brand: x")
    assert find_hint_file(folder) == (folder / "HINT.txt", [])


def test_find_hint_warns_on_several_txt_and_gdoc(tmp_path):
    folder = tmp_path / "item"
    folder.mkdir()
    (folder / "a.txt").write_text("")
    (folder / "b.txt").write_text("")
    assert find_hint_file(folder) == (None, ["several .txt files; name the hint hint.txt"])
    other = tmp_path / "other"
    other.mkdir()
    (other / "hint.gdoc").write_text("{}")
    assert find_hint_file(other) == (None, ["hint saved as Google Doc; save it as plain .txt"])
