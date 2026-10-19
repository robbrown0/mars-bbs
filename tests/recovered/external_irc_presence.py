#!/usr/bin/env python3
"""Deterministic native-Chat `/who` coverage for quiet external IRC users."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

from marsv2 import PROTOCOL_VERSION
import marsv2.chatd as chatd
from marsv2.external_irc import Adapter, ChatLink
from marsv2.rpc import MAX_PACKET, decode_packet, encode_packet


def receive(sock: socket.socket) -> dict[str, object]:
    sock.settimeout(3)
    return decode_packet(sock.recv(MAX_PACKET + 1))["data"]


def wait_path(path: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.02)
    raise AssertionError("bridge socket did not appear")


with tempfile.TemporaryDirectory() as root:
    secret_path = os.path.join(root, "bridge.secret")
    secret = os.urandom(32).hex()
    with open(secret_path, "w", encoding="ascii") as stream:
        stream.write(secret + "\n")
    os.chmod(secret_path, 0o600)
    bridge_socket = os.path.join(root, "bridge.sock")
    chat = chatd.Chat(os.path.join(root, "chat.sock"), os.path.join(root, "session.sock"),
                      bridge_socket, secret_path, os.getuid(), "synchronet", "marshotel")
    threading.Thread(target=chat.bridge_accept_loop, daemon=True).start()
    wait_path(bridge_socket)

    adapter = Adapter("synchronet", "irc.example.test", 6697, "irc.example.test",
                      "Synchronet", "#MarsHotel", "marshotel", "MarsHotel", True, True)
    bridge = ChatLink(bridge_socket, secret, adapter)
    bridge.event("status", state="online", channel="#MarsHotel")
    bridge.event("presence_sync", nicks=["QuietGuest", "TalkativeGuest"])

    # This service-level test supplies a fixed identity in place of sessiond;
    # authorization behavior is covered separately by the integration suite.
    original_validate = chatd.validate
    chatd.validate = lambda *_args, **_kwargs: {
        "account_id": 42, "userid": "v2alice", "display_name": "Alice"
    }
    service, user = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        threading.Thread(target=chat.client_loop, args=(service,), daemon=True).start()
        user.sendall(encode_packet({
            "v": PROTOCOL_VERSION,
            "op": "join",
            "token": "presence-test-token",
            "data": {"mode": "chat", "room": "marshotel"},
        }))
        joined = receive(user)
        assert joined["kind"] == "joined" and joined["room"] == "marshotel"
        motd = receive(user)
        assert motd["kind"] == "motd"
        bridge_status = receive(user)
        assert bridge_status["kind"] == "bridge" and "#MarsHotel" in bridge_status["message"]

        user.sendall(encode_packet({
            "v": PROTOCOL_VERSION,
            "op": "who",
            "token": "presence-test-token",
            "data": {},
        }))
        local = receive(user)
        external = receive(user)
        assert local["message"] == "Users in room: v2alice"
        assert external["message"] == \
            "IRC users on #MarsHotel: [IRC] QuietGuest, [IRC] TalkativeGuest"
        assert external["users"] == ["QuietGuest", "TalkativeGuest"]
        assert external["source"] == "irc"
    finally:
        chatd.validate = original_validate
        user.close()

print("PASS external IRC NAMES presence appears in native Chat /who")
