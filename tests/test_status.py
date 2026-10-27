from datetime import datetime
from pathlib import Path

from fakes import make_settings, usage
from vinted_listing.pipeline import ProductResult
from vinted_listing.scanner import ScanResult
from vinted_listing.status import write_status
from vinted_listing.store import StateStore
from vinted_listing.usage import UsageLedger

NOW = datetime(2026, 10, 27, 14, 5)


def test_status_file_lists_waiting_deferred_and_budget(tmp_path):
    settings = make_settings(tmp_path, monthly_budget_usd=20.0)
    settings.drive_root.mkdir(parents=True)
    ledger = UsageLedger(settings.db_path, settings)
    with ledger.item_scope("levis"):
        ledger.record("research", "claude-opus-5-5", usage(input_tokens=800_000, output_tokens=0), now=NOW)
    store = StateStore(settings.db_path)
    stuck_folder = settings.inbox / "stuck-item"
    stuck_folder.mkdir(parents=True)
    store.record(str(stuck_folder), "h1", "ready", moved=False)
    store.record(str(settings.inbox / "levis"), "h2", "ready", moved=True)
    scan = ScanResult(waiting=[(settings.inbox / "zara", "uploading"), (settings.inbox / "toy", "waiting for hint.txt")])
    results = [ProductResult(name="dress", status="deferred", out_dir=None, failed_step=None,
                             message="monthly budget reached ($20.13 of $20.00)")]

    write_status(settings, scan, results, ledger, store, NOW)

    text = settings.status_file.read_text(encoding="utf-8")
    assert "2026-10-27 14:05" in text
    assert "- zara: uploading" in text and "- toy: waiting for hint.txt" in text
    assert "- dress: monthly budget reached ($20.13 of $20.00)" in text
    assert "move this folder out of inbox manually" in text and "stuck-item" in text
    assert "$3.20 of $20.00" in text
    assert "levis: ready" in text


def test_status_hides_stuck_folders_already_gone(tmp_path):
    settings = make_settings(tmp_path)
    settings.drive_root.mkdir(parents=True)
    store = StateStore(settings.db_path)
    store.record(str(settings.inbox / "handled"), "h", "failed", moved=False)  # folder no longer exists
    write_status(settings, ScanResult(), [], UsageLedger(settings.db_path, settings), store, NOW)
    text = settings.status_file.read_text(encoding="utf-8")
    assert "handled" not in text.split("Last results")[0]
    assert not list(Path(settings.drive_root).glob("*.tmp"))


def test_store_attempts_and_recent(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.record("f", "h", "transient", moved=False)
    store.record("f", "h", "transient", moved=False)
    assert store.get("f", "h").attempts == 2 and store.get("f", "h").outcome == "transient"
    store.record("f", "h", "ready", moved=True)
    state = store.get("f", "h")
    assert state.outcome == "ready" and state.moved and state.attempts == 2
    assert store.get("f", "other") is None
    assert [s.outcome for s in store.recent(10)] == ["ready"]
