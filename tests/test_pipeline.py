import anthropic
import pytest

from fakes import FakeRemover, api_error, connection_error, make_settings, write_photo
from test_pipeline_build import fake_llm
from vinted_listing import pipeline
from vinted_listing.imaging import photos_key
from vinted_listing.pipeline import Deps, fail_empty, is_transient, process_product, unique_dest
from vinted_listing.scanner import ProductBatch, snapshot_hash, snapshot_of
from vinted_listing.pipeline import find_hint_file, list_photos
from vinted_listing.store import StateStore
from vinted_listing.usage import BudgetGuard, UsageLedger


@pytest.fixture(autouse=True)
def no_move_delays(monkeypatch):
    monkeypatch.setattr(pipeline, "MOVE_BASE_DELAY", 0.0)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def store(settings):
    return StateStore(settings.db_path)


def deps_for(settings, llm=None, remover=None):
    ledger = UsageLedger(settings.db_path, settings)
    return Deps(llm=llm or fake_llm(), remover=remover or FakeRemover(), ledger=ledger,
                budget=BudgetGuard(ledger, settings))


def make_item(settings, name, photos=1, hint=None, extra=None):
    folder = settings.inbox / name
    for i in range(1, photos + 1):
        write_photo(folder / f"IMG_{i}.jpg")
    if hint is not None:
        (folder / "hint.txt").write_text(hint, encoding="utf-8")
    for fname, content in (extra or {}).items():
        (folder / fname).write_bytes(content)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def batch_of(folder):
    hint_file, warnings = find_hint_file(folder)
    return ProductBatch(folder=folder, photos=list_photos(folder), hint_file=hint_file, warnings=warnings,
                        snapshot_hash=snapshot_hash(snapshot_of(folder)))


def test_success_layout(settings, store):
    folder = make_item(settings, "levis-501", photos=2, hint="size: M")
    r = process_product(batch_of(folder), settings, deps_for(settings), store)
    d = settings.ready / "levis-501"
    assert r.status == "ready" and r.out_dir == d
    assert {p.name for p in d.iterdir()} == {"01.jpg", "02.jpg", "listing.txt", "listing.json", "originals"}
    assert {p.name for p in (d / "originals").iterdir()} == {"IMG_1.jpg", "IMG_2.jpg", "hint.txt"}
    assert not folder.exists() and not list(settings.ready.glob("*.partial"))


def test_permanent_failure_moves_to_failed_with_step(settings, store):
    folder = make_item(settings, "toy")
    r = process_product(batch_of(folder), settings, deps_for(settings, fake_llm(raise_on="research")), store)
    failed = settings.failed / "toy"
    err = (failed / "error.txt").read_text(encoding="utf-8")
    assert r.status == "failed" and r.failed_step == "research" and "step: research" in err
    assert "RuntimeError" in err and len(list(failed.glob("*.jpg"))) == 1  # originals preserved
    assert not folder.exists()


def test_failure_error_txt_lists_batch_warnings(settings, store):
    folder = make_item(settings, "toy", extra={"a.txt": b"x", "b.txt": b"y"})
    process_product(batch_of(folder), settings, deps_for(settings, fake_llm(raise_on="describe")), store)
    assert "several .txt files" in (settings.failed / "toy" / "error.txt").read_text(encoding="utf-8")


def test_transient_error_stays_in_inbox_then_fails_after_three(settings, store):
    folder = make_item(settings, "toy")
    batch = batch_of(folder)
    exc = api_error(anthropic.RateLimitError, 429)
    statuses = [process_product(batch, settings, deps_for(settings, fake_llm(raise_on="describe", raise_exc=exc)),
                                store).status for _ in range(4)]
    assert statuses == ["retry", "retry", "retry", "failed"]
    assert (settings.failed / "toy" / "error.txt").exists()


def test_retry_reuses_cache_and_success_deletes_it(settings, store):
    folder = make_item(settings, "levis", photos=2, hint="size: M")
    batch = batch_of(folder)
    cache = settings.preprocessed_dir / f"levis-{photos_key(batch.photos, settings)}"
    remover = FakeRemover()
    first = process_product(batch, settings, deps_for(settings, fake_llm(raise_on="research",
                                                                         raise_exc=connection_error()), remover), store)
    assert first.status == "retry" and folder.exists() and cache.exists()
    second = process_product(batch, settings, deps_for(settings, remover=remover), store)
    assert second.status == "ready" and remover.calls == 2 and not cache.exists()


