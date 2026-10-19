#!/usr/bin/env python3
"""Security and end-to-end tests for the outbound external IRC adapter."""

from __future__ import annotations

import json
import os
import socket
import ssl
import tempfile
import threading
import time

from marsv2 import PROTOCOL_VERSION
from marsv2.chatd import Chat, Client
from marsv2.external_irc import (
    NICKSERV_REFRESH_SECONDS, Adapter, ChatLink, ExternalBridge, State,
    load_adapter, sanitize_external, truncate_irc,
)
from marsv2.rpc import MAX_PACKET, decode_packet, encode_packet


CERT = os.environ["TEST_IRC_CERT"]
KEY = os.environ["TEST_IRC_KEY"]


def receive(sock: socket.socket, timeout: float = 5.0) -> dict[str, object]:
    sock.settimeout(timeout)
    return decode_packet(sock.recv(MAX_PACKET + 1))["data"]


def wait_path(path: str) -> None:
    for _ in range(100):
        if os.path.exists(path):
            return
        time.sleep(0.02)
    raise AssertionError(f"socket did not appear: {path}")


assert sanitize_external("\x02bold\x02 \x0312,4color \x1b[31mred\x1b[0m") == \
    "bold color red"
assert sanitize_external("safe\u202eevil\x9b") == "safeevil"
assert len(truncate_irc("PRIVMSG #MarsHotel :", "é" * 400).encode()) + 2 <= 512
assert NICKSERV_REFRESH_SECONDS == 24 * 60 * 60


