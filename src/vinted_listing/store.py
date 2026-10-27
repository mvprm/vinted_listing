"""What happened to each inbox folder snapshot, so a failed move never turns into a reprocess loop."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

Outcome = Literal["ready", "failed", "transient"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    folder TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    moved INTEGER NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (folder, snapshot_hash)
)
"""


@dataclass
class ItemState:
    folder: str
    snapshot_hash: str
    outcome: Outcome
    attempts: int
    moved: bool
    ts: str


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return closing(sqlite3.connect(self.db_path))  # type: ignore[return-value]

    @staticmethod
    def _row(row) -> ItemState:
        folder, snapshot, outcome, attempts, moved, ts = row
        return ItemState(folder, snapshot, outcome, int(attempts), bool(moved), ts)

    def get(self, folder: str, snapshot_hash: str) -> ItemState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE folder = ? AND snapshot_hash = ?",
                               (folder, snapshot_hash)).fetchone()
        return self._row(row) if row else None

    def record(self, folder: str, snapshot_hash: str, outcome: Outcome, moved: bool) -> None:
        previous = self.get(folder, snapshot_hash)
        attempts = previous.attempts if previous else 0
        if outcome == "transient":
            attempts += 1
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)",
                (folder, snapshot_hash, outcome, attempts, int(moved), datetime.now().isoformat(timespec="seconds")),
            )

    def stuck(self) -> list[str]:
        """Folders whose final move failed: they stay in the inbox and are not reprocessed."""
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT folder FROM items WHERE moved = 0 AND outcome IN ('ready', 'failed')"
                                " ORDER BY folder").fetchall()
        return [r[0] for r in rows]

    def is_stuck(self, folder: str) -> bool:
        """True for any snapshot of this folder: a half-finished move changes the snapshot."""
        return folder in self.stuck()

    def clear_resolved(self) -> int:
        """Forget stuck records whose folder has left the inbox, so moving it back later retries it."""
        gone = [f for f in self.stuck() if not Path(f).exists()]
        with self._connect() as conn, conn:
            conn.executemany("DELETE FROM items WHERE folder = ? AND moved = 0", [(f,) for f in gone])
        return len(gone)

    def recent(self, limit: int = 10) -> list[ItemState]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM items WHERE outcome IN ('ready', 'failed')"
                                " ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]
