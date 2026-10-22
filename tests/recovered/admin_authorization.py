#!/usr/bin/env python3
"""Deterministic authorization and lifecycle checks for the admin console."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import marsv2.boardd as board_module
import marsv2.maild as mail_module
import marsv2.sessiond as session_module
from marsv2.authd import Authority
from marsv2.boardd import Boards
from marsv2.maild import Mail
from marsv2.rpc import RPCError
from marsv2.sessiond import Sessions


SYSOP_PASSWORD = "Synthetic-recovery-only-admin-password!"
USER_PASSWORD = "Synthetic-recovery-only-member-password!"


def packet(op: str, data: dict[str, object] | None = None,
           token: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"op": op, "data": data or {}}
    if token is not None:
        result["token"] = token
    return result


def denied(action, expected: str = "permission denied") -> None:
    try:
        action()
    except RPCError as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError("operation unexpectedly allowed")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mars-admin-") as root:
        auth = Authority(str(Path(root) / "auth.db"))
        auth.add_account("admin", SYSOP_PASSWORD, "Administrator", True)
        auth.add_account("alice", USER_PASSWORD, "Alice")
        auth.add_account("bob", USER_PASSWORD, "Bob")
        admin = auth.db.execute("SELECT * FROM accounts WHERE userid='admin'").fetchone()
        alice = auth.db.execute("SELECT * FROM accounts WHERE userid='alice'").fetchone()
        assert admin is not None and alice is not None
        admin_peer = (100, int(admin["runtime_uid"]), int(admin["runtime_uid"]))
        alice_peer = (101, int(alice["runtime_uid"]), int(alice["runtime_uid"]))

        denied(lambda: auth.handle(packet("admin_accounts"), alice_peer))
        listing = auth.handle(packet("admin_accounts"), admin_peer)["accounts"]
        assert {row["userid"] for row in listing} == {"admin", "alice", "bob"}
        assert all("password_hash" not in row and "runtime_uid" not in row for row in listing)
        definitions = auth.handle(packet("admin_roles"), admin_peer)["roles"]
        assert [row["name"] for row in definitions] == [
            "Basic-1", "AccountMgr", "BoardMgr", "Music", "Sysop"]
        assert all(row["description"] for row in definitions)
        denied(lambda: auth.handle(packet("admin_set_roles", {
            "userid": "alice", "roles": ["BoardMgr"], "password": "wrong-password!"
        }), admin_peer), "reauthentication failed")
        changed = auth.handle(packet("admin_set_roles", {
            "userid": "alice", "roles": ["Basic-1", "BoardMgr"],
            "password": SYSOP_PASSWORD,
        }), admin_peer)
        assert changed["roles"] == ["BoardMgr"]
        denied(lambda: auth.handle(packet("admin_set_roles", {
            "userid": "admin", "roles": ["Basic-1"], "password": SYSOP_PASSWORD,
        }), admin_peer), "last enabled Sysop")
        denied(lambda: auth.handle(packet("service_audit", {
            "actor_id": admin["id"], "actor_user": "admin", "action": "terminate",
            "target": "bob",
        }), alice_peer), "authentication required")
        assert auth.handle(packet("service_audit", {
            "actor_id": admin["id"], "actor_user": "admin", "action": "terminate",
            "target": "bob", "detail": "synthetic test",
        }), (200, 21002, 21002))["recorded"] is True
        audit = auth.handle(packet("admin_audit"), admin_peer)["events"]
        assert audit and audit[0]["action"] == "session.terminate"
        # Exercise the one-way startup migration using an existing account
        # database whose role table is empty, as an old V2 image would leave it.
        with auth.db:
            auth.db.execute("DELETE FROM account_roles")
        auth.db.close()
        auth = Authority(str(Path(root) / "auth.db"))
        assert auth._roles(int(admin["id"])) == ["Sysop"]
        assert auth._roles(int(alice["id"])) == ["Basic-1"]

        changed = auth.handle(packet("admin_set_roles", {
            "userid": "alice", "roles": ["AccountMgr"], "password": SYSOP_PASSWORD,
        }), admin_peer)
        assert changed["roles"] == ["AccountMgr"]
        reset = auth.handle(packet("admin_reset_password", {
            "userid": "bob", "new_password": "Temporary-password-2026!",
            "password": USER_PASSWORD,
        }), alice_peer)
        assert reset["password_change_required"] is True
        denied(lambda: auth.handle(packet("admin_reset_password", {
            "userid": "admin", "new_password": "Do-not-use-this-password-2026!",
            "password": USER_PASSWORD,
        }), alice_peer), "only a Sysop")
        denied(lambda: auth.handle(packet("admin_reset_password", {
            "userid": "admin", "new_password": "Do-not-use-this-password-2026!",
            "password": SYSOP_PASSWORD,
        }), admin_peer), "Account Settings")
        denied(lambda: auth.handle(packet("login", {
            "userid": "bob", "password": USER_PASSWORD,
        }), (300, 29999, 29999)), "authentication failed")
        time.sleep(0.6)
        ticket = auth.handle(packet("login", {
            "userid": "bob", "password": "Temporary-password-2026!",
        }), (300, 29999, 29999))["ticket"]
        identity = auth.handle(packet("consume_ticket", {"ticket": ticket}),
                               (300, 29999, 29999))
        assert identity["password_change_required"] is True
        bob = auth.db.execute("SELECT * FROM accounts WHERE userid='bob'").fetchone()
        assert bob is not None
        bob_peer = (102, int(bob["runtime_uid"]), int(bob["runtime_uid"]))
        auth.handle(packet("change_password", {
            "old_password": "Temporary-password-2026!",
            "new_password": "Bob-final-password-2026!",
        }), bob_peer)
        assert auth.db.execute(
            "SELECT password_change_required FROM accounts WHERE id=?",
            (bob["id"],)).fetchone()[0] == 0

        identities = {
            "ordinary": {"account_id": 2, "userid": "bob", "is_admin": False,
                         "capabilities": [], "roles": ["Basic-1"]},
            "manager": {"account_id": 3, "userid": "alice", "is_admin": False,
                        "capabilities": ["board.manage", "board.moderate"],
                        "roles": ["BoardMgr"]},
            "sysop": {"account_id": 1, "userid": "admin", "is_admin": True,
                      "capabilities": ["board.manage", "board.moderate", "audit.read",
                                       "account.delete"],
                      "roles": ["Sysop"]},
        }
        original_validate = board_module.validate
        board_module.validate = lambda _socket, token, _uid: identities[str(token)]
        try:
            boards = Boards(str(Path(root) / "board.db"), "unused")
            denied(lambda: boards.handle(packet("admin_create", {
                "name": "Staff", "description": "Staff board",
            }, "ordinary"), (0, 30002, 0)))
            created = boards.handle(packet("admin_create", {
                "name": "Staff", "description": "Staff board", "read_only": True,
            }, "manager"), (0, 30003, 0))
            board_id = int(created["id"])
            boards.handle(packet("admin_update", {
                "board_id": board_id, "name": "Staff", "description": "Staff board",
                "read_only": True, "archived": True,
            }, "manager"), (0, 30003, 0))
            visible = boards.handle(packet("boards", token="ordinary"), (0, 30002, 0))["boards"]
            assert all(row["id"] != board_id for row in visible)
            denied(lambda: boards.handle(packet("list", {"board_id": board_id}, "ordinary"),
                                         (0, 30002, 0)), "archived")
            events = boards.handle(packet("admin_audit", token="sysop"),
                                   (0, 30001, 0))["events"]
            assert [event["action"] for event in events[:2]] == ["board.update", "board.create"]
            with boards.db:
                boards.db.execute(
                    "INSERT INTO read_state(account_id,board_id,last_post_id) VALUES(?,?,?)",
                    (bob["id"], board_id, 0))
                boards.db.execute(
                    "INSERT INTO posts(board_id,author_id,author_user,subject,body,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (board_id, bob["id"], "bob", "Retained", "Public history", int(time.time())))
            cleaned = boards.handle(packet("admin_delete_account", {
                "account_id": int(bob["id"]),
            }, "sysop"), (0, 30001, 0))
            assert cleaned == {"read_state_deleted": 1, "posts_retained": 1}
        finally:
            board_module.validate = original_validate

        original_mail_validate = mail_module.validate
        mail_module.validate = lambda _socket, token, _uid: identities[str(token)]
        try:
            mail = Mail(str(Path(root) / "mail.db"), "unused", "unused")
            with mail.db:
                mail.db.execute(
                    "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,"
                    "subject,body,created_at) VALUES(?,?,?,?,?,?,?)",
                    (alice["id"], "alice", bob["id"], "bob", "one", "body", 1))
                mail.db.execute(
                    "INSERT INTO messages(sender_id,sender_user,recipient_id,recipient_user,"
                    "subject,body,created_at) VALUES(?,?,?,?,?,?,?)",
                    (bob["id"], "bob", alice["id"], "alice", "two", "body", 2))
                mail.db.execute(
                    "INSERT INTO drafts(owner_id,recipient_user,subject,body,updated_at) "
                    "VALUES(?,?,?,?,?)", (bob["id"], "alice", "draft", "body", 3))
            cleaned = mail.handle(packet("admin_delete_account", {
                "account_id": int(bob["id"]),
            }, "sysop"), (0, 30001, 0))
            assert cleaned == {"drafts_deleted": 1, "inbox_removed": 1, "sent_removed": 1}
            assert mail.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        finally:
            mail_module.validate = original_mail_validate

        sessions = Sessions("unused")
        now = time.monotonic()
        admin_token = "A" * 48
        user_token = "B" * 48
        sessions.sessions = {
            admin_token: {"session_id": "a" * 16, "account_id": 1,
                            "userid": "admin", "display_name": "Administrator",
                            "runtime_uid": 30001, "is_admin": True, "roles": ["Sysop"],
                            "capabilities": ["session.manage", "account.delete"],
                            "password_change_required": False, "created": now,
                            "last_activity": now, "expires": now + 90},
            user_token: {"session_id": "b" * 16, "account_id": 2,
                           "userid": "bob", "display_name": "Bob", "runtime_uid": 30002,
                           "is_admin": False, "roles": ["Basic-1"], "capabilities": [],
                           "password_change_required": False,
                           "created": now, "last_activity": now, "expires": now + 90},
        }
        original_request = session_module.request
        session_module.request = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "ok": True,
            "data": {
                "account_id": 2, "userid": "bob", "display_name": "Bob",
                "runtime_uid": 30002, "is_admin": False, "roles": ["Basic-1"],
                "capabilities": [], "password_change_required": True,
            },
        }
        denied(lambda: sessions._refresh(user_token), "session credentials changed")
        assert user_token not in sessions.sessions
        # Restore the user for the authority-operation tests below. Those
        # tests stub refresh so they can focus on capability enforcement.
        sessions.sessions[user_token] = {
            "session_id": "b" * 16, "account_id": 2, "userid": "bob",
            "display_name": "Bob", "runtime_uid": 30002, "is_admin": False,
            "roles": ["Basic-1"], "capabilities": [],
            "password_change_required": False, "created": now,
            "last_activity": now, "expires": now + 90,
        }
        sessions._refresh = lambda token: sessions.sessions[token]  # type: ignore[method-assign]
        session_module.request = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "ok": True, "data": {"recorded": True}}
        try:
            denied(lambda: sessions.handle(packet("admin_sessions", token=user_token),
                                           (0, 30002, 0)))
            result = sessions.handle(
                packet("admin_end", {"session_id": "b" * 16}, admin_token),
                (0, 30001, 0))
            assert result == {"ended": True, "userid": "bob"}
            denied(lambda: sessions.handle(packet("admin_end", {"session_id": "a" * 16},
                                                                 admin_token),
                                           (0, 30001, 0)), "current admin session")
            # Re-add the user and prove account-wide termination is bounded to
            # the selected stable account ID.
            sessions.sessions[user_token] = {
                "session_id": "b" * 16, "account_id": int(bob["id"]),
                "userid": "bob", "display_name": "Bob", "runtime_uid": 30002,
                "is_admin": False, "roles": ["Basic-1"], "capabilities": [],
                "password_change_required": False, "created": now,
                "last_activity": now, "expires": now + 90,
            }
            ended = sessions.handle(packet("admin_end_account", {
                "account_id": int(bob["id"]),
            }, admin_token), (0, 30001, 0))
            assert ended == {"ended": 1}
        finally:
            session_module.request = original_request

        prepared = auth.handle(packet("admin_prepare_delete", {
            "userid": "bob", "password": SYSOP_PASSWORD,
        }), admin_peer)
        assert prepared["account_id"] == bob["id"]
        assert tuple(auth.db.execute(
            "SELECT login_enabled,deletion_pending FROM accounts WHERE id=?",
            (bob["id"],)).fetchone()) == (0, 1)
        deleted = auth.handle(packet("admin_finalize_delete", {
            "userid": "bob", "password": SYSOP_PASSWORD,
        }), admin_peer)
        assert deleted == {"deleted": True, "userid": "bob"}
        assert auth.db.execute(
            "SELECT count(*) FROM accounts WHERE id=?", (bob["id"],)).fetchone()[0] == 0

    print("PASS: scoped admin authorization, reauthentication, lifecycle, and audit")


if __name__ == "__main__":
    main()
