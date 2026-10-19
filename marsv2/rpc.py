"""Small, bounded SOCK_SEQPACKET RPC transport.

Messages are JSON objects, but framing is supplied by SOCK_SEQPACKET.  The
decoder rejects duplicate keys, non-objects, excess nesting by size, unknown
protocol versions, and oversized packets before service dispatch.
"""

from __future__ import annotations

import json
import os
import socket
import struct
from typing import Any, Callable

from . import PROTOCOL_VERSION

MAX_PACKET = 65536


class RPCError(Exception):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RPCError("duplicate field")
        result[key] = value
    return result


def decode_packet(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_PACKET:
        raise RPCError("invalid packet length")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RPCError("invalid JSON") from exc
    if not isinstance(obj, dict) or obj.get("v") != PROTOCOL_VERSION:
        raise RPCError("unsupported protocol")
    op = obj.get("op")
    if not isinstance(op, str) or not op or len(op) > 32:
        raise RPCError("invalid operation")
    data = obj.get("data", {})
    if not isinstance(data, dict):
        raise RPCError("invalid data")
    token = obj.get("token")
    if token is not None and (not isinstance(token, str) or len(token) > 128):
        raise RPCError("invalid token")
    return obj


def encode_packet(obj: dict[str, Any]) -> bytes:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > MAX_PACKET:
        raise RPCError("response too large")
    return raw


def request(path: str, op: str, data: dict[str, Any] | None = None,
            token: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    packet: dict[str, Any] = {"v": PROTOCOL_VERSION, "op": op, "data": data or {}}
    if token is not None:
        packet["token"] = token
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(encode_packet(packet))
        raw = sock.recv(MAX_PACKET + 1)
    finally:
        sock.close()
    response = decode_packet(raw)
    if not isinstance(response.get("ok"), bool):
        raise RPCError("invalid response")
    return response


def peer_credentials(conn: socket.socket) -> tuple[int, int, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", raw)


def serve(path: str, handler: Callable[[dict[str, Any], tuple[int, int, int]], dict[str, Any]],
          socket_mode: int = 0o666) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    os.chmod(path, socket_mode)
    listener.listen(64)
    while True:
        conn, _ = listener.accept()
        try:
            peer = peer_credentials(conn)
            raw = conn.recv(MAX_PACKET + 1)
            if len(raw) > MAX_PACKET:
                raise RPCError("packet too large")
            req = decode_packet(raw)
            result = handler(req, peer)
            response = {"v": PROTOCOL_VERSION, "op": "response", "ok": True,
                        "data": result}
        except RPCError as exc:
            response = {"v": PROTOCOL_VERSION, "op": "response", "ok": False,
                        "data": {"error": str(exc)}}
        except Exception:
            response = {"v": PROTOCOL_VERSION, "op": "response", "ok": False,
                        "data": {"error": "internal error"}}
        try:
            conn.sendall(encode_packet(response))
        finally:
            conn.close()
