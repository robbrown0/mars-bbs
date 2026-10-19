"""Service-side session validation bound to Unix peer credentials."""

from __future__ import annotations

from typing import Any

from .rpc import RPCError, request


def validate(session_socket: str, token: object, peer_uid: int) -> dict[str, Any]:
    if not isinstance(token, str):
        raise RPCError("authentication required")
    response = request(session_socket, "validate", {"peer_uid": peer_uid}, token)
    if not response["ok"]:
        raise RPCError("authentication required")
    return dict(response["data"])
