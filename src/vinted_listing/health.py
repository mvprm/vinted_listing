"""Watcher heartbeat: a small local file the `health` command (and Docker's health check) reads."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

DEFAULT_MAX_AGE_SECONDS = 600  # one item can take a few minutes; the heartbeat is refreshed after each item


def write_heartbeat(path: Path, drive_ready: bool, now: float | None = None) -> None:
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": now, "time": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
               "drive_ready": drive_ready, "pid": os.getpid()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def check_health(path: Path, max_age: float = DEFAULT_MAX_AGE_SECONDS, now: float | None = None) -> tuple[bool, str]:
    """(healthy, message): healthy when the watcher's heartbeat is recent and it can reach the Drive folder."""
    now = time.time() if now is None else now
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        age = now - float(beat["ts"])
        drive_ready = bool(beat["drive_ready"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, f"no heartbeat at {path}: is 'watch' running?"
    if age > max_age:
        return False, f"stale: last heartbeat {age:.0f}s ago (limit {max_age:.0f}s)"
    if not drive_ready:
        return False, f"Drive folder not reachable (heartbeat {age:.0f}s ago)"
    return True, f"ok: last heartbeat {age:.0f}s ago"