with tempfile.TemporaryDirectory() as root:
    config_path = os.path.join(root, "external.json")
    config = {
        "version": 1,
        "adapters": {
            "fixture": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 1,
                "server_name": "irc.mars-v2.local",
                "network": "Fixture",
                "channel": "#MarsHotel",
                "room": "marshotel",
                "nick": "MarsHotel",
                "require_nick_identification": False,
                "channel_topic": "Fixture Mars topic | Type !info",
                "info_message": "Fixture BBS connection information",
            },
            "secondary": {
                "enabled": False,
                "host": "irc.example.invalid",
                "port": 6697,
                "server_name": "irc.example.invalid",
                "network": "Disabled",
                "channel": "#MarsHotel",
                "room": "secondary",
                "nick": "MarsHotel",
                "require_nick_identification": True,
            },
        },
    }
    with open(config_path, "w", encoding="utf-8") as stream:
        json.dump(config, stream)
    os.chmod(config_path, 0o600)
    loaded = load_adapter(config_path, "fixture")
    assert loaded.channel == "#MarsHotel"
    assert loaded.channel_topic == "Fixture Mars topic | Type !info"
    try:
        load_adapter(config_path, "secondary")
    except RuntimeError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("disabled secondary adapter was accepted")

    secret_path = os.path.join(root, "bridge.secret")
    secret = os.urandom(32).hex()
    with open(secret_path, "w", encoding="ascii") as stream:
        stream.write(secret + "\n")
    os.chmod(secret_path, 0o600)
    bridge_socket = os.path.join(root, "bridge.sock")
    chat = Chat(os.path.join(root, "unused-chat.sock"), os.path.join(root, "unused-session.sock"),
                bridge_socket, secret_path, os.getuid(), "fixture", "marshotel")
    threading.Thread(target=chat.bridge_accept_loop, daemon=True).start()
    wait_path(bridge_socket)

    # A forged capability is rejected before an adapter is registered.
    forged = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    forged.connect(bridge_socket)
    forged.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "bridge_hello",
                                  "data": {"adapter": "fixture", "room": "marshotel",
                                           "secret": "0" * 64}}))
    assert forged.recv(MAX_PACKET + 1) == b""
    forged.close()

    adapter = Adapter("fixture", "127.0.0.1", 1, "irc.mars-v2.local", "Fixture",
                      "#MarsHotel", "marshotel", "MarsHotel", True, False)
    link = ChatLink(bridge_socket, secret, adapter)
    local_service, local_user = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    local = Client(local_service, "synthetic", os.getuid(), 1, "rob", "Rob", "chat",
                   room="marshotel")
    with chat.lock:
        chat.clients.append(local)

    # Both sides exchange structured messages; no storage/session authority is involved.
    link.event("message", nick="Outside", message="hello from outside")
    event = receive(local_user)
    assert event["message"] == "[IRC] <Outside> hello from outside"
    chat.bridge_publish("marshotel", "message", userid="rob", message="hello outside")
    event = link.receive()
    assert event == {"kind": "message", "room": "marshotel", "userid": "rob",
                     "message": "hello outside"}

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(CERT, KEY)
    captured: list[str] = []
    second_connected = threading.Event()

    def server_worker() -> None:
        for attempt in range(2):
            raw, _ = server.accept()
            remote = tls_context.wrap_socket(raw, server_side=True)
            stream = remote.makefile("rwb", buffering=0)
            nick = "MarsHotel"
            joined = False
            user_seen = False
            collision_sent = False
            while True:
                raw_line = stream.readline(514)
                if not raw_line:
                    break
                line = raw_line.rstrip(b"\r\n").decode("utf-8", "replace")
                captured.append(line)
                if line.startswith("NICK "):
                    nick = line.split(" ", 1)[1]
                    if attempt == 0 and not collision_sent:
                        collision_sent = True
                        stream.write(f":fixture 433 * {nick} :nickname in use\r\n".encode())
                    elif user_seen:
                        stream.write(f":fixture 001 {nick} :welcome\r\n".encode())
                elif line.startswith("USER "):
                    user_seen = True
                    if attempt != 0:
                        stream.write(f":fixture 001 {nick} :welcome\r\n".encode())
                elif line == "PRIVMSG NickServ :IDENTIFY fixture-password-1234":
                    stream.write(
                        b":NickServ!service@fixture NOTICE MarsHotel :Password accepted - you are now recognized.\r\n"
                    )
                elif line == "JOIN #MarsHotel":
                    joined = True
                    stream.write(f":{nick}!bridge@fixture JOIN :#MarsHotel\r\n".encode())
                    quiet = "QuietGuest" if attempt == 0 else "QuietGuest2"
                    stream.write(
                        f":fixture 353 {nick} = #MarsHotel :@{nick} +{quiet}\r\n".encode()
                    )
                    stream.write(
                        f":fixture 366 {nick} #MarsHotel :End of /NAMES list.\r\n".encode()
                    )
                    if attempt == 0:
                        payload = ":Guest!u@h PRIVMSG #MarsHotel :\x02hello\x02 \x1b[31mred\x1b[0m\r\n"
                        stream.write(payload.encode())
                        stream.write(payload.encode())  # deterministic duplicate suppression
                        stream.write(b":Guest!u@h PRIVMSG #MarsHotel :\x01ACTION waves\x01\r\n")
                        stream.write(b":Guest!u@h PRIVMSG #MarsHotel :!info\r\n")
                    else:
                        stream.write(b":Guest2!u@h PRIVMSG #MarsHotel :after reconnect\r\n")
                        second_connected.set()
                elif line == "TOPIC #MarsHotel":
                    stream.write(
                        f":fixture 331 {nick} #MarsHotel :No topic is set\r\n".encode()
                    )
                elif joined and line.startswith("PRIVMSG #MarsHotel :[BBS] <rob>"):
                    if attempt == 0:
                        stream.write(f":fixture KICK #MarsHotel {nick} :test reconnect\r\n".encode())
                        break
            remote.close()
        server.close()

    threading.Thread(target=server_worker, daemon=True).start()
    live_adapter = Adapter("fixture", "127.0.0.1", port, "irc.mars-v2.local", "Fixture",
                           "#MarsHotel", "marshotel", "MarsHotel", True, True,
                           "Fixture Mars topic | Type !info",
                           "Fixture BBS connection information")
    state = State(os.path.join(root, "state", "state.json"), live_adapter)
    os.makedirs(os.path.dirname(state.path), exist_ok=True)
    stale = state.path + ".tmp.1"
    with open(stale, "w", encoding="ascii") as stream:
        stream.write("interrupted prior container write\n")
    state.write("starting")
    assert os.path.exists(stale), "state writer modified an unrelated stale file"
    with open(state.path, encoding="utf-8") as stream:
        assert json.load(stream)["status"] == "starting"
    bridge = ExternalBridge(live_adapter, link, state, "fixture-password-1234", None, CERT,
                            identify_interval=0.25)
    assert bridge.nickserv_identification_result(
        "NickServ!service@fixture",
        ["NOTICE", "MarsHotel", "Password accepted -- you are now recognized."],
    ) is True
    assert bridge.nickserv_identification_result(
        "NickServ!service@fixture",
        ["NOTICE", "MarsHotel", "Password incorrect."],
    ) is False
    assert bridge.nickserv_identification_result(
        "NickServ!service@fixture",
        ["NOTICE", "MarsHotel", "You are not identified."],
    ) is None
    assert all(bridge.accept_incoming() for _ in range(8))
    assert not bridge.accept_incoming(), "incoming burst limit was not enforced"
    bridge.incoming.clear()
    threading.Thread(target=bridge.run, daemon=True).start()

    seen_messages: list[str] = []
    deadline = time.monotonic() + 15
    sent_local = False
    while time.monotonic() < deadline and not second_connected.is_set():
        event = receive(local_user, max(0.2, deadline - time.monotonic()))
        seen_messages.append(str(event.get("message", "")))
        if "hello red" in str(event.get("message", "")) and not sent_local:
            chat.bridge_publish("marshotel", "message", userid="rob", message="relay marker")
            sent_local = True
    assert second_connected.wait(5), "bridge did not reconnect after KICK"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and \
            captured.count("PRIVMSG NickServ :IDENTIFY fixture-password-1234") < 3:
        time.sleep(0.05)
    assert captured.count("PRIVMSG NickServ :IDENTIFY fixture-password-1234") >= 3, \
        "long-lived connection did not renew NickServ identification"
    # Collect the second-connection marker.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any("after reconnect" in item for item in seen_messages):
        seen_messages.append(str(receive(local_user, deadline - time.monotonic()).get("message", "")))
    assert sum("hello red" in item for item in seen_messages) == 1
    assert sum("[IRC] Guest joined" in item for item in seen_messages) == 1
    assert any("[IRC] * Guest waves" in item for item in seen_messages)
    assert any("after reconnect" in item for item in seen_messages)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with chat.lock:
            visible = set(chat.bridge_users.get("marshotel", {}).values())
        if "QuietGuest2" in visible:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("quiet IRC NAMES occupant was absent from Chat presence")
    assert any(line == "PRIVMSG #MarsHotel :[BBS] <rob> relay marker" for line in captured)
    assert any(line == "TOPIC #MarsHotel :Fixture Mars topic | Type !info"
               for line in captured)
    assert any(line == "PRIVMSG #MarsHotel :Fixture BBS connection information"
               for line in captured)
    assert not any("!info" in item for item in seen_messages), \
        "bridge command leaked into native Chat"
    assert captured.count("PRIVMSG NickServ :IDENTIFY fixture-password-1234") >= 3
    assert any(line.startswith("NICK MarsHotel") and line != "NICK MarsHotel"
               for line in captured), "nickname collision did not select a suffix"
    assert not any("\x1b" in item or "\x02" in item for item in seen_messages)
    with open(state.path, encoding="utf-8") as stream:
        persisted = json.load(stream)
    assert persisted["channel"] == "#MarsHotel"
    assert persisted["last_identified_utc"] > 0
    assert "password" not in json.dumps(persisted).lower()

print("PASS external IRC boundary, TLS fixture, sanitization, dedupe, relay, KICK reconnect")
