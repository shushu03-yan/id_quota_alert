"""Consistent local SQLite backups and bounded retention."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3


def create_backup(
    database_path: Path | str, *, backup_directory: Path | str = Path("data/backups"),
    retain: int = 30, now: datetime | None = None,
) -> Path:
    if retain < 1:
        raise ValueError("retain must be at least 1")
    source_path = Path(database_path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    directory = Path(backup_directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    stem = now.astimezone(timezone.utc).strftime("quota-alert-%Y%m%dT%H%M%SZ")
    target = directory / f"{stem}.sqlite3"
    counter = 1
    while target.exists():
        target = directory / f"{stem}-{counter}.sqlite3"
        counter += 1
    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(target)) as destination:
        with destination:
            source.backup(destination)
            check = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"backup integrity check failed: {check}")
    backups = sorted(directory.glob("quota-alert-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for expired in backups[retain:]:
        expired.unlink()
    return target
