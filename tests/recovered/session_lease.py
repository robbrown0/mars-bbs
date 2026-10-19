#!/usr/bin/env python3
"""Deterministic regression for stale-session lease pruning."""

from __future__ import annotations

import time

from marsv2.sessiond import SESSION_LEASE_SECONDS, Sessions


if not 60.0 <= SESSION_LEASE_SECONDS <= 120.0:
    raise AssertionError(f"unexpected session lease: {SESSION_LEASE_SECONDS}")

authority = Sessions("/unused-in-this-test")
identity = {
    "account_id": 7,
    "userid": "lease-test",
    "display_name": "Lease Test",
    "runtime_uid": 30007,
    "is_admin": False,
    "supervisor_pid": 1,
    "created": time.monotonic() - 100,
}
authority.sessions["expired"] = dict(identity, expires=time.monotonic() - 1)
authority.sessions["active"] = dict(identity, expires=time.monotonic() + 60)

result = authority.handle({"op": "health", "data": {}}, (1, 0, 0))
if result != {"status": "ok", "sessions": 1}:
    raise AssertionError(result)
if "expired" in authority.sessions or "active" not in authority.sessions:
    raise AssertionError("lease pruning removed the wrong session")

print("PASS expired ghost pruned; active lease retained")
