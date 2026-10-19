#!/usr/bin/env python3
"""Deterministic recipient-directory filtering and ranking checks."""

from marsv2.ui import recipient_matches


USERS = [
    {"userid": "admin", "display_name": "System Administrator"},
    {"userid": "v2alice", "display_name": "Alice Example"},
    {"userid": "v2bob", "display_name": "Robert Example"},
    {"userid": "v2charlie", "display_name": "Bobby Tables"},
]

assert [item["userid"] for item in recipient_matches(USERS, "")] == [
    "admin", "v2alice", "v2bob", "v2charlie",
]
assert [item["userid"] for item in recipient_matches(USERS, "v2b")] == ["v2bob"]
assert [item["userid"] for item in recipient_matches(USERS, "bob")] == [
    "v2charlie", "v2bob",
]
assert [item["userid"] for item in recipient_matches(USERS, "example")] == [
    "v2alice", "v2bob",
]
assert recipient_matches(USERS, "not-a-user") == []
print("PASS recipient directory filtering and ranking")
