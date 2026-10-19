"""Client helpers used by the terminal frontend."""

from __future__ import annotations

import os
from typing import Any

from .rpc import RPCError, request

SOCKET_ROOT = os.environ.get("MARS_V2_SOCKET_ROOT", "/run/mars-v2")


def socket_path(service: str) -> str:
    return f"{SOCKET_ROOT}/{service}/{service}.sock"


def call(service: str, op: str, data: dict[str, Any] | None = None,
         token: str | None = None) -> dict[str, Any]:
    response = request(socket_path(service), op, data, token)
    if not response["ok"]:
        message = response.get("data", {}).get("error", "request failed")
        raise RPCError(str(message))
    return dict(response["data"])