def test_failed_move_is_stuck_not_reprocessed(settings, store, monkeypatch):
    folder = make_item(settings, "levis", hint="size: M")
    batch = batch_of(folder)

    def locked(src, dst, **kw):
        raise PermissionError("file is in use by Google Drive")

    monkeypatch.setattr(pipeline, "safe_move", locked)
    llm = fake_llm()
    assert process_product(batch, settings, deps_for(settings, llm), store).status == "stuck"
    calls = len(llm.calls)
    again = process_product(batch, settings, deps_for(settings, llm), store)
    assert again.status == "stuck" and len(llm.calls) == calls
    assert store.stuck() == [str(folder)]


def test_partial_move_failure_is_not_reprocessed_under_new_snapshot(settings, store, monkeypatch):
    folder = make_item(settings, "levis", photos=3, hint="size: M")
    real_move = pipeline.safe_move
    moves = []

    def lock_after_first(src, dst, **kw):
        moves.append(src)
        if len(moves) > 1:
            raise PermissionError("locked")
        return real_move(src, dst, **kw)

    monkeypatch.setattr(pipeline, "safe_move", lock_after_first)
    llm = fake_llm()
    assert process_product(batch_of(folder), settings, deps_for(settings, llm), store).status == "stuck"
    calls = len(llm.calls)
    leftover = batch_of(folder)  # one original already left: new snapshot hash
    assert process_product(leftover, settings, deps_for(settings, llm), store).status == "stuck"
    assert len(llm.calls) == calls


def test_stuck_record_clears_once_folder_leaves_inbox(settings, store):
    gone = settings.inbox / "handled"
    store.record(str(gone), "h", "ready", moved=False)
    present = make_item(settings, "still-there")
    store.record(str(present), "h", "failed", moved=False)
    assert store.clear_resolved() == 1
    assert store.stuck() == [str(present)]


def test_retry_after_moving_back_from_failed(settings, store):
    folder = make_item(settings, "toy")
    batch = batch_of(folder)
    process_product(batch, settings, deps_for(settings, fake_llm(raise_on="research")), store)
    failed = settings.failed / "toy"
    failed.rename(folder)  # the user moves it back to inbox to retry
    r = process_product(batch_of(folder), settings, deps_for(settings), store)
    assert r.status == "ready" and (settings.ready / "toy" / "originals" / "IMG_1.jpg").exists()


def test_budget_deferred_leaves_inbox_untouched(tmp_path):
    settings = make_settings(tmp_path, monthly_budget_usd=0.0)
    store = StateStore(settings.db_path)
    folder = make_item(settings, "toy")
    before = sorted(p.name for p in folder.iterdir())
    r = process_product(batch_of(folder), settings, deps_for(settings), store)
    assert r.status == "deferred" and "monthly budget" in r.message
    assert sorted(p.name for p in folder.iterdir()) == before and store.get(str(folder), batch_of(folder).snapshot_hash) is None


def test_name_collision_gets_suffix(settings, store):
    (settings.ready / "levis-501").mkdir(parents=True)
    folder = make_item(settings, "levis-501", hint="size: M")
    r = process_product(batch_of(folder), settings, deps_for(settings), store)
    assert r.out_dir == settings.ready / "levis-501-2" and (r.out_dir / "listing.txt").exists()


def test_unique_dest_counts_partial(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a-2.partial").mkdir()
    assert unique_dest(tmp_path, "a") == tmp_path / "a-3"
    assert unique_dest(tmp_path, "b") == tmp_path / "b"


def test_fail_empty(settings, store):
    folder = settings.inbox / "clip"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"x")
    r = fail_empty(folder, settings, store)
    assert r.status == "failed" and r.failed_step == "photos"
    assert (settings.failed / "clip" / "error.txt").read_text(encoding="utf-8").strip() == "no photos"
    assert (settings.failed / "clip" / "clip.mp4").exists() and not folder.exists()


def test_safe_move_retries_then_succeeds(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.txt", tmp_path / "b.txt"
    src.write_text("x")
    real_move = pipeline.shutil.move
    attempts = []

    def flaky(s, d):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("locked")
        return real_move(s, d)

    monkeypatch.setattr(pipeline.shutil, "move", flaky)
    pipeline.safe_move(src, dst, base_delay=0)
    assert dst.read_text() == "x" and len(attempts) == 3


def test_safe_move_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "move", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        pipeline.safe_move(tmp_path / "a", tmp_path / "b", attempts=3, base_delay=0)


@pytest.mark.parametrize("exc, expected", [
    (api_error(anthropic.RateLimitError, 429), True),
    (api_error(anthropic.InternalServerError, 500), True),
    (api_error(anthropic.APIStatusError, 529), True),
    (connection_error(), True),
    (api_error(anthropic.BadRequestError, 400), False),
    (api_error(anthropic.AuthenticationError, 401), False),
    (RuntimeError("boom"), False),
])
def test_is_transient(exc, expected):
    assert is_transient(exc) is expected
