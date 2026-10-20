#!/usr/bin/env python3
"""End-to-end pinned-TLS exchange between two isolated synthetic BBS stores."""

from __future__ import annotations

import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import marsv2.federationd as exchange_module
import marsv2.maild as mail_module
from marsv2.federation import certificate_fingerprint, generate_secret, write_peer_secret
from marsv2.federationd import Exchange, ExchangeHTTPServer, ExchangeHandler
from marsv2.maild import Mail


def certificate(root: Path, name: str) -> tuple[Path, Path, str]:
    key = root / f"{name}.key"
    cert = root / f"{name}.crt"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
        "-days", "1", "-subj", f"/CN={name}", "-keyout", str(key),
        "-out", str(cert),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return key, cert, certificate_fingerprint(str(cert))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mars-federation-model-") as temporary:
        root = Path(temporary)
        alpha_root = root / "alpha"
        beta_root = root / "beta"
        alpha_root.mkdir()
        beta_root.mkdir()
        _alpha_key, _alpha_cert, alpha_fp = certificate(root, "alpha")
        beta_key, beta_cert, beta_fp = certificate(root, "beta")
        shared = generate_secret()

        alpha_mail = Mail(str(alpha_root / "mail.db"), "unused", "unused",
                          str(alpha_root / "peer-secrets"))
        beta_mail = Mail(str(beta_root / "mail.db"), "unused", "unused",
                         str(beta_root / "peer-secrets"))
        now = int(time.time())
        with alpha_mail.db:
            alpha_mail.db.execute(
                "UPDATE site_config SET bbs_name='Alpha',bbs_id='alpha',updated_at=? WHERE singleton=1",
                (now,))
        with beta_mail.db:
            beta_mail.db.execute(
                "UPDATE site_config SET bbs_name='Beta',bbs_id='beta',updated_at=? WHERE singleton=1",
                (now,))
            beta_mail.db.execute(
                "INSERT INTO federation_peers(bbs_id,bbs_name,endpoint,certificate_sha256,"
                "enabled,created_at,updated_at) VALUES('alpha','Alpha','https://alpha.invalid',?,1,?,?)",
                (alpha_fp, now, now))
        write_peer_secret(str(alpha_root / "peer-secrets"), "beta", shared)
        write_peer_secret(str(beta_root / "peer-secrets"), "alpha", shared)

        beta_exchange = Exchange(str(beta_root / "mail.db"), "unused",
                                 str(beta_root / "peer-secrets"))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(beta_cert), str(beta_key))
        server = ExchangeHTTPServer(("127.0.0.1", 0), ExchangeHandler)
        server.exchange = beta_exchange  # type: ignore[attr-defined]
        server.tls_context = context
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        with alpha_mail.db:
            alpha_mail.db.execute(
                "INSERT INTO federation_peers(bbs_id,bbs_name,endpoint,certificate_sha256,"
                "enabled,created_at,updated_at) VALUES('beta','Beta',?,?,1,?,?)",
                (f"https://127.0.0.1:{port}", beta_fp, now, now))

        identity = {"account_id": 10, "userid": "alice", "capabilities": [],
                    "roles": ["Basic-1"], "is_admin": False}
        original_validate = mail_module.validate
        original_request = exchange_module.request
        mail_module.validate = lambda _socket, _token, _uid: identity
        exchange_module.request = lambda *_args, **_kwargs: {
            "ok": True, "data": {"account_id": 20, "userid": "bob"}}
        try:
            queued = alpha_mail.handle({
                "op": "send", "token": "member",
                "data": {"recipient": "bob@beta", "subject": "Pinned TLS",
                         "body": "End-to-end text delivery"},
            }, (1, 30010, 30010))
            assert queued["queued"] == 1 and queued["delivered"] == 0
            alpha_exchange = Exchange(str(alpha_root / "mail.db"), "unused",
                                      str(alpha_root / "peer-secrets"))
            assert alpha_exchange.dispatch_once() == 1
            state = alpha_mail.db.execute(
                "SELECT state,attempts,last_error FROM federation_outbound").fetchone()
            assert tuple(state) == ("delivered", 1, "")
            received = beta_mail.db.execute(
                "SELECT sender_user,recipient_user,subject,body FROM messages").fetchone()
            assert tuple(received) == (
                "alice@alpha", "bob", "Pinned TLS", "End-to-end text delivery")

            with alpha_mail.db:
                alpha_mail.db.execute(
                    "UPDATE federation_peers SET certificate_sha256=? WHERE bbs_id='beta'",
                    ("0" * 64,))
            alpha_mail.handle({
                "op": "send", "token": "member",
                "data": {"recipient": "bob@beta", "subject": "Bad pin",
                         "body": "This must remain queued"},
            }, (1, 30010, 30010))
            assert alpha_exchange.dispatch_once() == 1
            retry = alpha_mail.db.execute(
                "SELECT state,last_error FROM federation_outbound ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert retry["state"] == "retry"
            assert "fingerprint mismatch" in retry["last_error"]
            assert beta_mail.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        finally:
            mail_module.validate = original_validate
            exchange_module.request = original_request
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            alpha_mail.db.close()
            beta_mail.db.close()
    print("PASS authenticated text mail, pinned TLS, queue status, and pin rejection")


if __name__ == "__main__":
    main()
