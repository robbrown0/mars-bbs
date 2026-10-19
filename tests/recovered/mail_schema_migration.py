#!/usr/bin/env python3
"""Prove an existing V2 mailbox upgrades without rewriting old messages."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from marsv2.maild import Mail


with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary) / "mail.db"
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE messages (
      id INTEGER PRIMARY KEY,
      sender_id INTEGER NOT NULL,
      sender_user TEXT NOT NULL,
      recipient_id INTEGER NOT NULL,
      recipient_user TEXT NOT NULL,
      subject TEXT NOT NULL,
      body TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN (0,1))
    );
    CREATE INDEX mailbox_idx ON messages(recipient_id,id);
    INSERT INTO messages(id,sender_id,sender_user,recipient_id,recipient_user,
                         subject,body,created_at,is_read)
      VALUES(17,1,'alice',2,'bob','preserved','legacy body',12345,1);
    """)
    db.commit()
    db.close()

    mail = Mail(str(path), "/not-used/session.sock", "/not-used/auth.sock")
    columns = {row["name"] for row in mail.db.execute("PRAGMA table_info(messages)")}
    assert {"recipient_deleted", "sender_deleted"} <= columns
    assert tuple(mail.db.execute(
        "SELECT subject,body,is_read,recipient_deleted,sender_deleted "
        "FROM messages WHERE id=17").fetchone()) == (
            "preserved", "legacy body", 1, 0, 0)
    assert mail.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='drafts'").fetchone()
    mail.db.close()

print("PASS non-destructive mail schema migration")
