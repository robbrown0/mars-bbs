"""Offline one-way Eagles data migration into V2 authority databases.

Legacy password fields are deliberately never copied.  Only an already-modern
Argon2id file may enable a migrated login; all other identities are imported
disabled and require an administrator-controlled reset after migration.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .authd import SCHEMA as AUTH_SCHEMA
from .boardd import SCHEMA as BOARD_SCHEMA
from .filed import SCHEMA as FILE_SCHEMA
from .maild import SCHEMA as MAIL_SCHEMA
from .validate import userid

ARGON = re.compile(r"\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+\Z")


def regular_files(root: Path):
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in names:
            path = base / name
            if path.is_symlink():
                raise ValueError(f"symlink rejected: {path.relative_to(root)}")
        for name in files:
            path = base / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"special file rejected: {path.relative_to(root)}")
            if info.st_size > 16 * 1024 * 1024:
                raise ValueError(f"oversized source file: {path.relative_to(root)}")
            yield path, info


def inventory(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path, info in sorted(regular_files(root), key=lambda item: str(item[0])):
        relative = str(path.relative_to(root)).encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(info.st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        count += 1
    return digest.hexdigest(), count


def parse_accounts(root: Path) -> list[dict[str, object]]:
    source = root / "etc/passwds"
    accounts: list[dict[str, object]] = []
    if not source.is_file() or source.is_symlink():
        raise ValueError("missing safe etc/passwds")
    with source.open("r", encoding="utf-8", errors="strict") as stream:
        for line in stream:
            if not line.strip() or line[0].isspace() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split(":")
            if len(fields) < 5:
                raise ValueError("malformed account record")
            name = userid(fields[0].strip())
            display = fields[4].strip()[:25] or name
            modern = root / "home" / name / ".password-argon2id"
            hash_value = "!MIGRATED-DISABLED!"
            enabled = False
            if modern.is_file() and not modern.is_symlink():
                value = modern.read_text(encoding="ascii").strip()
                if not ARGON.fullmatch(value):
                    raise ValueError("invalid Argon2id record")
                hash_value, enabled = value, True
            profile = root / "home" / name / "profile"
            profile_text = ""
            if profile.is_file() and not profile.is_symlink():
                profile_text = profile.read_text(encoding="utf-8", errors="replace")[:4096]
            accounts.append({"userid": name, "display": display, "hash": hash_value,
                             "enabled": enabled, "profile": profile_text})
    return accounts


def parse_message(path: Path) -> tuple[str, str, str]:
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("oversized legacy message")
    parsed = email.parser.BytesParser().parsebytes(raw)
    sender = str(parsed.get("From", "legacy"))[:12]
    try:
        sender = userid(sender)
    except ValueError:
        sender = "legacy"
    subject = str(parsed.get("Subject", "Imported message"))[:72]
    body = parsed.get_payload(decode=True)
    if body is None:
        body_text = str(parsed.get_payload())
    else:
        body_text = body.decode("utf-8", "replace")
    return sender, subject or "Imported message", body_text[:16384]


def open_db(path: str, schema: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=DELETE")
    db.executescript(schema)
    return db


def apply(args, source_hash: str, accounts: list[dict[str, object]]) -> dict[str, object]:
    auth = open_db(args.auth_db, AUTH_SCHEMA)
    mail = open_db(args.mail_db, MAIL_SCHEMA)
    board = open_db(args.board_db, BOARD_SCHEMA)
    files = open_db(args.file_db, FILE_SCHEMA)
    auth.executescript("""
      CREATE TABLE IF NOT EXISTS profiles(
        account_id INTEGER PRIMARY KEY,
        email TEXT NOT NULL DEFAULT '',
        profile_text TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS migration_commits(
        source_hash TEXT PRIMARY KEY, committed_at INTEGER NOT NULL, rollback_json TEXT NOT NULL);
    """)
    if auth.execute("SELECT 1 FROM migration_commits WHERE source_hash=?", (source_hash,)).fetchone():
        raise ValueError("source was already migrated")
    inserted = {"accounts": [], "mail": [], "posts": [], "files": []}
    identity: dict[str, int] = {}
    now = int(time.time())
    try:
        for db in (auth, mail, board, files):
            db.execute("BEGIN IMMEDIATE")
        for account in sorted(accounts, key=lambda item: str(item["userid"])):
            existing = auth.execute("SELECT id FROM accounts WHERE userid=?",
                                    (account["userid"],)).fetchone()
            if existing:
                identity[str(account["userid"])] = int(existing["id"])
                continue
            cursor = auth.execute(
                "INSERT INTO accounts(userid,display_name,password_hash,runtime_uid,is_admin,login_enabled,created_at) "
                "VALUES(?,?,?,0,0,?,?)",
                (account["userid"], account["display"], account["hash"],
                 int(bool(account["enabled"])), now))
            account_id = int(cursor.lastrowid)
            runtime_uid = 30000 + account_id
            if runtime_uid > 60000:
                raise ValueError("runtime UID range exhausted")
            auth.execute("UPDATE accounts SET runtime_uid=? WHERE id=?", (runtime_uid, account_id))
            auth.execute("INSERT INTO profiles(account_id,profile_text) VALUES(?,?)",
                         (account_id, account["profile"]))
            identity[str(account["userid"])] = account_id
            inserted["accounts"].append(account_id)

        for mailbox in sorted((Path(args.source) / "home").glob("*/mail")):
            recipient = mailbox.parent.name.lower()
            if recipient not in identity:
                continue
            for path in sorted(mailbox.iterdir()):
                if not path.is_file() or path.is_symlink():
                    continue
                sender, subject, body = parse_message(path)
                cursor = mail.execute(
                    "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,subject,body,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (0, sender, identity[recipient], recipient,
                                                subject, body, int(path.stat().st_mtime)))
                inserted["mail"].append(int(cursor.lastrowid))

        for directory in sorted((Path(args.source) / "boards").glob("*")):
            if not directory.is_dir() or directory.is_symlink():
                continue
            name = directory.name[:40]
            board.execute("INSERT OR IGNORE INTO boards(name,description) VALUES(?,?)",
                          (name, "Migrated Eagles board"))
            board_id = board.execute("SELECT id FROM boards WHERE name=?", (name,)).fetchone()[0]
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.is_symlink():
                    continue
                sender, subject, body = parse_message(path)
                author_id = identity.get(sender.lower(), 0)
                cursor = board.execute(
                    "INSERT INTO posts(board_id,author_id,author_user,subject,body,created_at) "
                    "VALUES(?,?,?,?,?,?)", (board_id, author_id, sender, subject, body,
                                             int(path.stat().st_mtime)))
                inserted["posts"].append(int(cursor.lastrowid))

        for path in sorted((Path(args.source) / "files").glob("**/*")):
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            relative = path.relative_to(Path(args.source) / "files")
            area = str(relative.parent)[:32] if str(relative.parent) != "." else "General"
            cursor = files.execute(
                "INSERT INTO files(area,name,content) VALUES(?,?,?)",
                (area, relative.name[:80], content))
            inserted["files"].append(int(cursor.lastrowid))

        auth.execute("INSERT INTO migration_commits VALUES(?,?,?)",
                     (source_hash, now, json.dumps(inserted, sort_keys=True)))
        # Commit content authorities first and the auth ledger last.  The source
        # remains untouched; interrupted work is detected by preflight counts
        # and must be rolled back before retrying.
        for db in (mail, board, files, auth):
            db.commit()
    except BaseException:
        for db in (auth, mail, board, files):
            db.rollback()
        raise
    finally:
        for db in (auth, mail, board, files):
            db.close()
    return {key: len(value) for key, value in inserted.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--auth-db")
    parser.add_argument("--mail-db")
    parser.add_argument("--board-db")
    parser.add_argument("--file-db")
    parser.add_argument("--report", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-offline", action="store_true")
    args = parser.parse_args()
    root = Path(args.source).resolve()
    if not root.is_dir() or root.is_symlink():
        raise SystemExit("source must be a real directory")
    source_hash, file_count = inventory(root)
    accounts = parse_accounts(root)
    report: dict[str, object] = {
        "format": "eagles-v1-to-mars-v2", "source_sha256": source_hash,
        "source_files": file_count, "accounts": len(accounts),
        "accounts_with_argon2id": sum(bool(item["enabled"]) for item in accounts),
        "legacy_passwords_exported": False, "mode": "dry-run" if args.dry_run else "apply",
    }
    if args.apply:
        if not args.confirm_offline:
            raise SystemExit("--apply requires --confirm-offline")
        if not all((args.auth_db, args.mail_db, args.board_db, args.file_db)):
            raise SystemExit("--apply requires all four database paths")
        report["inserted"] = apply(args, source_hash, accounts)
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
