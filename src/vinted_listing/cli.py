"""Command line: run one folder, evaluate a golden set, compare evaluations, show costs."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from vinted_listing.config import MODEL_PROFILES, Settings, load_settings, with_profile
from vinted_listing.evaluation import compare_reports, run_eval
from vinted_listing.imaging import prune_cache
from vinted_listing.pipeline import (
    BudgetDeferred,
    Deps,
    ProductResult,
    StepError,
    build_package,
    fail_empty,
    find_hint_file,
    list_photos,
    process_product,
)
from vinted_listing.scanner import InboxScanner
from vinted_listing.status import write_status
from vinted_listing.store import StateStore
from vinted_listing.usage import BudgetGuard, UsageLedger

EXIT_FAILED, EXIT_CONFIG, EXIT_DEFERRED = 1, 2, 3
EXIT_ALREADY_RUNNING = 3
PRUNE_EVERY_SECONDS = 24 * 3600

log = logging.getLogger("vinted_listing.watch")


class LockHeld(Exception):
    pass


@contextmanager
def acquire_lock(path: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock on `path`, so only one watcher runs at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockHeld(str(path)) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def run_cycle(scanner: InboxScanner, settings: Settings, deps: Deps, store: StateStore) -> list[ProductResult]:
    store.clear_resolved()
    scan = scanner.scan()
    results: list[ProductResult] = []
    for batch in scan.ready:
        result = process_product(batch, settings, deps, store)
        results.append(result)
        if result.status not in ("deferred", "retry"):  # those keep their snapshot and come back next cycle
            scanner.forget(batch.folder)
    for folder in scan.empty:
        results.append(fail_empty(folder, settings, store))
        scanner.forget(folder)
    for loose in scan.loose_files:
        log.warning("loose file in inbox (put it in an item folder): %s", loose.name)
    try:
        write_status(settings, scan, results, deps.ledger, store, datetime.now())
    except OSError as exc:
        log.error("could not write status.txt: %s", exc)
    return results


def _drive_ready(settings: Settings) -> bool:
    """Create inbox/ready/failed; False while the Drive folder isn't reachable (e.g. not mounted yet at logon)."""
    try:
        for folder in (settings.inbox, settings.ready, settings.failed):
            folder.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def watch(settings: Settings, deps: Deps, max_cycles: int | None = None,
          sleep: Callable[[float], None] = time.sleep) -> None:
    store = StateStore(settings.db_path)
    scanner = InboxScanner(settings.inbox, settings.quiet_seconds, settings.hint_grace_seconds)
    last_prune: float | None = None
    cycles = 0
    waiting_for_drive = False
    log.info("watching %s every %ss", settings.inbox, settings.poll_seconds)
    while max_cycles is None or cycles < max_cycles:
        if last_prune is None or time.monotonic() - last_prune >= PRUNE_EVERY_SECONDS:
            removed = prune_cache(settings.preprocessed_dir)
            last_prune = time.monotonic()
            if removed:
                log.info("pruned %d old photo cache folders", removed)
        if not _drive_ready(settings):
            if not waiting_for_drive:
                log.warning("Drive folder %s not reachable yet; waiting", settings.drive_root)
            waiting_for_drive = True
        else:
            if waiting_for_drive:
                log.info("Drive folder %s is reachable again", settings.drive_root)
            waiting_for_drive = False
            try:
                run_cycle(scanner, settings, deps, store)
            except Exception:  # Drive offline, file locked...: log it and try again next cycle
                log.exception("watch cycle failed; retrying in %ss", settings.poll_seconds)
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            sleep(settings.poll_seconds)


def build_deps(settings: Settings) -> Deps:
    from vinted_listing.imaging import RembgRemover
    from vinted_listing.llm import ClaudeClient

    ledger = UsageLedger(settings.db_path, settings)
    return Deps(llm=ClaudeClient(settings, ledger), remover=RembgRemover(settings.rembg_model), ledger=ledger,
                budget=BudgetGuard(ledger, settings))


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_vinted_listing", False) for h in root.handlers):
        return
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(settings.log_dir / "vinted-listing.log", maxBytes=1_000_000, backupCount=3,
                                       encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(fmt)
        handler._vinted_listing = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vinted_listing", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build a package from one folder (the folder is not changed)")
    run.add_argument("folder", type=Path)
    run.add_argument("--out", type=Path, help="output folder (default: <state_dir>/runs/<name>)")
    ev = sub.add_parser("eval", help="run every item of a golden set and write report.csv")
    ev.add_argument("golden_dir", type=Path)
    ev.add_argument("--profile", choices=sorted(MODEL_PROFILES), help="use this model profile for every stage")
    cmp = sub.add_parser("eval-compare", help="compare two eval reports side by side")
    cmp.add_argument("report_a", type=Path)
    cmp.add_argument("report_b", type=Path)
    sub.add_parser("costs", help="month-to-date API cost and remaining budget")
    sub.add_parser("watch", help="watch the Drive inbox and build packages as folders finish uploading")
    return parser


def _run(settings: Settings, folder: Path, out: Path | None) -> int:
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return EXIT_FAILED
    photos = list_photos(folder)
    if not photos:
        print(f"no photos in {folder}", file=sys.stderr)
        return EXIT_FAILED
    hint_file, hint_warnings = find_hint_file(folder)
    out_dir = out or settings.runs_dir / folder.name
    try:
        result = build_package(folder.name, photos, hint_file, out_dir, settings, build_deps(settings),
                               extra_warnings=hint_warnings)
    except BudgetDeferred as exc:
        print(f"not started: {exc.reason}", file=sys.stderr)
        return EXIT_DEFERRED
    except StepError as exc:
        print(f"failed at step '{exc.step}': {type(exc.cause).__name__}: {exc.cause}", file=sys.stderr)
        return EXIT_FAILED
    print(f"{out_dir / 'listing.txt'} (cost ${result.cost_usd:.3f})")
    return 0


def _costs(settings: Settings) -> int:
    summary = UsageLedger(settings.db_path, settings).month_summary()
    approx = " (approximate: includes fallback or unpriced calls)" if summary["approximate"] else ""
    print(f"Month to date: ${summary['cost_usd']:.2f} of ${summary['budget_usd']:.2f}, "
          f"remaining ${summary['remaining_usd']:.2f}{approx}")
    print(f"Items: {summary['items']}  API calls: {summary['calls']}  "
          f"web searches: {summary['searches']}  page fetches: {summary['fetches']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.command == "costs":
        return _costs(settings)
    if args.command == "eval-compare":
        text = compare_reports(args.report_a, args.report_b)
        (args.report_b.parent / "compare.txt").write_text(text, encoding="utf-8")
        print(text)
        return 0

    setup_logging(settings)
    if args.command == "watch":
        try:
            with acquire_lock(settings.state_dir / "watch.lock"):
                watch(settings, build_deps(settings))
        except LockHeld:
            print("already running: another 'watch' holds the lock", file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        except KeyboardInterrupt:
            log.info("stopped")
        except Exception:  # under pythonw there is no console: the log file is the only trace
            log.exception("watch stopped by an unexpected error")
            return EXIT_FAILED
        return 0
    if args.command == "run":
        return _run(settings, args.folder, args.out)
    if args.profile:
        settings = with_profile(settings, args.profile)
    report = run_eval(args.golden_dir, settings, build_deps(settings))
    print(report)
    return 0
