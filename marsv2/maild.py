"""Private-mail authority; mailbox ownership comes only from the session."""

from __future__ import annotations

import argparse
import json
import secrets
import time

from .db import connect
from .identity import validate
from .rpc import RPCError, request, serve
from .validate import integer, text, userid

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  sender_id INTEGER NOT NULL,
  sender_user TEXT NOT NULL,
  recipient_id INTEGER NOT NULL,
  recipient_user TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN (0,1)),
  recipient_deleted INTEGER NOT NULL DEFAULT 0 CHECK(recipient_deleted IN (0,1)),
  sender_deleted INTEGER NOT NULL DEFAULT 0 CHECK(sender_deleted IN (0,1))
  ,group_id TEXT NOT NULL DEFAULT ''
  ,recipient_list TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS mailbox_idx ON messages(recipient_id,id);
CREATE INDEX IF NOT EXISTS sent_idx ON messages(sender_id,id);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER NOT NULL,
  recipient_user TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_owner_idx ON drafts(owner_id,id);
"""

MAX_MESSAGES_PER_USER = 1000
MAX_DRAFTS_PER_USER = 50


class Mail:
    def __init__(self, db_path: str, session_socket: str, auth_socket: str):
        self.db = connect(db_path)
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        with self.db:
            if "recipient_deleted" not in columns:
                self.db.execute(
                    "ALTER TABLE messages ADD COLUMN recipient_deleted INTEGER NOT NULL DEFAULT 0")
            if "sender_deleted" not in columns:
                self.db.execute(
                    "ALTER TABLE messages ADD COLUMN sender_deleted INTEGER NOT NULL DEFAULT 0")
            if "group_id" not in columns:
                self.db.execute(
                    "ALTER TABLE messages ADD COLUMN group_id TEXT NOT NULL DEFAULT ''")
            if "recipient_list" not in columns:
                self.db.execute(
                    "ALTER TABLE messages ADD COLUMN recipient_list TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE INDEX IF NOT EXISTS sent_idx ON messages(sender_id,id)")
        self.session_socket = session_socket
        self.auth_socket = auth_socket

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
                if "account.delete" not in ident.get("capabilities", []):
                    raise RPCError("permission denied")
                account_id = integer(data.get("account_id"), "account id")
                with self.db:
                    drafts = self.db.execute(
                        "DELETE FROM drafts WHERE owner_id=?", (account_id,)).rowcount
                    inbox = self.db.execute(
                        "UPDATE messages SET recipient_deleted=1 "
                        "WHERE recipient_id=? AND recipient_deleted=0",
                        (account_id,)).rowcount
                    sent = self.db.execute(
                        "UPDATE messages SET sender_deleted=1 "
                        "WHERE sender_id=? AND sender_deleted=0",
                        (account_id,)).rowcount
                    self.db.execute(
                        "DELETE FROM messages WHERE recipient_deleted=1 AND sender_deleted=1")
                return {"drafts_deleted": drafts, "inbox_removed": inbox,
                        "sent_removed": sent}
            if op == "resolve_recipient":
                recipient = userid(data.get("recipient"))
                lookup = request(self.auth_socket, "mail_recipient", {"userid": recipient})
                if not lookup["ok"]:
                    raise RPCError("No BBS user has that userid.")
                return {"userid": lookup["data"]["userid"]}
            if op == "recipients":
                # Session validation above is the access boundary.  Auth owns
                # the directory; Mail only relays its non-sensitive fields.
                offset = data.get("offset", 0)
                directory = request(self.auth_socket, "directory", {"offset": offset})
                if not directory["ok"]:
                    raise RPCError("recipient directory unavailable")
                users = directory["data"].get("users")
                if not isinstance(users, list):
                    raise RPCError("invalid recipient directory")
                return {"users": users,
                        "next_offset": directory["data"].get("next_offset")}
            if op == "list":
                folder = data.get("folder", "inbox")
                if folder == "inbox":
                    rows = self.db.execute(
                        "SELECT id,sender_user,recipient_user,subject,created_at,is_read "
                        "FROM messages WHERE recipient_id=? AND recipient_deleted=0 "
                        "ORDER BY id DESC LIMIT 200", (ident["account_id"],)).fetchall()
                elif folder == "sent":
                    rows = self.db.execute(
                        "SELECT id,sender_user,recipient_user,subject,created_at,is_read "
                        "FROM messages WHERE sender_id=? AND sender_deleted=0 "
                        "ORDER BY id DESC LIMIT 200", (ident["account_id"],)).fetchall()
                elif folder == "drafts":
                    rows = self.db.execute(
                        "SELECT id,recipient_user,subject,updated_at FROM drafts "
                        "WHERE owner_id=? ORDER BY id DESC LIMIT 50",
                        (ident["account_id"],)).fetchall()
                    return {"messages": [dict(row) for row in rows]}
                else:
                    raise RPCError("invalid mail folder")
                return {"messages": [dict(row) for row in rows]}
            if op == "read":
                message_id = integer(data.get("id"), "message id")
                folder = data.get("folder", "inbox")
                if folder == "inbox":
                    row = self.db.execute(
                        "SELECT * FROM messages WHERE id=? AND recipient_id=? "
                        "AND recipient_deleted=0", (message_id, ident["account_id"])).fetchone()
                elif folder == "sent":
                    row = self.db.execute(
                        "SELECT * FROM messages WHERE id=? AND sender_id=? AND sender_deleted=0",
                        (message_id, ident["account_id"])).fetchone()
                else:
                    raise RPCError("invalid mail folder")
                if row is None:
                    raise RPCError("no such message")
                if folder == "inbox":
                    with self.db:
                        self.db.execute("UPDATE messages SET is_read=1 WHERE id=?", (message_id,))
                return {"message": dict(row)}
            if op == "read_draft":
                draft_id = integer(data.get("id"), "draft id")
                row = self.db.execute(
                    "SELECT id,recipient_user,subject,body,updated_at FROM drafts "
                    "WHERE id=? AND owner_id=?", (draft_id, ident["account_id"])).fetchone()
                if row is None:
                    raise RPCError("no such draft")
                return {"draft": dict(row)}
            if op == "save_draft":
                draft_id = data.get("id")
                recipient = text(data.get("recipient", ""), "recipient", 12).strip()
                if recipient:
                    recipient = userid(recipient)
                subject = text(data.get("subject", ""), "subject", 72)
                body = text(data.get("body", ""), "message", 8192, 0, True)
                now = int(time.time())
                with self.db:
                    if draft_id is None:
                        count = self.db.execute(
                            "SELECT count(*) FROM drafts WHERE owner_id=?",
                            (ident["account_id"],)).fetchone()[0]
                        if count >= MAX_DRAFTS_PER_USER:
                            raise RPCError("draft folder is full")
                        cursor = self.db.execute(
                            "INSERT INTO drafts(owner_id,recipient_user,subject,body,updated_at) "
                            "VALUES(?,?,?,?,?)",
                            (ident["account_id"], recipient, subject, body, now))
                        draft_id = cursor.lastrowid
                    else:
                        draft_id = integer(draft_id, "draft id")
                        cursor = self.db.execute(
                            "UPDATE drafts SET recipient_user=?,subject=?,body=?,updated_at=? "
                            "WHERE id=? AND owner_id=?",
                            (recipient, subject, body, now, draft_id, ident["account_id"]))
                        if cursor.rowcount != 1:
                            raise RPCError("no such draft")
                return {"id": draft_id}
            if op == "send":
                raw_recipients = data.get("recipients")
                if raw_recipients is None:
                    raw_recipients = [data.get("recipient")]
                if not isinstance(raw_recipients, list) or not raw_recipients \
                        or len(raw_recipients) > 16:
                    raise RPCError("group mail requires 1 to 16 recipients")
                recipients: list[str] = []
                for value in raw_recipients:
                    name = userid(value)
                    if name.casefold() not in {item.casefold() for item in recipients}:
                        recipients.append(name)
                subject = text(data.get("subject"), "subject", 72, 1)
                body = text(data.get("body"), "message", 8192, 1, True)
                targets: list[dict[str, object]] = []
                for recipient in recipients:
                    lookup = request(self.auth_socket, "mail_recipient", {"userid": recipient})
                    if not lookup["ok"]:
                        raise RPCError(f"no such recipient: {recipient}")
                    target = dict(lookup["data"])
                    count = self.db.execute(
                        "SELECT count(*) FROM messages WHERE recipient_id=? "
                        "AND recipient_deleted=0", (target["account_id"],)).fetchone()[0]
                    if count >= MAX_MESSAGES_PER_USER:
                        raise RPCError(f"mailbox is full: {target['userid']}")
                    targets.append(target)
                sent_count = self.db.execute(
                    "SELECT count(*) FROM messages WHERE sender_id=? AND sender_deleted=0",
                    (ident["account_id"],)).fetchone()[0]
                if sent_count + len(targets) > MAX_MESSAGES_PER_USER:
                    raise RPCError("sent folder is full; delete old sent mail")
                draft_id = data.get("draft_id")
                group_id = secrets.token_hex(8) if len(targets) > 1 else ""
                recipient_list = json.dumps(
                    [str(target["userid"]) for target in targets], separators=(",", ":"))
                message_ids: list[int] = []
                with self.db:
                    now = int(time.time())
                    for target in targets:
                        cursor = self.db.execute(
                            "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,"
                            "subject,body,created_at,group_id,recipient_list) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (ident["account_id"], ident["userid"], target["account_id"],
                             target["userid"], subject, body, now, group_id, recipient_list))
                        message_ids.append(int(cursor.lastrowid))
                    if draft_id is not None:
                        draft_id = integer(draft_id, "draft id")
                        removed = self.db.execute(
                            "DELETE FROM drafts WHERE id=? AND owner_id=?",
                            (draft_id, ident["account_id"]))
                        if removed.rowcount != 1:
                            raise RPCError("no such draft")
                return {"id": message_ids[0], "ids": message_ids,
                        "delivered": len(message_ids),
                        "recipients": [target["userid"] for target in targets]}
            if op == "delete":
                message_id = integer(data.get("id"), "message id")
                folder = data.get("folder", "inbox")
                if folder == "inbox":
                    statement = ("UPDATE messages SET recipient_deleted=1 "
                                 "WHERE id=? AND recipient_id=? AND recipient_deleted=0")
                elif folder == "sent":
                    statement = ("UPDATE messages SET sender_deleted=1 "
                                 "WHERE id=? AND sender_id=? AND sender_deleted=0")
                else:
                    raise RPCError("invalid mail folder")
                with self.db:
                    cursor = self.db.execute(statement, (message_id, ident["account_id"]))
                    self.db.execute(
                        "DELETE FROM messages WHERE id=? AND recipient_deleted=1 "
                        "AND sender_deleted=1", (message_id,))
                if cursor.rowcount != 1:
                    raise RPCError("no such message")
                return {"deleted": True}
            if op == "delete_draft":
                draft_id = integer(data.get("id"), "draft id")
                with self.db:
                    cursor = self.db.execute(
                        "DELETE FROM drafts WHERE id=? AND owner_id=?",
                        (draft_id, ident["account_id"]))
                if cursor.rowcount != 1:
                    raise RPCError("no such draft")
                return {"deleted": True}
        except ValueError as exc:
            raise RPCError(str(exc)) from exc
        raise RPCError("unknown operation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--auth-socket", required=True)
    args = parser.parse_args()
    serve(args.socket, Mail(args.db, args.session_socket, args.auth_socket).handle)


if __name__ == "__main__":
    main()
