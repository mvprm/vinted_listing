"""Build one item's package: processed photos, listing.txt and listing.json."""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vinted_listing.config import Settings
from vinted_listing.describe import describe_item
from vinted_listing.hints import parse_hint
from vinted_listing.imaging import BackgroundRemover, photos_key, process_photos_cached
from vinted_listing.listing import draft_listing, finalize_listing, render_listing_txt
from vinted_listing.llm import LLMClient
from vinted_listing.models import Listing
from vinted_listing.pricing import suggest_price
from vinted_listing.specs import (
    enforce_sources,
    needs_web_research,
    parse_specs,
    research_comparables,
    research_specs,
    specs_from_hint,
)
from vinted_listing.store import StateStore
from vinted_listing.usage import BudgetGuard, UsageLedger

if TYPE_CHECKING:
    from vinted_listing.scanner import ProductBatch

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
IGNORED_NAMES = {"desktop.ini", "error.txt"}
IGNORED_EXTENSIONS = {".tmp", ".gdoc", ".gsheet", ".gslides", ".lnk"}
PACKAGE_PHOTO = re.compile(r"\d{2,}\.jpg")


@dataclass
class Deps:
    llm: LLMClient
    remover: BackgroundRemover
    ledger: UsageLedger
    budget: BudgetGuard
    detector: object | None = None  # Phase C


class StepError(Exception):
    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(f"{step}: {type(cause).__name__}: {cause}")
        self.step = step
        self.cause = cause


class BudgetDeferred(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class PackageResult:
    listing: Listing
    warnings: list[str]
    cost_usd: float


def is_ignored(path: Path) -> bool:
    name = path.name
    return (name.startswith((".", "~$")) or name.lower() in IGNORED_NAMES
            or path.suffix.lower() in IGNORED_EXTENSIONS)


def list_photos(folder: Path) -> list[Path]:
    """Photos in a folder, sorted by file name; the first one is the cover."""
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and not is_ignored(p) and p.suffix.lower() in PHOTO_EXTENSIONS),
                  key=lambda p: p.name.lower())


def find_hint_file(folder: Path) -> tuple[Path | None, list[str]]:
    """hint.txt if present, else the only .txt file; plus warnings about hints we can't use."""
    texts = sorted((p for p in folder.iterdir() if p.is_file() and not is_ignored(p) and p.suffix.lower() == ".txt"),
                   key=lambda p: p.name.lower())
    for p in texts:
        if p.name.lower() == "hint.txt":
            return p, []
    if len(texts) == 1:
        return texts[0], []
    if len(texts) > 1:
        return None, ["several .txt files; name the hint hint.txt"]
    if any(p.suffix.lower() == ".gdoc" for p in folder.iterdir()):
        return None, ["hint saved as Google Doc; save it as plain .txt"]
    return None, []


def _clear_package(out_dir: Path) -> None:
    """Remove files from an earlier package (NN.jpg, listing.*) so no stale photo survives a rebuild."""
    for p in out_dir.iterdir():
        if p.is_file() and (PACKAGE_PHOTO.fullmatch(p.name) or p.name in ("listing.txt", "listing.json")):
            p.unlink()


