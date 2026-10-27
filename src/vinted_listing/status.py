"""status.txt in the Drive folder: what the watcher is doing, readable from the phone."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vinted_listing.config import Settings
from vinted_listing.store import StateStore
from vinted_listing.usage import UsageLedger

if TYPE_CHECKING:
    from vinted_listing.pipeline import ProductResult
    from vinted_listing.scanner import ScanResult


def write_status(settings: Settings, scan: ScanResult, results: list[ProductResult], ledger: UsageLedger,
                 store: StateStore, now: datetime) -> None:
    summary = ledger.month_summary(now)
    lines = [
        f"Vinted listing status - updated {now:%Y-%m-%d %H:%M}",
        "",
        f"Budget: ${summary['cost_usd']:.2f} of ${summary['budget_usd']:.2f} used this month "
        f"(${summary['remaining_usd']:.2f} left)",
    ]

    def section(title: str, items: list[str]) -> None:
        if items:
            lines.extend(["", title, *items])

    section("Waiting:", [f"- {folder.name}: {reason}" for folder, reason in scan.waiting])
    section("Deferred (not started):", [f"- {r.name}: {r.message}" for r in results if r.status == "deferred"])
    section("Will retry:", [f"- {r.name}: {r.message}" for r in results if r.status == "retry"])
    section("Stuck - move this folder out of inbox manually:",
            [f"- {folder}" for folder in store.stuck() if Path(folder).exists()])
    section("Loose files in inbox (put them in an item folder):", [f"- {p.name}" for p in scan.loose_files])

    recent = store.recent(10)
    lines.extend(["", "Last results:"])
    lines.extend([f"- {s.ts[:16].replace('T', ' ')} {Path(s.folder).name}: {s.outcome}" for s in recent] or ["- none yet"])

    # Write then replace, so Drive never syncs a half-written file.
    tmp = settings.status_file.with_name(settings.status_file.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, settings.status_file)
