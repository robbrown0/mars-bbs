"""Authenticated, non-durable native chat, paging, and direct Talk service."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import secrets
import socket
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass, field

from . import PROTOCOL_VERSION
from .identity import validate
from .rpc import MAX_PACKET, RPCError, decode_packet, encode_packet, peer_credentials
from .validate import room, text, userid


PAGE_LIFETIME = 120.0
BRIDGE_ADAPTER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
BRIDGE_NICK_RE = re.compile(r"^[A-Za-z0-9_\-\[\]\\`^{|}]{1,32}$")
BRIDGE_PRESENCE_MAX = 256


@dataclass
class Client:
    conn: socket.socket
    token: str
    peer_uid: int
    account_id: int
    userid: str
    display_name: str
    mode: str
    room: str = "lobby"
    chatid: str = ""
    is_sysop: bool = False
    ignored: set[str] = field(default_factory=set)
    pager_available: bool = True
    monitors: set[str] = field(default_factory=set)
    talk_peer: Client | None = None
    talk_typing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Page:
    page_id: str
    requester: Client
    target_account_id: int
    target_userid: str
    created: float


@dataclass
class ExternalBridge:
    conn: socket.socket
    adapter: str
    room: str
    lock: threading.Lock = field(default_factory=threading.Lock)


def read_bridge_secret(path: str) -> bytes:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError("bridge secret must be a private regular file")
    with open(path, encoding="ascii") as stream:
        value = stream.read().strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        raise RuntimeError("bridge secret must contain 32 hexadecimal bytes")
    return bytes.fromhex(value)


def bridge_text(value: object, name: str, maximum: int) -> str:
    value = text(value, name, maximum, 1)
    for char in value:
        code = ord(char)
        if 0x7f <= code <= 0x9f or unicodedata.category(char) == "Cf":
            raise RPCError(f"invalid {name} character")
    return value


class Chat:
    def __init__(self, socket_path: str, session_socket: str,
                 bridge_socket: str | None = None,
                 bridge_secret_file: str | None = None,
                 bridge_uid: int | None = None,
                 bridge_adapter: str = "synchronet",
                 bridge_room: str = "marshotel",
                 motd_file: str | None = None):
        self.socket_path = socket_path
        self.session_socket = session_socket
        self.clients: list[Client] = []
        self.pages: dict[str, Page] = {}
        self.lock = threading.RLock()
        self.bridges: list[ExternalBridge] = []
        self.bridge_status: dict[str, str] = {}
        self.bridge_users: dict[str, dict[str, str]] = {}
        self.bridge_channels: dict[str, str] = {}
        self.bridge_socket = bridge_socket
        self.bridge_uid = bridge_uid
        self.bridge_adapter = bridge_adapter
        self.bridge_room = room(bridge_room)
        self.room_owners: dict[str, int] = {"lobby": 0}
        self.room_operators: dict[str, set[int]] = {"lobby": set()}
        self.room_flags: dict[str, set[str]] = {"lobby": set()}
        self.room_invites: dict[str, set[int]] = {}
        self.motd = ("Welcome to Mars Hotel native Chat.\n"
                     "Use /help for rooms, actions, private messages, and safety controls.")
        if motd_file:
            with open(motd_file, "r", encoding="utf-8") as stream:
                self.motd = text(stream.read(), "Chat MOTD", 4096, 1, True)
        self.bridge_secret = read_bridge_secret(bridge_secret_file) \
            if bridge_secret_file else None
        if bool(bridge_socket) != bool(self.bridge_secret) or \
                bool(bridge_socket) != (bridge_uid is not None):
            raise RuntimeError("bridge socket, secret, and UID must be configured together")
        if not BRIDGE_ADAPTER_RE.fullmatch(bridge_adapter):
            raise RuntimeError("invalid bridge adapter name")
        if bridge_socket:
            self.bridge_status[self.bridge_room] = \
                f"[IRC bridge offline: {self.bridge_adapter}]"

    def send(self, client: Client, kind: str, message: str, **extra: object) -> None:
        data: dict[str, object] = {"kind": kind, "message": message}
        data.update(extra)
        packet = encode_packet({"v": PROTOCOL_VERSION, "op": "event", "ok": True,
                                "data": data})
        with client.lock:
            client.conn.sendall(packet)

    def notify(self, targets: list[Client], kind: str, message: str,
               **extra: object) -> None:
        for target in targets:
            try:
                self.send(target, kind, message, **extra)
            except OSError:
                pass

    def send_bridge(self, bridge: ExternalBridge, kind: str, **data: object) -> None:
        packet = {"v": PROTOCOL_VERSION, "op": "local_event",
                  "data": {"kind": kind, **data}}
        with bridge.lock:
            bridge.conn.sendall(encode_packet(packet))

    def bridge_publish(self, room_name: str, kind: str, **data: object) -> None:
        with self.lock:
            targets = [item for item in self.bridges if item.room == room_name]
        for bridge in targets:
            try:
                self.send_bridge(bridge, kind, room=room_name, **data)
            except OSError:
                pass

    def bridge_notice(self, room_name: str, message: str) -> None:
        self.broadcast(room_name, "bridge", message, adapter=self.bridge_adapter,
                       room=room_name)

    def bridge_loop(self, conn: socket.socket) -> None:
        bridge: ExternalBridge | None = None
        try:
            peer = peer_credentials(conn)
            if peer[1] != self.bridge_uid:
                raise RPCError("bridge peer is not authorized")
            req = decode_packet(conn.recv(MAX_PACKET + 1))
            if req["op"] != "bridge_hello":
                raise RPCError("bridge hello required")
            data = req["data"]
            if set(data) != {"adapter", "room", "secret"}:
                raise RPCError("invalid bridge hello")
            adapter = bridge_text(data.get("adapter"), "bridge adapter", 32)
            room_name = room(data.get("room"))
            supplied = data.get("secret")
            if adapter != self.bridge_adapter or room_name != self.bridge_room \
                    or not isinstance(supplied, str) \
                    or not re.fullmatch(r"[0-9A-Fa-f]{64}", supplied) \
                    or not hmac.compare_digest(bytes.fromhex(supplied), self.bridge_secret):
                raise RPCError("bridge authentication failed")
            bridge = ExternalBridge(conn, adapter, room_name)
            with self.lock:
                if any(item.adapter == adapter for item in self.bridges):
                    raise RPCError("bridge adapter is already connected")
                self.bridges.append(bridge)
                self.bridge_users[room_name] = {}
            self.send_bridge(bridge, "bridge_ready", adapter=adapter, room=room_name)
            while True:
                raw = conn.recv(MAX_PACKET + 1)
                if not raw:
                    break
                req = decode_packet(raw)
                if req["op"] != "external_event":
                    raise RPCError("invalid bridge operation")
                data = req["data"]
                kind = data.get("kind")
                if kind == "message":
                    nick = bridge_text(data.get("nick"), "IRC nickname", 32)
                    if not BRIDGE_NICK_RE.fullmatch(nick):
                        raise RPCError("invalid IRC nickname")
                    message = bridge_text(data.get("message"), "IRC message", 400)
                    self.broadcast(room_name, "bridge_message",
                                   f"[IRC] <{nick}> {message}", nick=nick,
                                   adapter=adapter, room=room_name)
                elif kind == "action":
                    nick = bridge_text(data.get("nick"), "IRC nickname", 32)
                    if not BRIDGE_NICK_RE.fullmatch(nick):
                        raise RPCError("invalid IRC nickname")
                    message = bridge_text(data.get("message"), "IRC action", 400)
                    self.broadcast(room_name, "bridge_action",
                                   f"[IRC] * {nick} {message}", nick=nick,
                                   adapter=adapter, room=room_name)
                elif kind == "presence":
                    nick = bridge_text(data.get("nick"), "IRC nickname", 32)
                    state = data.get("state")
                    if not BRIDGE_NICK_RE.fullmatch(nick) or state not in ("joined", "left"):
                        raise RPCError("invalid IRC presence")
                    key = nick.casefold()
                    with self.lock:
                        users = self.bridge_users.setdefault(room_name, {})
                        changed = key not in users if state == "joined" else key in users
                        if state == "joined":
                            if len(users) >= BRIDGE_PRESENCE_MAX and key not in users:
                                raise RPCError("IRC presence limit exceeded")
                            users[key] = nick
                        else:
                            users.pop(key, None)
                    if changed:
                        self.broadcast(room_name, "bridge_presence",
                                       f"[IRC] {nick} {state}", nick=nick, state=state,
                                       adapter=adapter, room=room_name)
                elif kind == "presence_sync":
                    raw_nicks = data.get("nicks")
                    if not isinstance(raw_nicks, list) \
                            or len(raw_nicks) > BRIDGE_PRESENCE_MAX:
                        raise RPCError("invalid IRC presence snapshot")
                    snapshot: dict[str, str] = {}
                    for value in raw_nicks:
                        nick = bridge_text(value, "IRC nickname", 32)
                        if not BRIDGE_NICK_RE.fullmatch(nick):
                            raise RPCError("invalid IRC nickname")
                        snapshot[nick.casefold()] = nick
                    with self.lock:
                        self.bridge_users[room_name] = snapshot
                elif kind == "status":
                    state = data.get("state")
                    channel = bridge_text(data.get("channel"), "IRC channel", 64)
                    if state not in ("online", "offline"):
                        raise RPCError("invalid bridge status")
                    status = f"[IRC bridge {state}: {channel}]"
                    with self.lock:
                        changed = self.bridge_status.get(room_name) != status
                        self.bridge_status[room_name] = status
                        self.bridge_channels[room_name] = channel
                        if state == "offline":
                            self.bridge_users[room_name] = {}
                    if changed:
                        self.bridge_notice(room_name, status)
                else:
                    raise RPCError("invalid external event")
        except (RPCError, ValueError, OSError):
            pass
        finally:
            removed = False
            if bridge is not None:
                with self.lock:
                    if bridge in self.bridges:
                        self.bridges.remove(bridge)
                        removed = True
                    status = f"[IRC bridge offline: {bridge.adapter}]"
                    changed = self.bridge_status.get(bridge.room) != status
                    self.bridge_status[bridge.room] = status
                    self.bridge_users[bridge.room] = {}
                if removed and changed:
                    self.bridge_notice(bridge.room, status)
            conn.close()

    def bridge_accept_loop(self) -> None:
        if self.bridge_socket is None:
            raise RuntimeError("external bridge socket is not configured")
        try:
            os.unlink(self.bridge_socket)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(self.bridge_socket)
        os.chmod(self.bridge_socket, 0o666)
        listener.listen(4)
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=self.bridge_loop, args=(conn,), daemon=True).start()

    def broadcast(self, room_name: str, kind: str, message: str,
                  exclude: Client | None = None, **extra: object) -> None:
        with self.lock:
            targets = [item for item in self.clients
                       if item.mode == "chat" and item.room == room_name
                       and item is not exclude
                       and str(extra.get("userid", "")).casefold() not in item.ignored]
        self.notify(targets, kind, message, **extra)

    def display_name(self, client: Client) -> str:
        return client.chatid or client.userid

    def is_room_operator(self, client: Client, room_name: str | None = None) -> bool:
        target = room_name or client.room
        return (client.is_sysop or self.room_owners.get(target) == client.account_id
                or client.account_id in self.room_operators.get(target, set()))

    def join_room(self, client: Client, target: str) -> None:
        with self.lock:
            if target not in self.room_owners:
                self.room_owners[target] = client.account_id
                self.room_operators[target] = {client.account_id}
                self.room_flags[target] = set()
            flags = self.room_flags.get(target, set())
            if "locked" in flags and not self.is_room_operator(client, target) \
                    and client.account_id not in self.room_invites.get(target, set()):
                raise RPCError("room is locked; an invitation is required")
            self.room_invites.setdefault(target, set()).discard(client.account_id)
            old = client.room
            client.room = target
        if old != target:
            self.broadcast(old, "leave", f"{self.display_name(client)} left {old}",
                           exclude=client, userid=client.userid)
        self.send(client, "joined", f"Joined room {target}", room=target,
                  userid=client.userid, chatid=self.display_name(client))
        self.broadcast(target, "join", f"{self.display_name(client)} joined {target}",
                       exclude=client, userid=client.userid)

    def pager_clients(self, account_id: int) -> list[Client]:
        with self.lock:
            return [item for item in self.clients
                    if item.mode == "pager" and item.account_id == account_id]

    def expire_pages(self) -> None:
        now = time.monotonic()
        with self.lock:
            expired = [page for page in self.pages.values()
                       if now - page.created > PAGE_LIFETIME
                       or page.requester not in self.clients]
            for page in expired:
                self.pages.pop(page.page_id, None)
        for page in expired:
            try:
                self.send(page.requester, "page_expired",
                          f"Your page to {page.target_userid} expired.",
                          page_id=page.page_id, userid=page.target_userid)
            except OSError:
                pass
            self.notify(self.pager_clients(page.target_account_id), "page_closed",
                        f"Page from {page.requester.userid} expired.",
                        page_id=page.page_id, userid=page.requester.userid)

    def expiry_loop(self) -> None:
        while True:
            time.sleep(1.0)
            self.expire_pages()

    def end_talk(self, client: Client) -> None:
        with self.lock:
            partner = client.talk_peer
            client.talk_peer = None
            client.talk_typing = False
            if partner is not None and partner.talk_peer is client:
                partner.talk_peer = None
                partner.talk_typing = False
        if partner is not None:
            try:
                self.send(partner, "talk_ended", "[Direct Talk Closed]",
                          userid=client.userid)
            except OSError:
                pass

    def remove(self, client: Client) -> None:
        self.end_talk(client)
        removed = False
        cancelled: list[Page] = []
        unreachable: list[Page] = []
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
                removed = True
            for page_id, page in list(self.pages.items()):
                if page.requester is client:
                    cancelled.append(page)
                    del self.pages[page_id]
                elif page.target_account_id == client.account_id and not any(
                        item.mode == "pager" and item.account_id == client.account_id
                        for item in self.clients):
                    unreachable.append(page)
                    del self.pages[page_id]
        for page in cancelled:
            self.notify(self.pager_clients(page.target_account_id), "page_cancelled",
                        f"Page from {client.userid} was cancelled.",
                        page_id=page.page_id, userid=client.userid)
        for page in unreachable:
            try:
                self.send(page.requester, "page_cancelled",
                          f"{client.userid} disconnected before answering.",
                          page_id=page.page_id, userid=client.userid)
            except OSError:
                pass
        if removed and client.mode == "chat":
            self.broadcast(client.room, "leave", f"{client.userid} left {client.room}",
                           exclude=client, userid=client.userid)
        elif removed and client.mode == "pager":
            with self.lock:
                still_online = any(item.mode == "pager" and item.account_id == client.account_id
                                   for item in self.clients)
                watchers = [item for item in self.clients if item.mode == "pager"
                            and client.userid.casefold() in item.monitors]
            if not still_online:
                self.notify(watchers, "monitor", f"{client.userid} went offline.",
                            userid=client.userid, online=False)
        try:
            client.conn.close()
        except OSError:
            pass

    def create_page(self, client: Client, target_name: str) -> None:
        if client.mode != "pager":
            raise RPCError("paging requires the Talk pager")
        if target_name == client.userid:
            raise RPCError("you cannot page yourself")
        with self.lock:
            targets = [item for item in self.clients
                       if item.mode == "pager" and item.userid == target_name
                       and item.pager_available]
            if not targets:
                self.send(client, "error", "That user is not available for paging.")
                return
            if client.talk_peer is not None:
                self.send(client, "error", "You are already in Direct Talk.")
                return
            duplicate = next((page for page in self.pages.values()
                              if page.requester is client
                              and page.target_account_id == targets[0].account_id), None)
            if duplicate is not None:
                self.send(client, "error", "You already paged that user.")
                return
            if sum(1 for page in self.pages.values()
                   if page.target_account_id == targets[0].account_id) >= 8:
                self.send(client, "error", "That user's pager is busy.")
                return
            pending = Page(secrets.token_urlsafe(24), client, targets[0].account_id,
                           targets[0].userid, time.monotonic())
            self.pages[pending.page_id] = pending
        self.notify(targets, "page",
                    f"*** PAGE from {client.userid} -- open Talk and choose Answer ***",
                    page_id=pending.page_id, userid=client.userid,
                    display_name=client.display_name)
        self.send(client, "page_sent",
                  f"Paging {target_name}. Waiting up to two minutes for an answer...",
                  page_id=pending.page_id, userid=target_name)

    def answer_page(self, client: Client, page_id: str) -> None:
        if client.mode != "pager":
            raise RPCError("paging requires the Talk pager")
        with self.lock:
            pending = self.pages.get(page_id)
            if pending is None or pending.target_account_id != client.account_id:
                self.send(client, "error", "That page is no longer available.")
                return
            requester = pending.requester
            if requester not in self.clients or requester.talk_peer is not None \
                    or client.talk_peer is not None:
                del self.pages[page_id]
                self.send(client, "error", "That caller is no longer available.")
                return
            requester.talk_peer = client
            client.talk_peer = requester
            del self.pages[page_id]
            other_targets = [item for item in self.pager_clients(client.account_id)
                             if item is not client]
        self.notify(other_targets, "page_closed", f"Page from {requester.userid} was answered.",
                    page_id=page_id, userid=requester.userid)
        self.send(requester, "talk_started",
                  f"[{client.userid} connected]",
                  userid=client.userid, display_name=client.display_name)
        self.send(client, "talk_started",
                  f"[{requester.userid} connected]",
                  userid=requester.userid, display_name=requester.display_name)

    def decline_page(self, client: Client, page_id: str) -> None:
        with self.lock:
            pending = self.pages.get(page_id)
            if pending is None or pending.target_account_id != client.account_id:
                self.send(client, "error", "That page is no longer available.")
                return
            del self.pages[page_id]
        self.send(pending.requester, "page_declined",
                  f"{client.userid} declined your page.",
                  page_id=page_id, userid=client.userid)
        self.notify([item for item in self.pager_clients(client.account_id)
                     if item is not client], "page_closed",
                    f"Page from {pending.requester.userid} was declined.",
                    page_id=page_id, userid=pending.requester.userid)

    def client_loop(self, conn: socket.socket) -> None:
        client: Client | None = None
        try:
            peer = peer_credentials(conn)
            raw = conn.recv(MAX_PACKET + 1)
            req = decode_packet(raw)
            if req["op"] == "health":
                conn.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "health",
                                            "ok": True,
                                            "data": {"status": "ok",
                                                     "clients": len(self.clients)}}))
                return
            if req["op"] != "join":
                raise RPCError("join required")
            ident = validate(self.session_socket, req.get("token"), peer[1])
            token = req.get("token")
            if not isinstance(token, str):
                raise RPCError("authentication required")
            mode = req["data"].get("mode", "chat")
            if mode not in ("chat", "pager"):
                raise RPCError("invalid Chat mode")
            initial_room = room(req["data"].get("room", "lobby")) \
                if mode == "chat" else "lobby"
            if initial_room == self.bridge_room and self.bridge_socket is None:
                raise RPCError("MarsHotel is public IRC; use the IRC Center")
            client = Client(conn=conn, token=token, peer_uid=peer[1],
                            account_id=int(ident["account_id"]),
                            userid=str(ident["userid"]),
                            display_name=str(ident["display_name"]), mode=str(mode),
                            room=initial_room, chatid=str(ident["userid"]),
                            is_sysop=bool(ident.get("is_admin")))
            with self.lock:
                if initial_room not in self.room_owners:
                    self.room_owners[initial_room] = client.account_id
                    self.room_operators[initial_room] = {client.account_id}
                    self.room_flags[initial_room] = set()
                self.clients.append(client)
            if client.mode == "chat":
                self.send(client, "joined", f"Connected to native Chat room {client.room}",
                          room=client.room, userid=client.userid,
                          chatid=self.display_name(client))
                self.send(client, "motd", self.motd, room=client.room)
                with self.lock:
                    bridge_status = self.bridge_status.get(client.room)
                if bridge_status:
                    self.send(client, "bridge", bridge_status, room=client.room,
                              adapter=self.bridge_adapter)
                self.broadcast(client.room, "join", f"{client.userid} joined {client.room}",
                               exclude=client, userid=client.userid)
            else:
                self.send(client, "pager_ready", "Pager ready", userid=client.userid,
                          available=client.pager_available)
                with self.lock:
                    watchers = [item for item in self.clients if item.mode == "pager"
                                and client.userid.casefold() in item.monitors]
                self.notify(watchers, "monitor", f"{client.userid} is now online.",
                            userid=client.userid, online=True)
            while True:
                raw = conn.recv(MAX_PACKET + 1)
                if not raw:
                    break
                req = decode_packet(raw)
                if req.get("token") != client.token:
                    raise RPCError("session mismatch")
                validate(self.session_socket, client.token, client.peer_uid)
                self.expire_pages()
                data = req["data"]
                op = req["op"]
                if op == "message" and client.mode == "chat":
                    message = text(data.get("message"), "chat message", 512, 1)
                    self.broadcast(client.room, "message",
                                   f"<{self.display_name(client)}> {message}", userid=client.userid,
                                   room=client.room)
                    self.bridge_publish(client.room, "message", userid=client.userid,
                                        message=message)
                elif op == "join_room" and client.mode == "chat":
                    target = room(data.get("room"))
                    if target == self.bridge_room and self.bridge_socket is None:
                        self.send(client, "error",
                                  "MarsHotel is public IRC; use the IRC Center")
                        continue
                    if target != client.room:
                        self.join_room(client, target)
                        with self.lock:
                            bridge_status = self.bridge_status.get(target)
                        if bridge_status:
                            self.send(client, "bridge", bridge_status, room=target,
                                      adapter=self.bridge_adapter)
                elif op == "action" and client.mode == "chat":
                    message = text(data.get("message"), "Chat action", 480, 1)
                    self.broadcast(client.room, "action",
                                   f"* {self.display_name(client)} {message}",
                                   userid=client.userid, room=client.room)
                    self.bridge_publish(client.room, "action", userid=client.userid,
                                        message=message)
                elif op == "private" and client.mode == "chat":
                    target_name = userid(data.get("userid"))
                    message = text(data.get("message"), "private message", 512, 1)
                    with self.lock:
                        targets = [item for item in self.clients
                                   if item.mode == "chat" and item.userid == target_name]
                    if not targets:
                        self.send(client, "error", "User is not in Chat")
                    else:
                        targets = [item for item in targets
                                   if client.userid.casefold() not in item.ignored]
                        if not targets:
                            self.send(client, "error", "User is not accepting your messages")
                            continue
                        self.notify(targets, "private", f"*{self.display_name(client)}* {message}",
                                    userid=client.userid)
                        self.send(client, "private", f"*to {target_name}* {message}",
                                  userid=client.userid)
                elif op == "who" and client.mode == "chat":
                    with self.lock:
                        users = sorted({self.display_name(item) for item in self.clients
                                        if item.mode == "chat" and item.room == client.room})
                        irc_users = sorted(self.bridge_users.get(client.room, {}).values(),
                                           key=str.casefold)
                        channel = self.bridge_channels.get(client.room)
                    self.send(client, "who", "Users in room: " + ", ".join(users),
                              users=users, room=client.room)
                    if channel is not None:
                        visible = ", ".join(f"[IRC] {nick}" for nick in irc_users) \
                            if irc_users else "none"
                        self.send(client, "who", f"IRC users on {channel}: {visible}",
                                  users=irc_users, room=client.room, channel=channel,
                                  source="irc")
                elif op == "rooms" and client.mode == "chat":
                    with self.lock:
                        counts: dict[str, int] = {}
                        for item in self.clients:
                            if item.mode == "chat":
                                counts[item.room] = counts.get(item.room, 0) + 1
                        visible = [name for name in sorted(self.room_owners)
                                   if "secret" not in self.room_flags.get(name, set())
                                   or name == client.room or self.is_room_operator(client, name)]
                    self.send(client, "rooms", "Rooms: " + ", ".join(
                        f"{name} ({counts.get(name, 0)})" for name in visible), rooms=visible)
                elif op == "whois" and client.mode == "chat":
                    name = userid(data.get("userid"))
                    with self.lock:
                        target = next((item for item in self.clients if item.mode == "chat" and
                                       (item.userid.casefold() == name.casefold() or
                                        self.display_name(item).casefold() == name.casefold())), None)
                    if target is None:
                        self.send(client, "error", "User is not in Chat")
                    else:
                        self.send(client, "whois",
                                  f"{self.display_name(target)} is @{target.userid} in {target.room}",
                                  userid=target.userid, chatid=self.display_name(target),
                                  room=target.room)
                elif op == "nick" and client.mode == "chat":
                    alias = userid(data.get("chatid"))
                    with self.lock:
                        collision = any(item is not client and item.mode == "chat" and
                                        self.display_name(item).casefold() == alias.casefold()
                                        for item in self.clients)
                    if collision:
                        self.send(client, "error", "That Chat ID is already in use")
                    else:
                        old = self.display_name(client)
                        client.chatid = alias
                        self.broadcast(client.room, "nick", f"{old} is now {alias}",
                                       userid=client.userid)
                        self.send(client, "nick", f"Your Chat ID is now {alias}",
                                  chatid=alias)
                elif op in ("ignore", "unignore") and client.mode == "chat":
                    name = userid(data.get("userid"))
                    if name.casefold() == client.userid.casefold():
                        self.send(client, "error", "You cannot ignore yourself")
                    else:
                        if op == "ignore":
                            client.ignored.add(name.casefold())
                        else:
                            client.ignored.discard(name.casefold())
                        self.send(client, "ignore", f"{name} is " +
                                  ("ignored" if op == "ignore" else "no longer ignored"))
                elif op in ("invite", "room_flags", "room_op", "kick") \
                        and client.mode == "chat":
                    if not self.is_room_operator(client):
                        self.send(client, "error", "Room operator privilege required")
                        continue
                    name = userid(data.get("userid")) if op != "room_flags" else ""
                    with self.lock:
                        target = next((item for item in self.clients if item.mode == "chat" and
                                       item.userid.casefold() == name.casefold()), None) \
                            if name else None
                        if op == "invite":
                            if target is None:
                                raise RPCError("User is not in Chat")
                            self.room_invites.setdefault(client.room, set()).add(target.account_id)
                        elif op == "room_flags":
                            flag = text(data.get("flag"), "room flag", 12, 1)
                            enabled = data.get("enabled")
                            if flag not in ("locked", "secret") or not isinstance(enabled, bool):
                                raise RPCError("invalid room flag")
                            flags = self.room_flags.setdefault(client.room, set())
                            flags.add(flag) if enabled else flags.discard(flag)
                        elif op == "room_op":
                            if target is None or target.room != client.room:
                                raise RPCError("User is not in this room")
                            self.room_operators.setdefault(client.room, set()).add(target.account_id)
                        elif op == "kick":
                            if target is None or target.room != client.room:
                                raise RPCError("User is not in this room")
                            target.room = "lobby"
                    if op == "invite":
                        self.send(target, "invite", f"{client.userid} invited you to {client.room}",
                                  room=client.room, userid=client.userid)
                        self.send(client, "invite", f"Invited {name} to {client.room}")
                    elif op == "room_flags":
                        self.broadcast(client.room, "room_flags",
                                       f"Room {data['flag']} is " +
                                       ("on" if data["enabled"] else "off"),
                                       userid=client.userid)
                    elif op == "room_op":
                        self.broadcast(client.room, "room_op", f"{name} is now a room operator",
                                       userid=client.userid)
                    else:
                        self.send(target, "kicked", f"Removed from {client.room} by {client.userid}; joined lobby",
                                  room="lobby")
                        self.broadcast(client.room, "kick", f"{name} was removed by {client.userid}",
                                       exclude=target, userid=client.userid)
                        self.broadcast("lobby", "join", f"{name} joined lobby",
                                       exclude=target, userid=target.userid)
                elif op == "motd" and client.mode == "chat":
                    self.send(client, "motd", self.motd, room=client.room)
                elif op == "page":
                    self.create_page(client, userid(data.get("userid")))
                elif op == "answer_page":
                    page_id = text(data.get("page_id"), "page id", 64, 16)
                    self.answer_page(client, page_id)
                elif op == "decline_page":
                    page_id = text(data.get("page_id"), "page id", 64, 16)
                    self.decline_page(client, page_id)
                elif op == "cancel_page":
                    page_id = text(data.get("page_id"), "page id", 64, 16)
                    with self.lock:
                        pending = self.pages.get(page_id)
                        if pending is None or pending.requester is not client:
                            self.send(client, "error", "That page is no longer available.")
                        else:
                            del self.pages[page_id]
                            targets = self.pager_clients(pending.target_account_id)
                            self.notify(targets, "page_cancelled",
                                        f"Page from {client.userid} was cancelled.",
                                        page_id=page_id, userid=client.userid)
                            self.send(client, "page_cancelled", "Page cancelled.",
                                      page_id=page_id, userid=pending.target_userid)
                elif op == "pager_status" and client.mode == "pager":
                    available = data.get("available")
                    if not isinstance(available, bool):
                        raise RPCError("invalid pager availability")
                    client.pager_available = available
                    self.send(client, "pager_status",
                              "Pager is " + ("available" if available else "do not disturb"),
                              available=available)
                elif op in ("monitor", "unmonitor") and client.mode == "pager":
                    name = userid(data.get("userid"))
                    if name.casefold() == client.userid.casefold():
                        self.send(client, "error", "You cannot monitor yourself")
                    else:
                        if op == "monitor":
                            client.monitors.add(name.casefold())
                        else:
                            client.monitors.discard(name.casefold())
                        self.send(client, "monitor",
                                  f"{name} " + ("added to" if op == "monitor" else
                                                 "removed from") + " your monitor list",
                                  userid=name)
                elif op == "talk_message":
                    message = text(data.get("message"), "Talk message", 512, 1)
                    with self.lock:
                        partner = client.talk_peer
                        was_typing = client.talk_typing
                        client.talk_typing = False
                    if partner is None:
                        self.send(client, "error", "Direct Talk is not connected.")
                    else:
                        if was_typing:
                            self.send(partner, "talk_typing", "", userid=client.userid,
                                      typing=False)
                        self.send(partner, "talk_message", message, userid=client.userid)
                        self.send(client, "talk_message", message, userid=client.userid,
                                  own=True)
                elif op == "talk_typing":
                    typing = data.get("typing")
                    if not isinstance(typing, bool):
                        raise RPCError("invalid Talk typing state")
                    with self.lock:
                        partner = client.talk_peer
                        changed = client.talk_typing != typing
                        client.talk_typing = typing if partner is not None else False
                    if partner is None:
                        self.send(client, "error", "Direct Talk is not connected.")
                    elif changed:
                        self.send(partner, "talk_typing", "", userid=client.userid,
                                  typing=typing)
                elif op == "talk_end":
                    self.end_talk(client)
                    self.send(client, "talk_ended", "[Direct Talk Closed]",
                              userid=client.userid)
                elif op == "quit":
                    break
                else:
                    self.send(client, "error", "Unknown Chat command")
        except (RPCError, ValueError, OSError) as exc:
            if client is not None:
                try:
                    self.send(client, "error", str(exc))
                except OSError:
                    pass
        finally:
            if client is not None:
                self.remove(client)
            else:
                conn.close()

    def run(self) -> None:
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        listener.listen(64)
        threading.Thread(target=self.expiry_loop, daemon=True).start()
        if self.bridge_socket is not None:
            threading.Thread(target=self.bridge_accept_loop, daemon=True).start()
        while True:
            conn, _ = listener.accept()
            thread = threading.Thread(target=self.client_loop, args=(conn,), daemon=True)
            thread.start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--bridge-socket")
    parser.add_argument("--bridge-secret-file")
    parser.add_argument("--bridge-uid", type=int)
    parser.add_argument("--bridge-adapter", default="synchronet")
    parser.add_argument("--bridge-room", default="marshotel")
    parser.add_argument("--motd-file")
    args = parser.parse_args()
    Chat(args.socket, args.session_socket, args.bridge_socket,
         args.bridge_secret_file, args.bridge_uid, args.bridge_adapter,
         args.bridge_room, args.motd_file).run()


if __name__ == "__main__":
    main()
