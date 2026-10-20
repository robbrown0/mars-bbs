"""TLS and HMAC protected text-mail exchange between trusted BBS installs."""

from __future__ import annotations

import argparse
import http.client
import json
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlsplit

from .db import connect
from .federation import (MESSAGE_ID_RE, bbs_id, payload_digest, read_peer_secret,
                         signature, verify_signature)
from .maild import MAX_MESSAGES_PER_USER, SCHEMA
from .rpc import request
from .validate import text, userid


MAX_HTTP_BODY = 12_288
MAX_CLOCK_SKEW = 300
MAX_INBOUND_PER_PEER_HOUR = 120
MAX_ATTEMPTS = 12


class ExchangeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _decode_object(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExchangeError(400, "invalid request") from exc
    if not isinstance(value, dict):
        raise ExchangeError(400, "invalid request")
    return value


class Exchange:
    def __init__(self, db_path: str, auth_socket: str, peer_secret_dir: str):
        self.db_path = db_path
        self.auth_socket = auth_socket
        self.peer_secret_dir = peer_secret_dir
        db = connect(db_path)
        try:
            db.executescript(SCHEMA)
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO site_config(singleton,bbs_name,bbs_id,public_endpoint,updated_at) "
                    "VALUES(1,'The Mars Hotel','mars-hotel','',?)", (int(time.time()),))
        finally:
            db.close()

    def receive(self, headers: Any, raw: bytes) -> dict[str, object]:
        if not raw or len(raw) > MAX_HTTP_BODY:
            raise ExchangeError(413, "request too large")
        try:
            peer_id = bbs_id(headers.get("X-Mars-Peer", ""))
            timestamp_text = text(headers.get("X-Mars-Timestamp", ""), "timestamp", 12, 1)
            timestamp = int(timestamp_text)
            nonce = text(headers.get("X-Mars-Nonce", ""), "nonce", 32, 32).lower()
            supplied = text(headers.get("X-Mars-Signature", ""), "signature", 64, 64).lower()
        except (ValueError, TypeError):
            raise ExchangeError(401, "authentication failed") from None
        now = int(time.time())
        if abs(now - timestamp) > MAX_CLOCK_SKEW:
            raise ExchangeError(401, "authentication failed")
        db = connect(self.db_path)
        try:
            peer = db.execute(
                "SELECT bbs_id,bbs_name FROM federation_peers WHERE bbs_id=? AND enabled=1",
                (peer_id,)).fetchone()
            if peer is None:
                raise ExchangeError(401, "authentication failed")
            try:
                secret = read_peer_secret(self.peer_secret_dir, peer_id)
            except (OSError, ValueError):
                raise ExchangeError(401, "authentication failed") from None
            if not verify_signature(secret, timestamp_text, nonce, raw, supplied):
                raise ExchangeError(401, "authentication failed")
            with db:
                db.execute("DELETE FROM federation_nonces WHERE seen_at<?",
                           (now - MAX_CLOCK_SKEW * 2,))
                try:
                    db.execute(
                        "INSERT INTO federation_nonces(peer_id,nonce,seen_at) VALUES(?,?,?)",
                        (peer_id, nonce, now))
                except sqlite3.IntegrityError:
                    raise ExchangeError(409, "request replayed") from None
            payload = _decode_object(raw)
            if set(payload) != {
                    "v", "message_id", "sender_bbs", "sender_user", "recipient_user",
                    "subject", "body", "created_at"
            } or payload.get("v") != 1:
                raise ExchangeError(400, "invalid request")
            try:
                message_id = text(payload["message_id"], "message ID", 32, 32).lower()
                if not MESSAGE_ID_RE.fullmatch(message_id):
                    raise ValueError("invalid message ID")
                sender_bbs = bbs_id(payload["sender_bbs"])
                sender_user = userid(payload["sender_user"])
                recipient_user = userid(payload["recipient_user"])
                subject = text(payload["subject"], "subject", 72, 1)
                body = text(payload["body"], "message", 8192, 1, True)
                created_at = int(payload["created_at"])
            except (ValueError, TypeError, OverflowError):
                raise ExchangeError(400, "invalid request") from None
            if sender_bbs != peer_id or created_at < 0 or created_at > now + MAX_CLOCK_SKEW:
                raise ExchangeError(400, "invalid request")
            duplicate = db.execute(
                "SELECT 1 FROM federation_deliveries WHERE peer_id=? AND message_uuid=?",
                (peer_id, message_id)).fetchone()
            if duplicate is not None:
                return {"accepted": True, "duplicate": True, "message_id": message_id}
            recent = db.execute(
                "SELECT count(*) FROM federation_deliveries WHERE peer_id=? AND received_at>=?",
                (peer_id, now - 3600)).fetchone()[0]
            if recent >= MAX_INBOUND_PER_PEER_HOUR:
                raise ExchangeError(429, "peer delivery rate limit reached")
            lookup = request(self.auth_socket, "mail_recipient", {"userid": recipient_user})
            if not lookup["ok"]:
                raise ExchangeError(404, "recipient unavailable")
            target = dict(lookup["data"])
            count = db.execute(
                "SELECT count(*) FROM messages WHERE recipient_id=? AND recipient_deleted=0",
                (target["account_id"],)).fetchone()[0]
            if count >= MAX_MESSAGES_PER_USER:
                raise ExchangeError(507, "recipient mailbox is full")
            sender_address = f"{sender_user}@{peer_id}"
            with db:
                cursor = db.execute(
                    "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,"
                    "subject,body,created_at,sender_deleted,recipient_list) "
                    "VALUES(0,?,?,?,?,?,?,1,?)",
                    (sender_address, target["account_id"], target["userid"], subject, body,
                     now, json.dumps([str(target["userid"])], separators=(",", ":"))))
                db.execute(
                    "INSERT INTO federation_deliveries(peer_id,message_uuid,received_at) "
                    "VALUES(?,?,?)", (peer_id, message_id, now))
            return {"accepted": True, "duplicate": False,
                    "message_id": message_id, "local_id": int(cursor.lastrowid)}
        finally:
            db.close()

    @staticmethod
    def _payload(row: sqlite3.Row, local_bbs_id: str) -> bytes:
        return json.dumps({
            "v": 1,
            "message_id": row["message_uuid"],
            "sender_bbs": local_bbs_id,
            "sender_user": row["sender_user"],
            "recipient_user": row["recipient_user"],
            "subject": row["subject"],
            "body": row["body"],
            "created_at": row["created_at"],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    def _deliver(self, row: sqlite3.Row, site_id: str, peer: sqlite3.Row) -> None:
        raw = self._payload(row, site_id)
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        secret = read_peer_secret(self.peer_secret_dir, str(peer["bbs_id"]))
        parsed = urlsplit(str(peer["endpoint"]))
        port = parsed.port or 443
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            parsed.hostname, port, timeout=10, context=context)
        try:
            connection.connect()
            if connection.sock is None:
                raise OSError("TLS connection unavailable")
            certificate = connection.sock.getpeercert(binary_form=True)
            if payload_digest(certificate) != peer["certificate_sha256"]:
                raise OSError("peer TLS certificate fingerprint mismatch")
            connection.request("POST", "/v1/mail", body=raw, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "X-Mars-Peer": site_id,
                "X-Mars-Timestamp": timestamp,
                "X-Mars-Nonce": nonce,
                "X-Mars-Signature": signature(secret, timestamp, nonce, raw),
            })
            response = connection.getresponse()
            response_raw = response.read(8193)
            if len(response_raw) > 8192:
                raise OSError("peer response too large")
            if response.status in (200, 202):
                answer = _decode_object(response_raw)
                if answer.get("accepted") is not True:
                    raise OSError("peer rejected delivery")
                return
            message = "peer rejected delivery"
            try:
                answer = _decode_object(response_raw)
                if isinstance(answer.get("error"), str):
                    message = text(answer["error"], "peer error", 160, 1)
            except ExchangeError:
                pass
            if 400 <= response.status < 500 and response.status != 429:
                raise ExchangeError(response.status, message)
            raise OSError(message)
        finally:
            connection.close()

    def dispatch_once(self) -> int:
        now = int(time.time())
        db = connect(self.db_path)
        try:
            site = db.execute("SELECT bbs_id FROM site_config WHERE singleton=1").fetchone()
            if site is None:
                return 0
            rows = db.execute(
                "SELECT * FROM federation_outbound WHERE state IN ('queued','retry') "
                "AND next_attempt<=? ORDER BY id LIMIT 10", (now,)).fetchall()
            handled = 0
            for row in rows:
                peer = db.execute(
                    "SELECT * FROM federation_peers WHERE bbs_id=? AND enabled=1",
                    (row["peer_id"],)).fetchone()
                if peer is None:
                    continue
                attempts = int(row["attempts"]) + 1
                try:
                    self._deliver(row, str(site["bbs_id"]), peer)
                except ExchangeError as exc:
                    with db:
                        db.execute(
                            "UPDATE federation_outbound SET state='failed',attempts=?,"
                            "last_error=? WHERE id=?", (attempts, str(exc)[:160], row["id"]))
                except (OSError, ValueError, ssl.SSLError, socket.error) as exc:
                    final = attempts >= MAX_ATTEMPTS
                    delay = min(3600, 15 * (2 ** min(attempts - 1, 8)))
                    with db:
                        db.execute(
                            "UPDATE federation_outbound SET state=?,attempts=?,next_attempt=?,"
                            "last_error=? WHERE id=?",
                            ("failed" if final else "retry", attempts,
                             now + delay, str(exc)[:160], row["id"]))
                else:
                    with db:
                        db.execute(
                            "UPDATE federation_outbound SET state='delivered',attempts=?,"
                            "last_error='' WHERE id=?", (attempts, row["id"]))
                handled += 1
            return handled
        finally:
            db.close()

    def worker(self, stopped: threading.Event) -> None:
        while not stopped.is_set():
            try:
                handled = self.dispatch_once()
            except Exception:
                handled = 0
            stopped.wait(1.0 if handled else 5.0)


