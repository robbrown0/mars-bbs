"""Private-mail authority; mailbox ownership comes only from the session."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time

from .db import connect
from .federation import (bbs_id, bbs_name, certificate_fingerprint,
                         decode_invitation, delete_peer_secret, encode_invitation,
                         endpoint, generate_secret, read_peer_secret,
                         remote_address, write_peer_secret)
from .identity import validate
from .rpc import RPCError, request, serve
from .validate import integer, text

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
CREATE TABLE IF NOT EXISTS site_config (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  bbs_name TEXT NOT NULL,
  bbs_id TEXT NOT NULL,
  public_endpoint TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_peers (
  bbs_id TEXT PRIMARY KEY COLLATE NOCASE,
  bbs_name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  certificate_sha256 TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_outbound (
  id INTEGER PRIMARY KEY,
  message_uuid TEXT NOT NULL UNIQUE,
  message_id INTEGER NOT NULL,
  sender_id INTEGER NOT NULL,
  sender_user TEXT NOT NULL,
  peer_id TEXT NOT NULL,
  recipient_user TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS federation_outbound_queue_idx
  ON federation_outbound(state,next_attempt,id);
CREATE INDEX IF NOT EXISTS federation_outbound_sender_idx
  ON federation_outbound(sender_id,created_at);
CREATE TABLE IF NOT EXISTS federation_deliveries (
  peer_id TEXT NOT NULL,
  message_uuid TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY(peer_id,message_uuid)
);
CREATE TABLE IF NOT EXISTS federation_nonces (
  peer_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  seen_at INTEGER NOT NULL,
  PRIMARY KEY(peer_id,nonce)
);
CREATE TABLE IF NOT EXISTS mail_admin_audit (
  id INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor_user TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""

MAX_MESSAGES_PER_USER = 1000
MAX_DRAFTS_PER_USER = 50
MAX_REMOTE_MESSAGES_PER_HOUR = 20


class Mail:
    def __init__(self, db_path: str, session_socket: str, auth_socket: str,
                 peer_secret_dir: str | None = None,
                 certificate_path: str | None = None):
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
            self.db.execute(
                "INSERT OR IGNORE INTO site_config(singleton,bbs_name,bbs_id,public_endpoint,updated_at) "
                "VALUES(1,'The Mars Hotel','mars-hotel','',?)", (int(time.time()),))
        self.session_socket = session_socket
        self.auth_socket = auth_socket
        self.peer_secret_dir = peer_secret_dir or os.path.join(
            os.path.dirname(db_path), "peer-secrets")
        os.makedirs(self.peer_secret_dir, mode=0o700, exist_ok=True)
        os.chmod(self.peer_secret_dir, 0o700)
        self.certificate_sha256 = ""
        if certificate_path:
            try:
                self.certificate_sha256 = certificate_fingerprint(certificate_path)
            except (OSError, UnicodeError, ValueError):
                self.certificate_sha256 = ""

    def _site(self) -> dict[str, object]:
        row = self.db.execute(
            "SELECT bbs_name,bbs_id,public_endpoint,updated_at FROM site_config "
            "WHERE singleton=1").fetchone()
        if row is None:
            raise RPCError("BBS identity is unavailable")
        return dict(row)

    @staticmethod
    def _require(identity: dict[str, object], capability: str) -> None:
        if capability not in identity.get("capabilities", []):
            raise RPCError("permission denied")

    def _audit(self, identity: dict[str, object], action: str, target: str,
               detail: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO mail_admin_audit(created_at,actor_user,action,target,detail) "
                "VALUES(?,?,?,?,?)", (int(time.time()), identity["userid"],
                                      text(action, "audit action", 48, 1),
                                      text(target, "audit target", 80, 1),
                                      text(detail, "audit detail", 160)))

    def _public_identity(self) -> dict[str, object]:
        site = self._site()
        return {"bbs_name": site["bbs_name"], "bbs_id": site["bbs_id"],
                "federation_enabled": bool(site["public_endpoint"]
                                           and self.certificate_sha256)}

    def _invitation_identity(self) -> dict[str, str]:
        site = self._site()
        if not site["public_endpoint"]:
            raise RPCError("configure the public federation endpoint first")
        if not self.certificate_sha256:
            raise RPCError("federation TLS certificate is unavailable")
        return {"bbs_name": str(site["bbs_name"]), "bbs_id": str(site["bbs_id"]),
                "endpoint": str(site["public_endpoint"]),
                "certificate_sha256": self.certificate_sha256}

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
            if op == "site_identity":
                return self._public_identity()
            if op == "admin_site":
                self._require(ident, "federation.manage")
                site = self._site()
                return {**site, "certificate_sha256": self.certificate_sha256,
                        "ready": bool(site["public_endpoint"]
                                      and self.certificate_sha256)}
            if op == "admin_site_update":
                self._require(ident, "federation.manage")
                current = self._site()
                name = bbs_name(data.get("bbs_name"))
                network_id = bbs_id(data.get("bbs_id"))
                raw_endpoint = data.get("public_endpoint", "")
                public_endpoint = endpoint(raw_endpoint) if raw_endpoint else ""
                if network_id != current["bbs_id"]:
                    linked = self.db.execute(
                        "SELECT count(*) FROM federation_peers").fetchone()[0]
                    exchanged = self.db.execute(
                        "SELECT count(*) FROM federation_outbound").fetchone()[0] + \
                        self.db.execute(
                            "SELECT count(*) FROM federation_deliveries").fetchone()[0]
                    if linked or exchanged:
                        raise RPCError(
                            "unlink peers and clear exchange history before changing the BBS network ID")
                now = int(time.time())
                with self.db:
                    self.db.execute(
                        "UPDATE site_config SET bbs_name=?,bbs_id=?,public_endpoint=?,updated_at=? "
                        "WHERE singleton=1", (name, network_id, public_endpoint, now))
                self._audit(ident, "federation.site.update", network_id,
                            f"name={name}; endpoint_configured={str(bool(public_endpoint)).lower()}")
                return {"bbs_name": name, "bbs_id": network_id,
                        "public_endpoint": public_endpoint,
                        "certificate_sha256": self.certificate_sha256,
                        "ready": bool(public_endpoint and self.certificate_sha256)}
            if op == "admin_peers":
                self._require(ident, "federation.manage")
                rows = self.db.execute(
                    "SELECT p.bbs_id,p.bbs_name,p.endpoint,p.certificate_sha256,p.enabled,"
                    "p.created_at,p.updated_at,"
                    "sum(CASE WHEN o.state IN ('queued','retry') THEN 1 ELSE 0 END) queued,"
                    "sum(CASE WHEN o.state='failed' THEN 1 ELSE 0 END) failed "
                    "FROM federation_peers p LEFT JOIN federation_outbound o "
                    "ON o.peer_id=p.bbs_id GROUP BY p.bbs_id ORDER BY p.bbs_name COLLATE NOCASE"
                ).fetchall()
                return {"peers": [{**dict(row), "enabled": bool(row["enabled"]),
                                    "queued": int(row["queued"] or 0),
                                    "failed": int(row["failed"] or 0)} for row in rows]}
            if op == "admin_create_invitation":
                self._require(ident, "federation.manage")
                invitation = encode_invitation(self._invitation_identity(), generate_secret())
                self._audit(ident, "federation.invitation.create",
                            str(self._site()["bbs_id"]), "secret shown once")
                return {"invitation": invitation}
            if op == "admin_import_invitation":
                self._require(ident, "federation.manage")
                peer, secret = decode_invitation(data.get("invitation"))
                site = self._site()
                if peer["bbs_id"] == site["bbs_id"]:
                    raise RPCError("cannot link this BBS to itself")
                existing = self.db.execute(
                    "SELECT 1 FROM federation_peers WHERE bbs_id=?", (peer["bbs_id"],)).fetchone()
                replace = data.get("replace", False)
                if not isinstance(replace, bool):
                    raise RPCError("invalid replacement flag")
                if existing is not None and not replace:
                    raise RPCError("peer already exists; explicitly replace it to rotate the link")
                write_peer_secret(self.peer_secret_dir, peer["bbs_id"], secret)
                now = int(time.time())
                with self.db:
                    self.db.execute(
                        "INSERT INTO federation_peers(bbs_id,bbs_name,endpoint,certificate_sha256,"
                        "enabled,created_at,updated_at) VALUES(?,?,?,?,1,?,?) "
                        "ON CONFLICT(bbs_id) DO UPDATE SET bbs_name=excluded.bbs_name,"
                        "endpoint=excluded.endpoint,certificate_sha256=excluded.certificate_sha256,"
                        "enabled=1,updated_at=excluded.updated_at",
                        (peer["bbs_id"], peer["bbs_name"], peer["endpoint"],
                         peer["certificate_sha256"], now, now))
                self._audit(ident, "federation.peer.import", peer["bbs_id"],
                            f"name={peer['bbs_name']}; replaced={str(existing is not None).lower()}")
                return {"bbs_id": peer["bbs_id"], "bbs_name": peer["bbs_name"],
                        "enabled": True}
            if op == "admin_peer_invitation":
                self._require(ident, "federation.manage")
                peer_id = bbs_id(data.get("bbs_id"))
                peer = self.db.execute(
                    "SELECT bbs_id FROM federation_peers WHERE bbs_id=?", (peer_id,)).fetchone()
                if peer is None:
                    raise RPCError("no such peer")
                secret = read_peer_secret(self.peer_secret_dir, peer_id)
                invitation = encode_invitation(self._invitation_identity(), secret)
                self._audit(ident, "federation.invitation.reciprocal", peer_id,
                            "secret shown once")
                return {"invitation": invitation}
            if op == "admin_peer_enabled":
                self._require(ident, "federation.manage")
                peer_id = bbs_id(data.get("bbs_id"))
                enabled = data.get("enabled")
                if not isinstance(enabled, bool):
                    raise RPCError("invalid peer status")
                with self.db:
                    cursor = self.db.execute(
                        "UPDATE federation_peers SET enabled=?,updated_at=? WHERE bbs_id=?",
                        (int(enabled), int(time.time()), peer_id))
                if cursor.rowcount != 1:
                    raise RPCError("no such peer")
                self._audit(ident, "federation.peer.enable" if enabled
                            else "federation.peer.disable", peer_id)
                return {"bbs_id": peer_id, "enabled": enabled}
            if op == "admin_peer_delete":
                self._require(ident, "federation.manage")
                peer_id = bbs_id(data.get("bbs_id"))
                pending = self.db.execute(
                    "SELECT count(*) FROM federation_outbound WHERE peer_id=? "
                    "AND state IN ('queued','retry')", (peer_id,)).fetchone()[0]
                if pending:
                    raise RPCError("disable the peer or allow queued mail to finish before unlinking")
                with self.db:
                    cursor = self.db.execute(
                        "DELETE FROM federation_peers WHERE bbs_id=?", (peer_id,))
                    self.db.execute("DELETE FROM federation_nonces WHERE peer_id=?", (peer_id,))
                if cursor.rowcount != 1:
                    raise RPCError("no such peer")
                delete_peer_secret(self.peer_secret_dir, peer_id)
                self._audit(ident, "federation.peer.delete", peer_id)
                return {"deleted": True, "bbs_id": peer_id}
            if op == "admin_mail_audit":
                self._require(ident, "audit.read")
                rows = self.db.execute(
                    "SELECT created_at,actor_user,action,target,'success' result,detail "
                    "FROM mail_admin_audit ORDER BY id DESC LIMIT 200").fetchall()
                return {"events": [dict(row) for row in rows]}
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
                        "UPDATE federation_outbound SET state='cancelled',last_error='account deleted' "
                        "WHERE sender_id=? AND state IN ('queued','retry')", (account_id,))
                    self.db.execute(
                        "DELETE FROM messages WHERE recipient_deleted=1 AND sender_deleted=1")
                return {"drafts_deleted": drafts, "inbox_removed": inbox,
                        "sent_removed": sent}
            if op == "resolve_recipient":
                local_user, peer_id, recipient = remote_address(
                    data.get("recipient"), str(self._site()["bbs_id"]))
                if peer_id is None:
                    lookup = request(self.auth_socket, "mail_recipient", {"userid": local_user})
                    if not lookup["ok"]:
                        raise RPCError("No BBS user has that userid.")
                    return {"userid": lookup["data"]["userid"], "remote": False}
                linked = self.db.execute(
                    "SELECT bbs_id FROM federation_peers WHERE bbs_id=? AND enabled=1",
                    (peer_id,)).fetchone()
                if linked is None:
                    raise RPCError("No enabled BBS link has that network ID.")
                return {"userid": recipient, "remote": True, "bbs_id": peer_id}
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
                peers = []
                if offset == 0:
                    peers = [dict(row) for row in self.db.execute(
                        "SELECT bbs_id,bbs_name FROM federation_peers WHERE enabled=1 "
                        "ORDER BY bbs_name COLLATE NOCASE").fetchall()]
                return {"users": users, "peers": peers,
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
                        "SELECT m.id,m.sender_user,m.recipient_user,m.subject,m.created_at,m.is_read,"
                        "COALESCE(o.state,'delivered') exchange_state "
                        "FROM messages m LEFT JOIN federation_outbound o ON o.message_id=m.id "
                        "WHERE m.sender_id=? AND m.sender_deleted=0 "
                        "ORDER BY m.id DESC LIMIT 200", (ident["account_id"],)).fetchall()
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
                        "SELECT m.*,COALESCE(o.state,'delivered') exchange_state "
                        "FROM messages m LEFT JOIN federation_outbound o ON o.message_id=m.id "
                        "WHERE m.id=? AND m.sender_id=? AND m.sender_deleted=0",
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
                recipient = text(data.get("recipient", ""), "recipient", 45).strip()
                if recipient:
                    _user, _peer, recipient = remote_address(
                        recipient, str(self._site()["bbs_id"]))
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
                    _name, _peer, address = remote_address(
                        value, str(self._site()["bbs_id"]))
                    if address.casefold() not in {item.casefold() for item in recipients}:
                        recipients.append(address)
                subject = text(data.get("subject"), "subject", 72, 1)
                body = text(data.get("body"), "message", 8192, 1, True)
                targets: list[dict[str, object]] = []
                for recipient in recipients:
                    local_user, peer_id, address = remote_address(
                        recipient, str(self._site()["bbs_id"]))
                    if peer_id is None:
                        lookup = request(self.auth_socket, "mail_recipient", {"userid": local_user})
                        if not lookup["ok"]:
                            raise RPCError(f"no such recipient: {recipient}")
                        target = dict(lookup["data"])
                        count = self.db.execute(
                            "SELECT count(*) FROM messages WHERE recipient_id=? "
                            "AND recipient_deleted=0", (target["account_id"],)).fetchone()[0]
                        if count >= MAX_MESSAGES_PER_USER:
                            raise RPCError(f"mailbox is full: {target['userid']}")
                        targets.append({**target, "kind": "local", "address": target["userid"]})
                    else:
                        linked = self.db.execute(
                            "SELECT bbs_id FROM federation_peers WHERE bbs_id=? AND enabled=1",
                            (peer_id,)).fetchone()
                        if linked is None:
                            raise RPCError(f"no enabled BBS link for recipient: {recipient}")
                        recent = self.db.execute(
                            "SELECT count(*) FROM federation_outbound WHERE sender_id=? "
                            "AND created_at>=?", (ident["account_id"],
                                                 int(time.time()) - 3600)).fetchone()[0]
                        requested_remote = sum(
                            1 for target in targets if target.get("kind") == "remote") + 1
                        if recent + requested_remote > MAX_REMOTE_MESSAGES_PER_HOUR:
                            raise RPCError("remote BBS mail rate limit reached; try again later")
                        targets.append({"kind": "remote", "userid": local_user,
                                        "peer_id": peer_id, "address": address,
                                        "account_id": 0})
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
                remote_ids: list[int] = []
                with self.db:
                    now = int(time.time())
                    for target in targets:
                        remote = target["kind"] == "remote"
                        cursor = self.db.execute(
                            "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,"
                            "subject,body,created_at,recipient_deleted,group_id,recipient_list) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (ident["account_id"], ident["userid"], target["account_id"],
                             target["address"], subject, body, now, int(remote),
                             group_id, recipient_list))
                        message_ids.append(int(cursor.lastrowid))
                        if remote:
                            remote_ids.append(int(cursor.lastrowid))
                            self.db.execute(
                                "INSERT INTO federation_outbound(message_uuid,message_id,sender_id,"
                                "sender_user,peer_id,recipient_user,subject,body,created_at,state,"
                                "next_attempt) VALUES(?,?,?,?,?,?,?,?,?,'queued',?)",
                                (secrets.token_hex(16), cursor.lastrowid, ident["account_id"],
                                 ident["userid"], target["peer_id"], target["userid"],
                                 subject, body, now, now))
                    if draft_id is not None:
                        draft_id = integer(draft_id, "draft id")
                        removed = self.db.execute(
                            "DELETE FROM drafts WHERE id=? AND owner_id=?",
                            (draft_id, ident["account_id"]))
                        if removed.rowcount != 1:
                            raise RPCError("no such draft")
                return {"id": message_ids[0], "ids": message_ids,
                        "delivered": len(message_ids) - len(remote_ids),
                        "queued": len(remote_ids),
                        "recipients": [target["address"] for target in targets]}
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
    parser.add_argument("--peer-secret-dir")
    parser.add_argument("--certificate")
    args = parser.parse_args()
    serve(args.socket, Mail(args.db, args.session_socket, args.auth_socket,
                            args.peer_secret_dir, args.certificate).handle)


if __name__ == "__main__":
    main()
