"""SQLite helpers with conservative durability defaults."""

from __future__ import annotations

import os
import sqlite3


def connect(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None,
                         check_same_thread=check_same_thread)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    return db
