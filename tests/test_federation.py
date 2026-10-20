"""Security and behavior tests for trusted BBS-to-BBS text mail."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import marsv2.federationd as exchange_module
import marsv2.maild as mail_module
from marsv2.federation import (decode_invitation, encode_invitation, generate_secret,
                               signature)
from marsv2.federationd import Exchange, ExchangeError
from marsv2.maild import Mail
from marsv2.rpc import RPCError


def packet(op: str, data: dict[str, object] | None = None,
           token: str = "token") -> dict[str, object]:
    return {"op": op, "data": data or {}, "token": token}


class FederationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mars-federation-")
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "mail.db"
        self.secret_dir = self.root / "peer-secrets"
        self.mail = Mail(str(self.db_path), "unused", "unused", str(self.secret_dir))
        self.mail.certificate_sha256 = "a" * 64
        self.identities = {
            "admin": {"account_id": 1, "userid": "admin", "is_admin": True,
                      "roles": ["Sysop"],
                      "capabilities": ["federation.manage", "audit.read",
                                       "account.delete"]},
            "alice": {"account_id": 2, "userid": "alice", "is_admin": False,
                      "roles": ["Basic-1"], "capabilities": []},
            "bob": {"account_id": 3, "userid": "bob", "is_admin": False,
                    "roles": ["Basic-1"], "capabilities": []},
        }
        self.validate_patch = patch.object(
            mail_module, "validate", side_effect=lambda _socket, token, _uid:
            self.identities[str(token)])
        self.validate_patch.start()

    def tearDown(self) -> None:
        self.validate_patch.stop()
        self.mail.db.close()
        self.temporary.cleanup()

    def _configure(self) -> None:
        self.mail.handle(packet("admin_site_update", {
            "bbs_name": "Station Alpha", "bbs_id": "alpha",
            "public_endpoint": "https://alpha.example:2443",
        }, "admin"), (1, 30001, 30001))

    def _import_beta(self, secret: bytes | None = None) -> bytes:
        secret = secret or generate_secret()
        invitation = encode_invitation({
            "bbs_name": "Station Beta", "bbs_id": "beta",
            "endpoint": "https://beta.example:2443",
            "certificate_sha256": "b" * 64,
        }, secret)
        result = self.mail.handle(packet("admin_import_invitation", {
            "invitation": invitation,
        }, "admin"), (1, 30001, 30001))
        self.assertEqual(result["bbs_id"], "beta")
        return secret

    def test_admin_names_bbs_and_pairing_secret_is_not_returned(self) -> None:
        self._configure()
        public = self.mail.handle(packet("site_identity", token="alice"),
                                  (2, 30002, 30002))
        self.assertEqual(public, {"bbs_name": "Station Alpha", "bbs_id": "alpha",
                                  "federation_enabled": True})
        with self.assertRaisesRegex(RPCError, "permission denied"):
            self.mail.handle(packet("admin_site", token="alice"), (2, 30002, 30002))
        first = self.mail.handle(packet("admin_create_invitation", token="admin"),
                                 (1, 30001, 30001))["invitation"]
        identity, first_secret = decode_invitation(first)
        self.assertEqual(identity["bbs_id"], "alpha")
        self.assertEqual(len(first_secret), 32)
        peer_secret = self._import_beta()
        peers = self.mail.handle(packet("admin_peers", token="admin"),
                                 (1, 30001, 30001))["peers"]
        self.assertEqual(peers[0]["bbs_id"], "beta")
        self.assertNotIn("secret", peers[0])
        key_path = self.secret_dir / "beta.key"
        self.assertEqual(key_path.read_bytes(), peer_secret)
        self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
        self.assertNotIn(peer_secret, self.db_path.read_bytes())
        with self.assertRaisesRegex(RPCError, "unlink peers"):
            self.mail.handle(packet("admin_site_update", {
                "bbs_name": "Renamed Alpha", "bbs_id": "different",
                "public_endpoint": "https://alpha.example:2443",
            }, "admin"), (1, 30001, 30001))

    def test_remote_recipient_queues_text_mail_and_hides_routing(self) -> None:
        self._configure()
        self._import_beta()
        directory = {"ok": True, "data": {"users": [], "next_offset": None}}
        with patch.object(mail_module, "request", return_value=directory):
            listing = self.mail.handle(packet("recipients", token="alice"),
                                       (2, 30002, 30002))
        self.assertEqual(listing["peers"], [{"bbs_id": "beta", "bbs_name": "Station Beta"}])
        self.assertNotIn("endpoint", listing["peers"][0])
        result = self.mail.handle(packet("send", {
            "recipient": "bob@beta", "subject": "Linked systems", "body": "Text only",
        }, "alice"), (2, 30002, 30002))
        self.assertEqual((result["delivered"], result["queued"]), (0, 1))
        sent = self.mail.handle(packet("list", {"folder": "sent"}, "alice"),
                                (2, 30002, 30002))["messages"]
        self.assertEqual(sent[0]["recipient_user"], "bob@beta")
        self.assertEqual(sent[0]["exchange_state"], "queued")
        queued = self.mail.db.execute("SELECT * FROM federation_outbound").fetchone()
        self.assertEqual((queued["sender_user"], queued["recipient_user"], queued["peer_id"]),
                         ("alice", "bob", "beta"))

    def test_remote_sender_rate_limit_is_enforced(self) -> None:
        self._configure()
        self._import_beta()
        now = int(time.time())
        with self.mail.db:
            for index in range(mail_module.MAX_REMOTE_MESSAGES_PER_HOUR):
                message = self.mail.db.execute(
                    "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,"
                    "subject,body,created_at,recipient_deleted) VALUES(2,'alice',0,'bob@beta',"
                    "'Existing','Text',?,1)", (now,))
                self.mail.db.execute(
                    "INSERT INTO federation_outbound(message_uuid,message_id,sender_id,"
                    "sender_user,peer_id,recipient_user,subject,body,created_at,next_attempt) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"{index:032x}", message.lastrowid, 2, "alice", "beta", "bob",
                     "Existing", "Text", now, now))
        with self.assertRaisesRegex(RPCError, "rate limit"):
            self.mail.handle(packet("send", {
                "recipient": "bob@beta", "subject": "One too many", "body": "Text only",
            }, "alice"), (2, 30002, 30002))

    def test_authenticated_inbound_delivery_is_deduplicated(self) -> None:
        self._configure()
        secret = self._import_beta()
        exchange = Exchange(str(self.db_path), "unused", str(self.secret_dir))
        now = int(time.time())
        payload = json.dumps({
            "v": 1, "message_id": "1" * 32, "sender_bbs": "beta",
            "sender_user": "carol", "recipient_user": "bob",
            "subject": "Across the link", "body": "Authenticated text", "created_at": now,
        }, sort_keys=True, separators=(",", ":")).encode("ascii")

        def headers(nonce: str) -> dict[str, str]:
            timestamp = str(now)
            return {"X-Mars-Peer": "beta", "X-Mars-Timestamp": timestamp,
                    "X-Mars-Nonce": nonce,
                    "X-Mars-Signature": signature(secret, timestamp, nonce, payload)}

        auth_answer = {"ok": True, "data": {"account_id": 3, "userid": "bob"}}
        with patch.object(exchange_module, "request", return_value=auth_answer):
            accepted = exchange.receive(headers("2" * 32), payload)
            duplicate = exchange.receive(headers("3" * 32), payload)
            with self.assertRaises(ExchangeError) as replay:
                exchange.receive(headers("3" * 32), payload)
        self.assertTrue(accepted["accepted"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(replay.exception.status, 409)
        inbox = self.mail.db.execute(
            "SELECT sender_id,sender_user,recipient_id,recipient_user,subject,body "
            "FROM messages WHERE recipient_id=3").fetchall()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(tuple(inbox[0]),
                         (0, "carol@beta", 3, "bob", "Across the link", "Authenticated text"))

    def test_inbound_peer_rate_limit_precedes_recipient_lookup(self) -> None:
        self._configure()
        secret = self._import_beta()
        exchange = Exchange(str(self.db_path), "unused", str(self.secret_dir))
        now = int(time.time())
        with self.mail.db:
            for index in range(exchange_module.MAX_INBOUND_PER_PEER_HOUR):
                self.mail.db.execute(
                    "INSERT INTO federation_deliveries(peer_id,message_uuid,received_at) "
                    "VALUES('beta',?,?)", (f"{index:032x}", now))
        payload = json.dumps({
            "v": 1, "message_id": "f" * 32, "sender_bbs": "beta",
            "sender_user": "carol", "recipient_user": "bob",
            "subject": "Limited", "body": "Authenticated text", "created_at": now,
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
        timestamp = str(now)
        nonce = "5" * 32
        headers = {"X-Mars-Peer": "beta", "X-Mars-Timestamp": timestamp,
                   "X-Mars-Nonce": nonce,
                   "X-Mars-Signature": signature(secret, timestamp, nonce, payload)}
        with patch.object(exchange_module, "request") as lookup:
            with self.assertRaises(ExchangeError) as rejected:
                exchange.receive(headers, payload)
        self.assertEqual(rejected.exception.status, 429)
        lookup.assert_not_called()

    def test_wrong_signature_is_rejected_without_recipient_lookup(self) -> None:
        self._configure()
        self._import_beta()
        exchange = Exchange(str(self.db_path), "unused", str(self.secret_dir))
        raw = b'{"v":1}'
        headers = {"X-Mars-Peer": "beta", "X-Mars-Timestamp": str(int(time.time())),
                   "X-Mars-Nonce": "4" * 32, "X-Mars-Signature": "0" * 64}
        with patch.object(exchange_module, "request") as lookup:
            with self.assertRaises(ExchangeError) as rejected:
                exchange.receive(headers, raw)
        self.assertEqual(rejected.exception.status, 401)
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
