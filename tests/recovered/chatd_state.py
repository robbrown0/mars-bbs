#!/usr/bin/env python3
"""Deterministic state-machine checks for non-durable page/Talk lifecycle."""

from __future__ import annotations

import socket
import time

from marsv2.chatd import PAGE_LIFETIME, Chat, Client, Page
from marsv2.rpc import MAX_PACKET, decode_packet


def pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)


caller_service, caller_test = pair()
target_service, target_test = pair()
caller = Client(caller_service, "caller-token", 30001, 1, "caller", "Caller", "pager")
target = Client(target_service, "target-token", 30002, 2, "target", "Target", "pager")
chat = Chat("/unused/chat.sock", "/unused/session.sock")
chat.clients.extend((caller, target))
chat.pages["expired-page-identifier"] = Page(
    "expired-page-identifier", caller, target.account_id, target.userid,
    time.monotonic() - PAGE_LIFETIME - 1.0,
)
chat.expire_pages()
assert not chat.pages
caller_event = decode_packet(caller_test.recv(MAX_PACKET + 1))["data"]
target_event = decode_packet(target_test.recv(MAX_PACKET + 1))["data"]
assert caller_event["kind"] == "page_expired", caller_event
assert target_event["kind"] == "page_closed", target_event

caller.talk_peer = target
target.talk_peer = caller
caller.talk_typing = True
target.talk_typing = True
chat.end_talk(caller)
assert caller.talk_peer is None and target.talk_peer is None
assert caller.talk_typing is False and target.talk_typing is False
ended = decode_packet(target_test.recv(MAX_PACKET + 1))["data"]
assert ended["kind"] == "talk_ended", ended
assert ended["message"] == "[Direct Talk Closed]", ended

for item in (caller_service, caller_test, target_service, target_test):
    item.close()
print("PASS chatd expiry and Talk teardown")
