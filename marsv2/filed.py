"""Read-only curated file-area and historical-catalog authority."""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import secrets
import time

from .db import connect
from .identity import validate
from .rpc import RPCError, serve
from .validate import integer, text

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  area TEXT NOT NULL,
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  UNIQUE(area,name)
);
CREATE TABLE IF NOT EXISTS historical_catalog (
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL,
  filename TEXT NOT NULL,
  listed_size INTEGER NOT NULL,
  listed_date TEXT NOT NULL,
  mode TEXT NOT NULL,
  listing_line INTEGER NOT NULL,
  recovery_status TEXT NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  license_status TEXT NOT NULL DEFAULT 'unknown',
  notes TEXT NOT NULL DEFAULT '',
  published_file_id INTEGER,
  UNIQUE(category,filename)
);
CREATE TABLE IF NOT EXISTS source_payloads (
  sha256 TEXT PRIMARY KEY,
  original BLOB NOT NULL,
  size INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  CHECK(length(sha256)=64),
  CHECK(length(original)=size)
);
CREATE TABLE IF NOT EXISTS import_batches (
  id TEXT PRIMARY KEY,
  manifest_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  item_count INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('committed','rolled_back'))
);
CREATE TABLE IF NOT EXISTS download_tickets (
  token_hash TEXT PRIMARY KEY CHECK(length(token_hash)=64),
  file_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  payload_hash TEXT NOT NULL DEFAULT '',
  payload_size INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL,
  stream_hash TEXT NOT NULL DEFAULT '',
  next_offset INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS download_tickets_expiry
  ON download_tickets(expires_at);
CREATE TABLE IF NOT EXISTS download_log (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL,
  file_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  size INTEGER NOT NULL,
  completed_at INTEGER NOT NULL
);
"""

FILE_COLUMNS = {
    "description": "TEXT NOT NULL DEFAULT ''",
    "source_url": "TEXT NOT NULL DEFAULT ''",
    "sha256": "TEXT NOT NULL DEFAULT ''",
    "original_date": "TEXT NOT NULL DEFAULT ''",
    "license_status": "TEXT NOT NULL DEFAULT 'locally generated'",
    "historical": "INTEGER NOT NULL DEFAULT 0",
    "encoding": "TEXT NOT NULL DEFAULT 'utf-8'",
    "source_identity": "TEXT NOT NULL DEFAULT ''",
    "payload_sha256": "TEXT NOT NULL DEFAULT ''",
    "batch_id": "TEXT NOT NULL DEFAULT ''",
    "original_size": "INTEGER NOT NULL DEFAULT 0",
    "attribution": "TEXT NOT NULL DEFAULT ''",
    "retrieved_at": "TEXT NOT NULL DEFAULT ''",
    "rights_evidence": "TEXT NOT NULL DEFAULT ''",
    "rendering_policy": "TEXT NOT NULL DEFAULT 'wrapped'",
    "rights_decision": "TEXT NOT NULL DEFAULT ''",
    "content_decision": "TEXT NOT NULL DEFAULT ''",
    "max_line_columns": "INTEGER NOT NULL DEFAULT 0",
}

MAX_EXPORT_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
DOWNLOAD_TICKET_SECONDS = 5 * 60
DOWNLOAD_CHUNK_BYTES = 24 * 1024
DOWNLOAD_GATEWAY_UID = 29999
DOWNLOAD_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def initialize(db: object) -> None:
    db.executescript(SCHEMA)
    existing = {str(row[1]) for row in db.execute("PRAGMA table_info(files)")}
    ticket_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(download_tickets)")
    }
    with db:
        for name, declaration in FILE_COLUMNS.items():
            if name not in existing:
                db.execute(f"ALTER TABLE files ADD COLUMN {name} {declaration}")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS files_managed_source "
            "ON files(source_identity,payload_sha256) WHERE source_identity<>''")
        db.execute(
            "INSERT OR IGNORE INTO files(area,name,content,description) VALUES(?,?,?,?)",
            ("Local", "WELCOME.TXT",
             "Mars Hotel Revival local file area.\n"
             "This harmless fixture was generated locally.\n",
             "Locally generated file-area test and welcome document."))
        if "stream_hash" not in ticket_columns:
            db.execute(
                "ALTER TABLE download_tickets ADD COLUMN stream_hash "
                "TEXT NOT NULL DEFAULT ''")
        if "payload_hash" not in ticket_columns:
            db.execute(
                "ALTER TABLE download_tickets ADD COLUMN payload_hash "
                "TEXT NOT NULL DEFAULT ''")
        if "payload_size" not in ticket_columns:
            db.execute(
                "ALTER TABLE download_tickets ADD COLUMN payload_size "
                "INTEGER NOT NULL DEFAULT 0")


class Files:
    def __init__(self, db_path: str, session_socket: str):
        self.db = connect(db_path)
        initialize(self.db)
        self.session_socket = session_socket
        # A restart invalidates an in-flight transport stream but not a fresh
        # unclaimed ticket. Reset claims so their owners may retry.
        with self.db:
            self.db.execute(
                "UPDATE download_tickets SET stream_hash='',next_offset=0 "
                "WHERE expires_at>=?", (int(time.time()),))
            self._prune_downloads()

    def _prune_downloads(self) -> None:
        now = int(time.time())
        self.db.execute("DELETE FROM download_tickets WHERE expires_at<?", (now,))
        self.db.execute(
            "DELETE FROM download_log WHERE id NOT IN "
            "(SELECT id FROM download_log ORDER BY id DESC LIMIT 5000)")

    def _payload(self, file_id: int):
        row = self.db.execute(
            "SELECT f.id,f.name,f.payload_sha256,f.sha256,f.content,p.original,p.size "
            "FROM files f LEFT JOIN source_payloads p ON p.sha256=f.payload_sha256 "
            "WHERE f.id=?", (file_id,)).fetchone()
        if row is None:
            raise RPCError("no such file")
        payload = bytes(row["original"]) if row["original"] is not None \
            else str(row["content"]).encode("utf-8")
        return row, payload

    def _payload_chunk(self, file_id: int, offset: int,
                       expected_size: int, expected_hash: str):
        """Read one bounded byte slice without materializing a large BLOB."""
        row = self.db.execute(
            "SELECT f.name,f.payload_sha256,p.sha256 AS stored_sha256,"
            "CASE WHEN p.original IS NOT NULL THEN p.size "
            "ELSE length(CAST(f.content AS BLOB)) END AS payload_size,"
            "CASE WHEN p.original IS NOT NULL THEN substr(p.original,?,?) "
            "ELSE substr(CAST(f.content AS BLOB),?,?) END AS payload "
            "FROM files f LEFT JOIN source_payloads p ON p.sha256=f.payload_sha256 "
            "WHERE f.id=?",
            (offset + 1, DOWNLOAD_CHUNK_BYTES, offset + 1,
             DOWNLOAD_CHUNK_BYTES, file_id)).fetchone()
        if row is None or int(row["payload_size"]) != expected_size:
            raise RPCError("file changed during download")
        if (row["stored_sha256"] is not None and
                not secrets.compare_digest(str(row["stored_sha256"]), expected_hash)):
            raise RPCError("file changed during download")
        return row, bytes(row["payload"] or b"")

    @staticmethod
    def _download_secret(data: dict[str, object], field: str) -> tuple[str, str]:
        token = data.get(field)
        if not isinstance(token, str) or not DOWNLOAD_TOKEN.fullmatch(token):
            raise RPCError("invalid or expired download ticket")
        return token, hashlib.sha256(token.encode("ascii")).hexdigest()

    def _download_open(self, data: dict[str, object], peer: tuple[int, int, int]):
        if peer[1] != DOWNLOAD_GATEWAY_UID:
            raise RPCError("download transport denied")
        if set(data) != {"ticket"}:
            raise RPCError("invalid download request")
        _, digest = self._download_secret(data, "ticket")
        now = int(time.time())
        with self.db:
            self._prune_downloads()
            ticket = self.db.execute(
                "SELECT * FROM download_tickets WHERE token_hash=?", (digest,)).fetchone()
            if ticket is None or int(ticket["expires_at"]) < now:
                raise RPCError("invalid or expired download ticket")
            if str(ticket["stream_hash"]):
                raise RPCError("download ticket has already been claimed")
            row, payload = self._payload(int(ticket["file_id"]))
            digest_now = hashlib.sha256(payload).hexdigest()
            if (len(payload) > MAX_DOWNLOAD_BYTES or len(payload) != int(ticket["payload_size"]) or
                    not secrets.compare_digest(digest_now, str(ticket["payload_hash"]))):
                raise RPCError("file changed during download")
            stream = secrets.token_urlsafe(32)
            stream_hash = hashlib.sha256(stream.encode("ascii")).hexdigest()
            self.db.execute(
                "UPDATE download_tickets SET stream_hash=?,next_offset=0 "
                "WHERE token_hash=?", (stream_hash, digest))
        return {"name": row["name"], "size": len(payload),
                "sha256": digest_now,
                "chunk_bytes": DOWNLOAD_CHUNK_BYTES, "stream": stream}

    def _download_chunk(self, data: dict[str, object], peer: tuple[int, int, int]):
        if peer[1] != DOWNLOAD_GATEWAY_UID:
            raise RPCError("download transport denied")
        if set(data) != {"ticket", "stream", "offset"}:
            raise RPCError("invalid download request")
        _, digest = self._download_secret(data, "ticket")
        _, stream_hash = self._download_secret(data, "stream")
        offset = integer(data.get("offset", 0), "offset", 0, MAX_DOWNLOAD_BYTES)
        now = int(time.time())
        with self.db:
            self._prune_downloads()
            ticket = self.db.execute(
                "SELECT * FROM download_tickets WHERE token_hash=?", (digest,)).fetchone()
            if (ticket is None or int(ticket["expires_at"]) < now or
                    not secrets.compare_digest(str(ticket["stream_hash"]), stream_hash)):
                raise RPCError("invalid or expired download ticket")
            if offset != int(ticket["next_offset"]):
                raise RPCError("download offset is out of sequence")
            expected_size = int(ticket["payload_size"])
            expected_hash = str(ticket["payload_hash"])
            if expected_size > MAX_DOWNLOAD_BYTES or offset > expected_size:
                raise RPCError("invalid download size")
            row, chunk = self._payload_chunk(
                int(ticket["file_id"]), offset, expected_size, expected_hash)
            next_offset = offset + len(chunk)
            eof = next_offset >= expected_size
            if eof:
                self.db.execute("DELETE FROM download_tickets WHERE token_hash=?", (digest,))
                self.db.execute(
                    "INSERT INTO download_log(account_id,file_id,name,size,completed_at) "
                    "VALUES(?,?,?,?,?)", (ticket["account_id"], ticket["file_id"],
                                          row["name"], expected_size, now))
                self._prune_downloads()
            else:
                self.db.execute(
                    "UPDATE download_tickets SET next_offset=? WHERE token_hash=?",
                    (next_offset, digest))
        return {"data": base64.b64encode(chunk).decode("ascii"),
                "offset": offset, "next_offset": next_offset,
                "total": expected_size, "eof": eof}

    def handle(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = req["op"]
        if op == "health":
            self.db.execute("SELECT 1").fetchone()
            return {"status": "ok"}
        data = req["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        if op == "download_open":
            return self._download_open(data, peer)
        if op == "download_chunk":
            return self._download_chunk(data, peer)
        ident = validate(self.session_socket, req.get("token"), peer[1])
        try:
            if op == "list":
                area = text(data.get("area", ""), "area", 64).strip()
                if area:
                    rows = self.db.execute(
                        "SELECT id,area,name,CASE WHEN original_size>0 THEN original_size "
                        "ELSE length(content) END AS size,description,original_date,"
                        "historical,rendering_policy,max_line_columns FROM files "
                        "WHERE area=? ORDER BY name",
                        (area,)).fetchall()
                else:
                    rows = self.db.execute(
                        "SELECT id,area,name,CASE WHEN original_size>0 THEN original_size "
                        "ELSE length(content) END AS size,description,original_date,"
                        "historical,rendering_policy,max_line_columns FROM files "
                        "ORDER BY area,name").fetchall()
                return {"files": [dict(row) for row in rows]}
            if op == "areas":
                rows = self.db.execute(
                    "SELECT area,count(*) AS files,sum(CASE WHEN original_size>0 "
                    "THEN original_size ELSE length(content) END) AS size "
                    "FROM files GROUP BY area ORDER BY area COLLATE NOCASE").fetchall()
                return {"areas": [dict(row) for row in rows]}
            if op == "read":
                file_id = integer(data.get("id"), "file id")
                row = self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
                if row is None:
                    raise RPCError("no such file")
                if len(str(row["content"])) > 48_000:
                    raise RPCError("file is too large for one response; use read_chunk")
                return {"file": dict(row)}
            if op == "info":
                file_id = integer(data.get("id"), "file id")
                row = self.db.execute(
                    "SELECT id,area,name,description,source_url,sha256,original_date,"
                    "license_status,historical,encoding,source_identity,payload_sha256,"
                    "CASE WHEN original_size>0 THEN original_size ELSE length(content) END "
                    "AS original_size,attribution,retrieved_at,rights_evidence,"
                    "rendering_policy,rights_decision,content_decision,max_line_columns,"
                    "length(content) AS preview_length FROM files WHERE id=?", (file_id,)
                ).fetchone()
                if row is None:
                    raise RPCError("no such file")
                return {"file": dict(row)}
            if op == "read_chunk":
                file_id = integer(data.get("id"), "file id")
                offset = integer(data.get("offset", 0), "offset", 0, 10_000_000)
                limit = integer(data.get("limit", 12_000), "limit", 1, 12_000)
                row = self.db.execute(
                    "SELECT length(content) AS total,substr(content,? + 1,?) AS chunk "
                    "FROM files WHERE id=?", (offset, limit, file_id)).fetchone()
                if row is None:
                    raise RPCError("no such file")
                total = int(row["total"])
                if offset > total:
                    raise RPCError("offset beyond end of file")
                chunk = str(row["chunk"])
                next_offset = offset + len(chunk)
                return {"chunk": chunk, "offset": offset, "total": total,
                        "next_offset": next_offset, "eof": next_offset >= total}
            if op in ("export_info", "export_chunk"):
                file_id = integer(data.get("id"), "file id")
                row, payload = self._payload(file_id)
                if len(payload) > MAX_EXPORT_BYTES:
                    raise RPCError("file exceeds the terminal export limit")
                digest = hashlib.sha256(payload).hexdigest()
                if op == "export_info":
                    return {"name": row["name"], "size": len(payload), "sha256": digest,
                            "encoding": "base64", "chunk_bytes": 3072}
                offset = integer(data.get("offset", 0), "offset", 0, MAX_EXPORT_BYTES)
                if offset > len(payload):
                    raise RPCError("offset beyond end of file")
                chunk = payload[offset:offset + 3072]
                next_offset = offset + len(chunk)
                return {"data": base64.b64encode(chunk).decode("ascii"),
                        "offset": offset, "next_offset": next_offset,
                        "total": len(payload), "eof": next_offset >= len(payload)}
            if op == "issue_download":
                if set(data) != {"id"}:
                    raise RPCError("invalid download request")
                file_id = integer(data.get("id"), "file id")
                row, payload = self._payload(file_id)
                if len(payload) > MAX_DOWNLOAD_BYTES:
                    raise RPCError("file exceeds the SSH download limit")
                raw_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
                now = int(time.time())
                account_id = int(ident["account_id"])
                with self.db:
                    self._prune_downloads()
                    # Bound abandoned tickets without invalidating a user's
                    # newest five transfer attempts.
                    self.db.execute(
                        "DELETE FROM download_tickets WHERE account_id=? AND token_hash NOT IN "
                        "(SELECT token_hash FROM download_tickets WHERE account_id=? "
                        "ORDER BY expires_at DESC LIMIT 4)", (account_id, account_id))
                    self.db.execute(
                        "INSERT INTO download_tickets"
                        "(token_hash,file_id,account_id,payload_hash,payload_size,expires_at) "
                        "VALUES(?,?,?,?,?,?)", (token_hash, file_id, account_id,
                                                hashlib.sha256(payload).hexdigest(),
                                                len(payload), now + DOWNLOAD_TICKET_SECONDS))
                return {"ticket": raw_token, "name": row["name"],
                        "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                        "expires_seconds": DOWNLOAD_TICKET_SECONDS}
            if op == "catalog_areas":
                rows = self.db.execute(
                    "SELECT category,count(*) AS files,"
                    "sum(recovery_status='recovered_verified') AS recovered "
                    "FROM historical_catalog GROUP BY category "
                    "ORDER BY category COLLATE NOCASE").fetchall()
                return {"areas": [dict(row) for row in rows]}
            if op == "catalog_list":
                category = text(data.get("category", ""), "category", 64).strip()
                query = text(data.get("query", ""), "query", 64).strip()
                offset = integer(data.get("offset", 0), "offset", 0, 1_000_000)
                limit = integer(data.get("limit", 50), "limit", 1, 50)
                clauses: list[str] = []
                parameters: list[object] = []
                if category:
                    clauses.append("category=?")
                    parameters.append(category)
                if query:
                    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    clauses.append("filename LIKE ? ESCAPE '\\'")
                    parameters.append("%" + escaped + "%")
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                total = self.db.execute(
                    "SELECT count(*) FROM historical_catalog" + where,
                    parameters).fetchone()[0]
                order_parameters: list[object] = []
                order = " ORDER BY category COLLATE NOCASE,filename COLLATE NOCASE"
                if query:
                    # A precise historical filename should win over unrelated
                    # README/readme-suffixed substring matches. Root records
                    # win ties because they describe the archive itself.
                    order = (" ORDER BY CASE WHEN category='ROOT' AND "
                             "filename=? COLLATE NOCASE THEN 0 WHEN "
                             "filename=? COLLATE NOCASE THEN 1 ELSE 2 END,"
                             "category COLLATE NOCASE,"
                             "filename COLLATE NOCASE")
                    order_parameters.extend((query, query))
                rows = self.db.execute(
                    "SELECT id,category,filename,listed_size,listed_date,"
                    "recovery_status,published_file_id FROM historical_catalog" + where +
                    order + " LIMIT ? OFFSET ?",
                    parameters + order_parameters + [limit, offset]).fetchall()
                return {"files": [dict(row) for row in rows], "total": total,
                        "offset": offset, "next_offset": offset + len(rows)
                        if offset + len(rows) < total else None}
            if op == "catalog_read":
                catalog_id = integer(data.get("id"), "catalog id")
                row = self.db.execute(
                    "SELECT * FROM historical_catalog WHERE id=?", (catalog_id,)).fetchone()
                if row is None:
                    raise RPCError("no such catalog entry")
                return {"file": dict(row)}
        except ValueError as exc:
            raise RPCError(str(exc)) from exc
        raise RPCError("unknown operation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    args = parser.parse_args()
    serve(args.socket, Files(args.db, args.session_socket).handle)


if __name__ == "__main__":
    main()