class ExchangeHandler(BaseHTTPRequestHandler):
    server_version = "MarsMailExchange/1"

    def _json(self, status: int, body: dict[str, object]) -> None:
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/health":
            self._json(404, {"error": "not found"})
            return
        self._json(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/mail":
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self._json(415, {"error": "application/json required"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(400, {"error": "invalid request"})
            return
        if length < 1 or length > MAX_HTTP_BODY:
            self._json(413, {"error": "request too large"})
            return
        raw = self.rfile.read(length)
        try:
            result = self.server.exchange.receive(self.headers, raw)  # type: ignore[attr-defined]
        except ExchangeError as exc:
            self._json(exc.status, {"error": str(exc)})
            return
        self._json(202, result)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ExchangeHTTPServer(HTTPServer):
    """Do not turn unauthenticated TLS disconnects into traceback log spam."""

    tls_context: ssl.SSLContext

    def get_request(self) -> tuple[ssl.SSLSocket, object]:
        connection, address = self.socket.accept()
        connection.settimeout(10)
        try:
            secured = self.tls_context.wrap_socket(connection, server_side=True)
        except Exception:
            connection.close()
            raise
        return secured, address

    def handle_error(self, _request: object, _client_address: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--auth-socket", required=True)
    parser.add_argument("--peer-secret-dir", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=2443)
    args = parser.parse_args()
    exchange = Exchange(args.db, args.auth_socket, args.peer_secret_dir)
    stopped = threading.Event()
    worker = threading.Thread(target=exchange.worker, args=(stopped,), daemon=True)
    worker.start()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.certificate, args.private_key)
    server = ExchangeHTTPServer((args.bind, args.port), ExchangeHandler)
    server.exchange = exchange  # type: ignore[attr-defined]
    server.tls_context = context
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stopped.set()
        worker.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