@contextmanager
def _step(name: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        raise StepError(name, exc) from exc


def build_package(
    name: str,
    photos: list[Path],
    hint_file: Path | None,
    out_dir: Path,
    settings: Settings,
    deps: Deps,
    extra_warnings: list[str] | None = None,
) -> PackageResult:
    warnings = list(extra_warnings or [])
    llm = deps.llm
    with deps.ledger.item_scope(name):
        reason = deps.budget.blocked_reason()
        if reason:
            raise BudgetDeferred(reason)
        cost_before = deps.ledger.item_totals(name)["cost_usd"]

        with _step("hint"):
            hint = parse_hint(name, hint_file.read_text(encoding="utf-8-sig") if hint_file else None)
            warnings += hint.warnings

        with _step("photos"):
            out_dir.mkdir(parents=True, exist_ok=True)
            _clear_package(out_dir)
            for processed in process_photos_cached(name, photos, settings, deps.remover):
                shutil.copy2(processed, out_dir / processed.name)

        # Every Claude step reads the original photos, never the processed copies.
        with _step("describe"):
            desc = describe_item(photos, llm, settings)

        results = []
        with _step("research"):
            if needs_web_research(hint):
                specs_research = research_specs(photos, hint, desc, llm, settings)
                results.append(specs_research)
                comparables = research_comparables(hint, desc, specs_research, llm, settings, warnings=warnings)
                if comparables is not None:
                    results.append(comparables)
            else:
                log.info("%s: research skipped (hint complete or research: no)", name)

        with _step("specs"):
            if results:
                allowed = [u for r in results for u in (*r.retrieved_urls, *r.cited_urls)]
                specs, demoted = enforce_sources(parse_specs(hint, desc, results, llm, settings), allowed)
            else:
                specs, demoted = specs_from_hint(hint, desc), []

        with _step("listing"):
            draft = draft_listing(photos, hint, desc, specs, llm, settings)

        with _step("price"):
            price = suggest_price(hint, specs, hint.condition or draft.condition)

        with _step("write"):
            listing = finalize_listing(draft, hint, specs, price, demoted, settings)
            (out_dir / "listing.txt").write_text(render_listing_txt(listing, warnings), encoding="utf-8")
            (out_dir / "listing.json").write_text(listing.model_dump_json(indent=2), encoding="utf-8")

        cost = deps.ledger.item_totals(name)["cost_usd"] - cost_before
    log.info("%s: package written to %s ($%.3f)", name, out_dir, cost)
    return PackageResult(listing=listing, warnings=warnings, cost_usd=cost)


# --- Drive orchestration: inbox -> ready/ or failed/ -------------------------------------------------

MAX_TRANSIENT_ATTEMPTS = 3
MOVE_ATTEMPTS = 5
MOVE_BASE_DELAY = 1.0  # seconds; doubles each retry (Drive for desktop holds brief file locks)


@dataclass
class ProductResult:
    name: str
    status: Literal["ready", "failed", "deferred", "retry", "stuck"]
    out_dir: Path | None
    failed_step: str | None
    message: str


def unique_dest(parent: Path, name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    candidate, n = name, 1
    while (parent / candidate).exists() or (parent / f"{candidate}.partial").exists():
        n += 1
        candidate = f"{name}-{n}"
    return parent / candidate


def safe_move(src: Path, dst: Path, attempts: int = MOVE_ATTEMPTS, base_delay: float = MOVE_BASE_DELAY) -> None:
    for attempt in range(attempts):
        try:
            shutil.move(str(src), str(dst))
            return
        except OSError:  # PermissionError is an OSError
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * 2**attempt)


def is_transient(exc: BaseException) -> bool:
    """Errors worth retrying later: rate limits, overload, server errors and connection problems."""
    import anthropic

    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


def _move(src: Path, dst: Path) -> None:
    safe_move(src, dst, base_delay=MOVE_BASE_DELAY)


def _write_error(folder: Path, step: str, error: str, warnings: list[str]) -> None:
    lines = [f"step: {step}", f"error: {error}", f"time: {datetime.now():%Y-%m-%d %H:%M}"]
    if warnings:
        lines += ["warnings:", *(f"- {w}" for w in warnings)]
    lines.append("To retry, fix the cause and move this folder back into inbox.")
    (folder / "error.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fail(batch: ProductBatch, settings: Settings, store: StateStore, step: str, error: str) -> ProductResult:
    name = batch.folder.name
    dest = unique_dest(settings.failed, name)
    try:
        _move(batch.folder, dest)
        moved, where = True, dest
    except OSError as exc:
        log.error("%s: could not move to failed/: %s", name, exc)
        moved, where = False, batch.folder  # error.txt is ignored by the scanner, so writing it here is safe
    try:
        _write_error(where, step, error, batch.warnings)
    except OSError as exc:
        log.error("%s: could not write error.txt: %s", name, exc)
    store.record(str(batch.folder), batch.snapshot_hash, "failed", moved=moved)
    log.warning("%s: failed at step %s: %s", name, step, error)
    return ProductResult(name=name, status="failed" if moved else "stuck", out_dir=dest if moved else None,
                         failed_step=step, message=error if moved else f"failed at {step}; could not move to failed/")


def _deliver(batch: ProductBatch, built: Path, settings: Settings) -> Path:
    """Copy the package into ready/ as .partial, move the originals in, then rename into place."""
    dest = unique_dest(settings.ready, batch.folder.name)
    partial = dest.with_name(dest.name + ".partial")
    shutil.copytree(built, partial)
    originals = partial / "originals"
    originals.mkdir()
    for entry in sorted(batch.folder.iterdir()):
        _move(entry, originals / entry.name)
    _move(partial, dest)
    batch.folder.rmdir()
    return dest


def process_product(batch: ProductBatch, settings: Settings, deps: Deps, store: StateStore) -> ProductResult:
    """Process one settled inbox folder. Never raises."""
    name = batch.folder.name
    folder_key = str(batch.folder)
    if store.is_stuck(folder_key):
        return ProductResult(name=name, status="stuck", out_dir=None, failed_step=None,
                             message="already processed but could not be moved; move it out of inbox manually")
    state = store.get(folder_key, batch.snapshot_hash)
    attempts = state.attempts if state is not None and state.outcome == "transient" else 0

    try:
        cache_dir = settings.preprocessed_dir / f"{name}-{photos_key(batch.photos, settings)}"
    except OSError as exc:
        return _fail(batch, settings, store, "photos", f"{type(exc).__name__}: {exc}")

    built = Path(tempfile.mkdtemp(prefix="vinted-listing-"))
    try:
        try:
            result = build_package(name, batch.photos, batch.hint_file, built, settings, deps,
                                   extra_warnings=batch.warnings)
        except BudgetDeferred as exc:
            return ProductResult(name=name, status="deferred", out_dir=None, failed_step=None, message=exc.reason)
        except StepError as exc:
            error = f"{type(exc.cause).__name__}: {exc.cause}"
            if is_transient(exc.cause) and attempts < MAX_TRANSIENT_ATTEMPTS:
                store.record(folder_key, batch.snapshot_hash, "transient", moved=False)
                log.warning("%s: temporary error at step %s (attempt %d): %s", name, exc.step, attempts + 1, error)
                return ProductResult(name=name, status="retry", out_dir=None, failed_step=exc.step,
                                     message=f"temporary error at {exc.step}, will retry: {error}")
            return _fail(batch, settings, store, exc.step, error)
        except Exception as exc:  # anything build_package didn't attribute to a step
            return _fail(batch, settings, store, "unknown", f"{type(exc).__name__}: {exc}")

        try:
            dest = _deliver(batch, built, settings)
        except OSError as exc:
            store.record(folder_key, batch.snapshot_hash, "ready", moved=False)
            log.error("%s: listing built but the Drive move failed: %s", name, exc)
            return ProductResult(name=name, status="stuck", out_dir=None, failed_step=None,
                                 message=f"listing built but moving files failed: {exc}")
        store.record(folder_key, batch.snapshot_hash, "ready", moved=True)
        try:
            shutil.rmtree(cache_dir)
        except OSError as exc:
            log.info("%s: photo cache not deleted (%s); prune_cache will remove it", name, exc)
        log.info("%s: ready in %s ($%.3f)", name, dest, result.cost_usd)
        return ProductResult(name=name, status="ready", out_dir=dest, failed_step=None,
                             message=f"ready (${result.cost_usd:.2f})")
    finally:
        shutil.rmtree(built, ignore_errors=True)


def fail_empty(folder: Path, settings: Settings, store: StateStore) -> ProductResult:
    """A settled folder with no photos goes to failed/ with error.txt reading 'no photos'."""
    from vinted_listing.scanner import snapshot_hash, snapshot_of

    name = folder.name
    snapshot = snapshot_hash(snapshot_of(folder))
    if store.is_stuck(str(folder)):
        return ProductResult(name=name, status="stuck", out_dir=None, failed_step="photos",
                             message="no photos; could not be moved, move it out of inbox manually")
    dest = unique_dest(settings.failed, name)
    try:
        _move(folder, dest)
    except OSError as exc:
        store.record(str(folder), snapshot, "failed", moved=False)
        return ProductResult(name=name, status="stuck", out_dir=None, failed_step="photos",
                             message=f"no photos; could not be moved: {exc}")
    (dest / "error.txt").write_text("no photos\n", encoding="utf-8")
    store.record(str(folder), snapshot, "failed", moved=True)
    log.warning("%s: no photos, moved to %s", name, dest)
    return ProductResult(name=name, status="failed", out_dir=dest, failed_step="photos", message="no photos")
