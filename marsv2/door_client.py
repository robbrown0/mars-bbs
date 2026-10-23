"""Framed terminal transport for authenticated external game processes."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import tty
from collections.abc import Callable

from .client import socket_path


MAX_FRAME = 16_384
MAX_HANDSHAKE = 4_096


class DoorError(Exception):
    pass


def _read_exact(sock: socket.socket, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        part = sock.recv(length - len(value))
        if not part:
            raise EOFError("game service disconnected")
        value.extend(part)
    return bytes(value)


def send_frame(sock: socket.socket, kind: bytes, payload: bytes = b"") -> None:
    if len(kind) != 1 or len(payload) > MAX_FRAME:
        raise DoorError("invalid game frame")
    sock.sendall(struct.pack("!I", len(payload) + 1) + kind + payload)


def recv_frame(sock: socket.socket) -> tuple[bytes, bytes]:
    length = struct.unpack("!I", _read_exact(sock, 4))[0]
    if length < 1 or length > MAX_FRAME + 1:
        raise DoorError("invalid game frame")
    raw = _read_exact(sock, length)
    return raw[:1], raw[1:]


def _size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
        return max(20, min(size.columns, 240)), max(10, min(size.lines, 100))
    except OSError:
        return 80, 24


def play(game: str, token: str, *, notification_socket: socket.socket | None = None,
         on_notification: Callable[[], object] | None = None,
         paint_notification: Callable[[], None] | None = None) -> int:
    """Attach this authenticated terminal to a process owned by doord."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(socket_path("door").replace("door.sock", "play.sock"))
    columns, rows = _size()
    request = json.dumps({"v": 1, "game": game, "token": token,
                          "columns": columns, "rows": rows},
                         separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(request) > MAX_HANDSHAKE:
        raise DoorError("game request is too large")
    sock.sendall(struct.pack("!I", len(request)) + request)
    kind, payload = recv_frame(sock)
    if kind != b"A":
        message = payload.decode("utf-8", "replace") or "game unavailable"
        sock.close()
        raise DoorError(message)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if sys.stdin.isatty() else None
    resized = True

    def on_resize(_signum, _frame) -> None:
        nonlocal resized
        resized = True

    previous_handler = signal.signal(signal.SIGWINCH, on_resize)
    try:
        if old is not None:
            tty.setraw(fd)
        while True:
            if resized:
                columns, rows = _size()
                send_frame(sock, b"R", struct.pack("!HH", rows, columns))
                resized = False
            sources = [fd, sock]
            if notification_socket is not None and on_notification is not None:
                sources.append(notification_socket)
            if paint_notification is not None:
                paint_notification()
            ready, _, _ = select.select(sources, [], [])
            if notification_socket is not None and notification_socket in ready:
                on_notification()
            if fd in ready:
                data = os.read(fd, 4096)
                if not data:
                    send_frame(sock, b"C")
                    return 0
                send_frame(sock, b"I", data)
            if sock in ready:
                kind, payload = recv_frame(sock)
                if kind == b"O":
                    os.write(sys.stdout.fileno(), payload)
                    if paint_notification is not None:
                        paint_notification()
                elif kind == b"E":
                    return struct.unpack("!i", payload)[0] if len(payload) == 4 else 1
                elif kind == b"X":
                    raise DoorError(payload.decode("utf-8", "replace"))
                else:
                    raise DoorError("game service sent an invalid frame")
    finally:
        signal.signal(signal.SIGWINCH, previous_handler)
        if old is not None:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        sock.close()
        sys.stdout.write("\033[0m\033[?25h")
        sys.stdout.flush()
