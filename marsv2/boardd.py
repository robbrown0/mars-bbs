"""Board authority with server-side read/post/edit authorization."""

from __future__ import annotations

import argparse
import sqlite3
import time

from .authz import has
from .db import connect
from .identity import validate
from .rpc import RPCError, serve
from .validate import integer, text

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  description TEXT NOT NULL,
  read_only INTEGER NOT NULL DEFAULT 0 CHECK(read_only IN (0,1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
  no_zap INTEGER NOT NULL DEFAULT 0 CHECK(no_zap IN (0,1)),
  read_roles TEXT NOT NULL DEFAULT '',
  post_roles TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  author_id INTEGER NOT NULL,
  author_user TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  parent_id INTEGER REFERENCES posts(id),
  created_at INTEGER NOT NULL,
  edited_at INTEGER,
  protected INTEGER NOT NULL DEFAULT 0 CHECK(protected IN (0,1))
);
CREATE TABLE IF NOT EXISTS read_state (
  account_id INTEGER NOT NULL,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  last_post_id INTEGER NOT NULL,
  PRIMARY KEY(account_id,board_id)
);
CREATE INDEX IF NOT EXISTS posts_board_idx ON posts(board_id,id);
CREATE TABLE IF NOT EXISTS post_reads (
  account_id INTEGER NOT NULL,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  read_at INTEGER NOT NULL,
  PRIMARY KEY(account_id,post_id)
);
CREATE INDEX IF NOT EXISTS post_reads_account_idx ON post_reads(account_id,post_id);
CREATE TABLE IF NOT EXISTS board_preferences (
  account_id INTEGER NOT NULL,
  board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  scan_enabled INTEGER NOT NULL DEFAULT 1 CHECK(scan_enabled IN (0,1)),
  PRIMARY KEY(account_id,board_id)
);
CREATE TABLE IF NOT EXISTS board_managers (
  board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  userid TEXT NOT NULL COLLATE NOCASE,
  PRIMARY KEY(board_id,userid)
);
CREATE TABLE IF NOT EXISTS admin_audit (
  id INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor_id INTEGER NOT NULL,
  actor_user TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""


class Boards:
    def __init__(self, db_path: str, session_socket: str):
        self.db = connect(db_path)
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(boards)")}
        if "archived" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE boards ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        for name, definition in (
                ("no_zap", "INTEGER NOT NULL DEFAULT 0"),
                ("read_roles", "TEXT NOT NULL DEFAULT ''"),
                ("post_roles", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                with self.db:
                    self.db.execute(f"ALTER TABLE boards ADD COLUMN {name} {definition}")
        post_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(posts)")}
        if "protected" not in post_columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE posts ADD COLUMN protected INTEGER NOT NULL DEFAULT 0")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO boards(name,description) VALUES(?,?)",
                            ("General", "General discussion"))
            self.db.execute("INSERT OR IGNORE INTO boards(name,description) VALUES(?,?)",
                            ("Retro Computing", "Vintage systems and software"))
            # Preserve the old contiguous high-water mark as individual read
            # receipts.  The legacy table remains for rollback compatibility.
            self.db.execute(
                "INSERT OR IGNORE INTO post_reads(account_id,post_id,read_at) "
                "SELECT r.account_id,p.id,? FROM read_state r JOIN posts p "
                "ON p.board_id=r.board_id AND p.id<=r.last_post_id",
                (int(time.time()),))
        self.session_socket = session_socket

    def _board(self, board_id: object):
        value = integer(board_id, "board id")
        row = self.db.execute("SELECT * FROM boards WHERE id=?", (value,)).fetchone()
        if row is None:
            raise RPCError("no such board")
        return row

    @staticmethod
    def _role_set(value: object) -> set[str]:
        return {part.strip() for part in str(value or "").split(",") if part.strip()}

    def _can_manage(self, ident: dict[str, object], board_id: int) -> bool:
        if has(ident, "board.moderate"):
            return True
        return self.db.execute(
            "SELECT 1 FROM board_managers WHERE board_id=? AND userid=?",
            (board_id, ident["userid"])).fetchone() is not None

    def _require_access(self, ident: dict[str, object], board: sqlite3.Row,
                        writing: bool = False) -> None:
        if self._can_manage(ident, int(board["id"])):
            return
        required = self._role_set(board["post_roles"] if writing else board["read_roles"])
        if required and not required.intersection(set(ident.get("roles", []))):
            raise RPCError("permission denied")

    def _mark_read(self, account_id: int, post_id: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO post_reads(account_id,post_id,read_at) VALUES(?,?,?)",
            (account_id, post_id, int(time.time())))

    def _mark_board_visited(self, account_id: int, board_id: int) -> int:
        """Mark every current post read and retain the legacy high-water mark."""
        now = int(time.time())
        marked = self.db.execute(
            "INSERT OR IGNORE INTO post_reads(account_id,post_id,read_at) "
            "SELECT ?,id,? FROM posts WHERE board_id=?",
            (account_id, now, board_id)).rowcount
        latest = int(self.db.execute(
            "SELECT coalesce(max(id),0) FROM posts WHERE board_id=?",
            (board_id,)).fetchone()[0])
        if latest:
            self.db.execute(
                "INSERT INTO read_state(account_id,board_id,last_post_id) VALUES(?,?,?) "
                "ON CONFLICT(account_id,board_id) DO UPDATE SET "
                "last_post_id=max(last_post_id,excluded.last_post_id)",
                (account_id, board_id, latest))
        return marked

    def _audit(self, ident: dict[str, object], action: str,
               target: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO admin_audit(created_at,actor_id,actor_user,action,target,detail) "
            "VALUES(?,?,?,?,?,?)",
            (int(time.time()), ident["account_id"], ident["userid"], action,
             text(target, "audit target", 80), text(detail, "audit detail", 160)))

    def handle(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = req["op"]
        if op == "health":
            self.db.execute("SELECT 1").fetchone()
            return {"status": "ok"}
        ident = validate(self.session_socket, req.get("token"), peer[1])
        data = req["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        try:
            if op == "admin_delete_account":
                if not has(ident, "account.delete"):
                    raise RPCError("permission denied")
                account_id = integer(data.get("account_id"), "account id")
                account_user = text(data.get("userid", ""), "userid", 12)
                with self.db:
                    removed = self.db.execute(
                        "DELETE FROM read_state WHERE account_id=?", (account_id,)).rowcount
                    removed += self.db.execute(
                        "DELETE FROM post_reads WHERE account_id=?", (account_id,)).rowcount
                    removed += self.db.execute(
                        "DELETE FROM board_preferences WHERE account_id=?", (account_id,)).rowcount
                    if account_user:
                        removed += self.db.execute(
                            "DELETE FROM board_managers WHERE userid=?", (account_user,)).rowcount
                    # Public posts are historical community content. Preserve
                    # their original attribution while removing the deleted
                    # account's private per-board state and future authority.
                    retained = self.db.execute(
                        "SELECT count(*) FROM posts WHERE author_id=?",
                        (account_id,)).fetchone()[0]
                    self._audit(ident, "account.delete_cleanup", str(account_id),
                                f"read_state={removed},posts_retained={retained}")
                return {"read_state_deleted": removed, "posts_retained": retained}
            if op == "boards":
                rows = self.db.execute(
                    "SELECT b.id,b.name,b.description,b.read_only,b.no_zap,b.read_roles,b.post_roles,"
                    "count(p.id) AS posts,coalesce(max(p.id),0) AS latest_post,"
                    "coalesce(sum(CASE WHEN p.id IS NOT NULL AND pr.post_id IS NULL "
                    "THEN 1 ELSE 0 END),0) AS unread,"
                    "coalesce(bp.scan_enabled,1) AS scan_enabled "
                    "FROM boards b "
                    "LEFT JOIN posts p ON p.board_id=b.id "
                    "LEFT JOIN post_reads pr ON pr.post_id=p.id AND pr.account_id=? "
                    "LEFT JOIN board_preferences bp ON bp.board_id=b.id AND bp.account_id=? "
                    "WHERE b.archived=0 GROUP BY b.id ORDER BY b.id",
                    (ident["account_id"], ident["account_id"])).fetchall()
                visible = []
                for row in rows:
                    try:
                        self._require_access(ident, row)
                    except RPCError:
                        continue
                    item = dict(row)
                    item["can_manage"] = self._can_manage(ident, int(row["id"]))
                    visible.append(item)
                return {"boards": visible}
            if op == "list":
                board = self._board(data.get("board_id"))
                if board["archived"]:
                    raise RPCError("board is archived")
                self._require_access(ident, board)
                rows = self.db.execute(
                    "SELECT p.id,p.author_user,p.subject,p.parent_id,p.created_at,p.edited_at,"
                    "p.protected,CASE WHEN pr.post_id IS NULL THEN 0 ELSE 1 END AS is_read "
                    "FROM posts p LEFT JOIN post_reads pr ON pr.post_id=p.id AND pr.account_id=? "
                    "WHERE p.board_id=? ORDER BY p.id LIMIT 500",
                    (ident["account_id"], board["id"])).fetchall()
                info = dict(board)
                info["can_manage"] = self._can_manage(ident, int(board["id"]))
                return {"board": info, "posts": [dict(row) for row in rows]}
            if op == "scan_new":
                rows = self.db.execute(
                    "SELECT p.id,p.board_id,b.name AS board_name,p.author_user,p.subject,"
                    "p.parent_id,p.created_at,p.edited_at,p.protected "
                    "FROM posts p JOIN boards b ON b.id=p.board_id "
                    "LEFT JOIN post_reads pr ON pr.post_id=p.id AND pr.account_id=? "
                    "LEFT JOIN board_preferences bp ON bp.board_id=b.id AND bp.account_id=? "
                    "WHERE b.archived=0 AND pr.post_id IS NULL AND coalesce(bp.scan_enabled,1)=1 "
                    "ORDER BY b.id,p.id LIMIT 500",
                    (ident["account_id"], ident["account_id"])).fetchall()
                visible = []
                for row in rows:
                    board = self._board(row["board_id"])
                    try:
                        self._require_access(ident, board)
                    except RPCError:
                        continue
                    visible.append(dict(row))
                return {"posts": visible}
            if op == "read":
                post_id = integer(data.get("id"), "post id")
                row = self.db.execute(
                    "SELECT p.*,b.name AS board_name,b.archived AS board_archived "
                    "FROM posts p JOIN boards b ON b.id=p.board_id "
                    "WHERE p.id=?", (post_id,)).fetchone()
                if row is None:
                    raise RPCError("no such post")
                if row["board_archived"]:
                    raise RPCError("board is archived")
                board = self._board(row["board_id"])
                self._require_access(ident, board)
                with self.db:
                    self._mark_read(int(ident["account_id"]), post_id)
                    self.db.execute(
                        "INSERT INTO read_state(account_id,board_id,last_post_id) VALUES(?,?,?) "
                        "ON CONFLICT(account_id,board_id) DO UPDATE SET last_post_id=max(last_post_id,excluded.last_post_id)",
                        (ident["account_id"], row["board_id"], post_id))
                return {"post": dict(row)}
            if op == "set_scan":
                board = self._board(data.get("board_id"))
                self._require_access(ident, board)
                enabled = data.get("enabled")
                if not isinstance(enabled, bool):
                    raise RPCError("invalid scan setting")
                if not enabled and bool(board["no_zap"]):
                    raise RPCError("this board must remain in the New scan")
                with self.db:
                    self.db.execute(
                        "INSERT INTO board_preferences(account_id,board_id,scan_enabled) VALUES(?,?,?) "
                        "ON CONFLICT(account_id,board_id) DO UPDATE SET scan_enabled=excluded.scan_enabled",
                        (ident["account_id"], board["id"], int(enabled)))
                return {"board_id": board["id"], "scan_enabled": enabled}
            if op in ("mark_board_read", "mark_all_read"):
                board_id = None
                if op == "mark_board_read":
                    board = self._board(data.get("board_id"))
                    self._require_access(ident, board)
                    board_id = int(board["id"])
                with self.db:
                    if board_id is None:
                        boards = self.db.execute(
                            "SELECT * FROM boards WHERE archived=0 ORDER BY id").fetchall()
                        count = 0
                        for candidate in boards:
                            try:
                                self._require_access(ident, candidate)
                            except RPCError:
                                continue
                            count += self._mark_board_visited(
                                int(ident["account_id"]), int(candidate["id"]))
                    else:
                        count = self._mark_board_visited(
                            int(ident["account_id"]), board_id)
                return {"marked": count}
            if op in ("post", "reply"):
                board = self._board(data.get("board_id"))
                if board["archived"]:
                    raise RPCError("board is archived")
                self._require_access(ident, board, writing=True)
                if board["read_only"] and not has(ident, "board.manage"):
                    raise RPCError("board is read-only")
                subject = text(data.get("subject"), "subject", 72, 1)
                body = text(data.get("body"), "post", 16384, 1, True)
                parent_id = None
                if op == "reply":
                    parent_id = integer(data.get("parent_id"), "parent id")
                    parent = self.db.execute(
                        "SELECT id FROM posts WHERE id=? AND board_id=?",
                        (parent_id, board["id"])).fetchone()
                    if parent is None:
                        raise RPCError("no such parent post")
                with self.db:
                    cursor = self.db.execute(
                        "INSERT INTO posts(board_id,author_id,author_user,subject,body,parent_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?)", (board["id"], ident["account_id"], ident["userid"],
                                                  subject, body, parent_id, int(time.time())))
                return {"id": cursor.lastrowid}
            if op == "edit":
                post_id = integer(data.get("id"), "post id")
                subject = text(data.get("subject"), "subject", 72, 1)
                body = text(data.get("body"), "post", 16384, 1, True)
                row = self.db.execute(
                    "SELECT author_id,board_id FROM posts WHERE id=?", (post_id,)).fetchone()
                if row is None:
                    raise RPCError("no such post")
                if row["author_id"] != ident["account_id"] and not self._can_manage(
                        ident, int(row["board_id"])):
                    raise RPCError("permission denied")
                with self.db:
                    self.db.execute("UPDATE posts SET subject=?,body=?,edited_at=? WHERE id=?",
                                    (subject, body, int(time.time()), post_id))
                return {"edited": True}
            if op in ("delete", "move", "protect"):
                post_id = integer(data.get("id"), "post id")
                post = self.db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
                if post is None:
                    raise RPCError("no such post")
                manager = self._can_manage(ident, int(post["board_id"]))
                owner = int(post["author_id"]) == int(ident["account_id"])
                if op == "protect":
                    if not manager:
                        raise RPCError("permission denied")
                    protected = data.get("protected")
                    if not isinstance(protected, bool):
                        raise RPCError("invalid protection setting")
                    with self.db:
                        self.db.execute("UPDATE posts SET protected=? WHERE id=?",
                                        (int(protected), post_id))
                    return {"id": post_id, "protected": protected}
                if not (owner or manager):
                    raise RPCError("permission denied")
                if bool(post["protected"]) and not manager:
                    raise RPCError("post is protected")
                if op == "move":
                    destination = self._board(data.get("board_id"))
                    self._require_access(ident, destination, writing=True)
                    with self.db:
                        # A reply chain cannot safely span boards.  Moving the
                        # post therefore promotes it to a new thread in the
                        # destination instead of leaving a cross-board parent.
                        self.db.execute("UPDATE posts SET board_id=?, parent_id=NULL WHERE id=?",
                                        (destination["id"], post_id))
                    return {"id": post_id, "board_id": destination["id"]}
                with self.db:
                    self.db.execute("UPDATE posts SET parent_id=NULL WHERE parent_id=?", (post_id,))
                    self.db.execute("DELETE FROM posts WHERE id=?", (post_id,))
                return {"deleted": True, "id": post_id}
            if op == "admin_managers":
                if not has(ident, "board.manage"):
                    raise RPCError("permission denied")
                board = self._board(data.get("board_id"))
                if "managers" not in data:
                    rows = self.db.execute(
                        "SELECT userid FROM board_managers WHERE board_id=? ORDER BY userid",
                        (board["id"],)).fetchall()
                    return {"board_id": board["id"],
                            "managers": [str(row["userid"]) for row in rows]}
                managers = data.get("managers")
                if not isinstance(managers, list) or len(managers) > 32:
                    raise RPCError("invalid manager list")
                names = sorted({text(value, "manager userid", 12, 1) for value in managers},
                               key=str.casefold)
                with self.db:
                    self.db.execute("DELETE FROM board_managers WHERE board_id=?", (board["id"],))
                    self.db.executemany(
                        "INSERT INTO board_managers(board_id,userid) VALUES(?,?)",
                        [(board["id"], value) for value in names])
                    self._audit(ident, "board.managers", str(board["name"]),
                                "managers=" + ",".join(names))
                return {"board_id": board["id"], "managers": names}
            if op == "admin_boards":
                if not has(ident, "board.manage"):
                    raise RPCError("permission denied")
                rows = self.db.execute(
                    "SELECT b.id,b.name,b.description,b.read_only,b.archived,b.no_zap,"
                    "b.read_roles,b.post_roles,count(p.id) posts "
                    "FROM boards b LEFT JOIN posts p ON p.board_id=b.id "
                    "GROUP BY b.id ORDER BY b.id").fetchall()
                return {"boards": [dict(row) for row in rows]}
            if op == "admin_create":
                if not has(ident, "board.manage"):
                    raise RPCError("permission denied")
                name = text(data.get("name"), "board name", 40, 1)
                description = text(data.get("description"), "board description", 100, 1)
                read_only = data.get("read_only", False)
                no_zap = data.get("no_zap", False)
                if not isinstance(read_only, bool) or not isinstance(no_zap, bool):
                    raise RPCError("invalid read-only setting")
                try:
                    with self.db:
                        cursor = self.db.execute(
                            "INSERT INTO boards(name,description,read_only,no_zap) VALUES(?,?,?,?)",
                            (name, description, int(read_only), int(no_zap)))
                        self._audit(ident, "board.create", name,
                                    "read_only=" + str(read_only).lower())
                except sqlite3.IntegrityError as exc:
                    raise RPCError("board name unavailable") from exc
                return {"id": cursor.lastrowid, "name": name}
            if op == "admin_update":
                if not has(ident, "board.manage"):
                    raise RPCError("permission denied")
                board = self._board(data.get("board_id"))
                name = text(data.get("name"), "board name", 40, 1)
                description = text(data.get("description"), "board description", 100, 1)
                read_only = data.get("read_only")
                archived = data.get("archived")
                no_zap = data.get("no_zap", bool(board["no_zap"]))
                read_roles = text(data.get("read_roles", board["read_roles"]),
                                  "read roles", 160)
                post_roles = text(data.get("post_roles", board["post_roles"]),
                                  "post roles", 160)
                valid_roles = set(ident.get("roles", [])) | {"Basic-1", "AccountMgr", "BoardMgr", "Sysop"}
                if (self._role_set(read_roles) - valid_roles or
                        self._role_set(post_roles) - valid_roles):
                    raise RPCError("unknown board role")
                if (not isinstance(read_only, bool) or not isinstance(archived, bool)
                        or not isinstance(no_zap, bool)):
                    raise RPCError("invalid board setting")
                try:
                    with self.db:
                        self.db.execute(
                            "UPDATE boards SET name=?,description=?,read_only=?,archived=?,no_zap=?,"
                            "read_roles=?,post_roles=? WHERE id=?",
                            (name, description, int(read_only), int(archived), int(no_zap),
                             read_roles, post_roles, board["id"]))
                        self._audit(
                            ident, "board.update", name,
                            f"read_only={str(read_only).lower()},archived={str(archived).lower()}")
                except sqlite3.IntegrityError as exc:
                    raise RPCError("board name unavailable") from exc
                return {"id": board["id"], "name": name, "read_only": read_only,
                        "archived": archived, "no_zap": no_zap,
                        "read_roles": read_roles, "post_roles": post_roles}
            if op == "admin_audit":
                if not has(ident, "audit.read"):
                    raise RPCError("permission denied")
                rows = self.db.execute(
                    "SELECT created_at,actor_user,action,target,detail "
                    "FROM admin_audit ORDER BY id DESC LIMIT 200").fetchall()
                return {"events": [dict(row) for row in rows]}
        except ValueError as exc:
            raise RPCError(str(exc)) from exc
        raise RPCError("unknown operation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    args = parser.parse_args()
    serve(args.socket, Boards(args.db, args.session_socket).handle)


if __name__ == "__main__":
    main()
