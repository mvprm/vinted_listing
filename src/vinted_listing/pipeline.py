"""Build one item's package: processed photos, listing.txt and listing.json."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vinted_listing.config import Settings
from vinted_listing.describe import describe_item
from vinted_listing.hints import parse_hint
from vinted_listing.imaging import BackgroundRemover, process_photos_cached
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
from vinted_listing.usage import BudgetGuard, UsageLedger

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
IGNORED_NAMES = {"desktop.ini", "error.txt"}
IGNORED_EXTENSIONS = {".tmp", ".gdoc", ".gsheet", ".gslides", ".lnk"}


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


def _ignored(path: Path) -> bool:
    name = path.name
    return (name.startswith((".", "~$")) or name.lower() in IGNORED_NAMES
            or path.suffix.lower() in IGNORED_EXTENSIONS)


def list_photos(folder: Path) -> list[Path]:
    """Photos in a folder, sorted by file name; the first one is the cover."""
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and not _ignored(p) and p.suffix.lower() in PHOTO_EXTENSIONS),
                  key=lambda p: p.name.lower())


def find_hint_file(folder: Path) -> tuple[Path | None, list[str]]:
    """hint.txt if present, else the only .txt file; plus warnings about hints we can't use."""
    texts = sorted((p for p in folder.iterdir() if p.is_file() and not _ignored(p) and p.suffix.lower() == ".txt"),
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
