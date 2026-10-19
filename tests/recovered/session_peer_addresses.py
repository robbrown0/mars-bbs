#!/usr/bin/env python3
"""Deterministic validation, activity, and peer-address privacy checks."""

from __future__ import annotations

import os
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from marsv2 import bbs_login, sessiond, ui
from marsv2.rpc import RPCError


with patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.44 50123 192.0.2.10 2222"}):
    assert bbs_login.ssh_peer_address() == "192.0.2.44"
with patch.dict(os.environ, {"SSH_CONNECTION": "2001:0db8::7 50123 2001:db8::1 2222"}):
    assert bbs_login.ssh_peer_address() == "2001:db8::7"
for malformed in ("", "not-an-ip 1 192.0.2.10 2222", "192.0.2.1 1"):
    with patch.dict(os.environ, {"SSH_CONNECTION": malformed}):
        assert bbs_login.ssh_peer_address() == ""


identity = {"account_id": 7, "userid": "alice", "display_name": "Alice",
            "runtime_uid": 30007, "is_admin": False, "roles": ["Basic-1"], "capabilities": [], "password_change_required": False}
original_request = sessiond.request
sessiond.request = lambda *_args, **_kwargs: {"ok": True, "data": dict(identity)}
try:
    authority = sessiond.Sessions("/unused")
    clock = iter((1000.0, 1000.0, 1065.0, 1070.0))
    with patch("marsv2.sessiond.time.monotonic", side_effect=lambda: next(clock)):
        created = authority.handle(
            {"op": "create", "data": {"ticket": "t" * 32,
                                        "remote_ip": "2001:0db8::7"}},
            (100, 0, 0))
    token = str(created["token"])
    assert authority.sessions[token]["remote_ip"] == "2001:db8::7"
    authority.sessions[token]["expires"] = float("inf")
    authority.sessions["duplicate"] = dict(
        authority.sessions[token], created=900.0, last_activity=1040.0,
        remote_ip="192.0.2.44")
    authority.sessions["unknown"] = dict(
        authority.sessions[token], userid="bob", account_id=8,
        created=1020.0, last_activity=1030.0, remote_ip="")
    with patch("marsv2.sessiond.time.monotonic", return_value=1065.0):
        details = authority.handle(
            {"op": "online_details", "token": token, "data": {}},
            (101, 30007, 30007))
    assert details == {"users": [
        {"userid": "alice", "sessions": 2,
         "connected_seconds": 165, "idle_seconds": 25, "activity": "Lobby"},
        {"userid": "bob", "sessions": 1,
         "connected_seconds": 45, "idle_seconds": 35, "activity": "Lobby"},
    ]}, details
    assert "remote_ip" not in repr(details) and "192.0.2.44" not in repr(details)

    with patch("marsv2.sessiond.time.monotonic", return_value=1070.0):
        recorded = authority.handle(
            {"op": "activity", "token": token, "data": {}},
            (101, 30007, 30007))
    assert recorded == {"recorded": True, "activity_mode": "Lobby"}
    assert authority.sessions[token]["last_activity"] == 1070.0
    try:
        authority.handle(
            {"op": "activity", "token": token, "data": {}},
            (101, 30008, 30008))
        raise AssertionError("another runtime UID updated session activity")
    except RPCError as exc:
        assert "identity mismatch" in str(exc)
    try:
        authority.handle(
            {"op": "online_details", "token": "x" * 48, "data": {}},
            (101, 30007, 30007))
        raise AssertionError("invalid session viewed peer addresses")
    except RPCError as exc:
        assert "invalid session" in str(exc)

    try:
        authority.handle(
            {"op": "create", "data": {"ticket": "t" * 32,
                                        "remote_ip": "not-an-ip"}},
            (100, 0, 0))
        raise AssertionError("malformed peer address accepted")
    except RPCError as exc:
        assert "invalid peer address" in str(exc)
finally:
    sessiond.request = original_request

assert ui.elapsed(0) == "0s"
assert ui.elapsed(65) == "1m 05s"
assert ui.elapsed(3660) == "1h 01m"
assert ui.elapsed(90000) == "1d 01h"
rendered = StringIO()
with redirect_stdout(rendered):
    ui.render_online_users([
        {"userid": "alice", "sessions": 2,
         "connected_seconds": 3660, "idle_seconds": 300},
    ])
assert "1h 01m" in rendered.getvalue()
assert "5m 00s" in rendered.getvalue()
assert "IDLE" in rendered.getvalue()
assert "SSH PEER IP" not in rendered.getvalue()

print("PASS presence timing, activity authorization, and peer-address privacy")
