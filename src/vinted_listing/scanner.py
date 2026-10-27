"""Inbox scanner: a product folder is handed over only once its uploads have settled."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from vinted_listing.pipeline import find_hint_file, is_ignored, list_photos

UPLOADING = "uploading"
WAITING_FOR_HINT = "waiting for hint.txt"


@dataclass
class ProductBatch:
    folder: Path
    photos: list[Path]
    hint_file: Path | None
    warnings: list[str]
    snapshot_hash: str


@dataclass
class ScanResult:
    ready: list[ProductBatch] = field(default_factory=list)
    empty: list[Path] = field(default_factory=list)
    waiting: list[tuple[Path, str]] = field(default_factory=list)
    loose_files: list[Path] = field(default_factory=list)


Snapshot = frozenset[tuple[str, int]]


def snapshot_of(folder: Path) -> Snapshot:
    """(name, size) of every non-ignored file in the folder. File times can't be trusted on Drive."""
    return frozenset((p.name, p.stat().st_size) for p in folder.iterdir() if p.is_file() and not is_ignored(p))


def snapshot_hash(snapshot: Snapshot) -> str:
    return hashlib.sha256(json.dumps(sorted(snapshot)).encode()).hexdigest()[:16]


class InboxScanner:
    def __init__(self, inbox: Path, quiet_seconds: int, hint_grace_seconds: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.inbox = inbox
        self.quiet_seconds = quiet_seconds
        self.hint_grace_seconds = hint_grace_seconds
        self.clock = clock
        self._seen: dict[Path, tuple[Snapshot, float]] = {}

    def forget(self, folder: Path) -> None:
        self._seen.pop(folder, None)

    def scan(self) -> ScanResult:
        result = ScanResult()
        if not self.inbox.is_dir():
            return result
        now = self.clock()
        present: set[Path] = set()
        for entry in sorted(self.inbox.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file():
                if not is_ignored(entry):
                    result.loose_files.append(entry)
                continue
            if not entry.is_dir():
                continue
            present.add(entry)
            self._classify(entry, now, result)
        for gone in set(self._seen) - present:
            del self._seen[gone]
        return result

    def _classify(self, folder: Path, now: float, result: ScanResult) -> None:
        try:
            snapshot = snapshot_of(folder)
        except OSError:  # vanished or locked mid-scan: try again next cycle
            result.waiting.append((folder, UPLOADING))
            return
        previous = self._seen.get(folder)
        if previous is None or previous[0] != snapshot:
            self._seen[folder] = (snapshot, now)
            first_seen = now
        else:
            first_seen = previous[1]
        settled_for = now - first_seen

        if settled_for < self.quiet_seconds or any(size == 0 for _, size in snapshot):
            result.waiting.append((folder, UPLOADING))
            return
        photos = list_photos(folder)
        if not photos:
            result.empty.append(folder)
            return
        has_txt = any(Path(name).suffix.lower() == ".txt" for name, _ in snapshot)
        if not has_txt and settled_for < self.hint_grace_seconds:
            result.waiting.append((folder, WAITING_FOR_HINT))
            return
        hint_file, warnings = find_hint_file(folder)
        result.ready.append(ProductBatch(folder=folder, photos=photos, hint_file=hint_file, warnings=warnings,
                                         snapshot_hash=snapshot_hash(snapshot)))
