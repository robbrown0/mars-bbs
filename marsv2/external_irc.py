"""Outbound-only bridge from one native Chat room to one external IRC channel."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import re
import select
import secrets
import socket
import ssl
import stat
import time
import unicodedata
from dataclasses import dataclass

from . import PROTOCOL_VERSION
from .rpc import MAX_PACKET, RPCError, decode_packet, encode_packet
from .validate import room


IRC_LINE_MAX = 512
NICKSERV_REFRESH_SECONDS = 24 * 60 * 60
CHANNEL_RE = re.compile(r"^#[^\x00\x07\r\n ,:]{1,49}$")
NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}$")
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|P[^\x1b]*(?:\x1b\\)|[@-_])"
)
IRC_COLOR_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?")
IRC_FORMAT_RE = re.compile(r"[\x02\x0f\x11\x16\x1d\x1e\x1f]")


def private_text(path: str, name: str, minimum: int = 12,
                 maximum: int = 160) -> str:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError(f"{name} must be a private regular file")
    with open(path, encoding="utf-8") as stream:
        value = stream.read().rstrip("\r\n")
    if len(value) < minimum or len(value) > maximum \
            or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise RuntimeError(f"invalid {name}")
    return value


def bridge_secret(path: str) -> str:
    value = private_text(path, "bridge secret", 64, 64)
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        raise RuntimeError("bridge secret must contain 32 hexadecimal bytes")
    return value.lower()


def sanitize_external(value: str, maximum_bytes: int = 400) -> str:
    value = ANSI_RE.sub("", value)
    value = IRC_COLOR_RE.sub("", value)
    value = IRC_FORMAT_RE.sub("", value)
    value = unicodedata.normalize("NFC", value)
    value = "".join(char for char in value
                    if ord(char) >= 32 and not 0x7f <= ord(char) <= 0x9f
                    and unicodedata.category(char) != "Cf")
    raw = value.encode("utf-8")[:maximum_bytes]
    while True:
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raw = raw[:-1]


def display_nick(value: str) -> str:
    value = sanitize_external(value, 32)
    value = re.sub(r"[^A-Za-z0-9_\-\[\]\\`^{|}]", "_", value)
    return value[:32] or "unknown"


def parse_irc(line: str) -> tuple[str, list[str]]:
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    parts = line.split()
    if trailing is not None:
        parts.append(trailing)
    return prefix, parts


def truncate_irc(prefix: str, value: str) -> str:
    available = IRC_LINE_MAX - 2 - len(prefix.encode("utf-8"))
    raw = value.encode("utf-8")[:max(0, available)]
    while True:
        try:
            return prefix + raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]


def irc_send(sock: ssl.SSLSocket, line: str) -> None:
    raw = line.encode("utf-8")
    if len(raw) + 2 > IRC_LINE_MAX or any(char in raw for char in b"\r\n\0"):
        raise RPCError("invalid IRC output")
    sock.sendall(raw + b"\r\n")


@dataclass(frozen=True)
class Adapter:
    name: str
    host: str
    port: int
    server_name: str
    network: str
    channel: str
    room: str
    nick: str
    enabled: bool
    require_nick_identification: bool
    channel_topic: str = ""
    info_message: str = ""


def load_adapter(path: str, selected: str) -> Adapter:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise RuntimeError("external IRC configuration must not be writable by group/other")
    with open(path, encoding="utf-8") as stream:
        obj = json.load(stream)
    if not isinstance(obj, dict) or set(obj) != {"version", "adapters"} \
            or obj["version"] != 1 or not isinstance(obj["adapters"], dict):
        raise RuntimeError("invalid external IRC configuration")
    raw = obj["adapters"].get(selected)
    required = {"enabled", "host", "port", "server_name", "network", "channel",
                "room", "nick", "require_nick_identification"}
    optional = {"channel_topic", "info_message"}
    if not isinstance(raw, dict) or not required.issubset(raw) \
            or not set(raw).issubset(required | optional):
        raise RuntimeError("invalid external IRC adapter")
    if not isinstance(raw["enabled"], bool) or not raw["enabled"]:
        raise RuntimeError("selected external IRC adapter is disabled")
    if not isinstance(raw["require_nick_identification"], bool):
        raise RuntimeError("invalid nickname-identification policy")
    string_fields = ("host", "server_name", "network", "channel", "room", "nick")
    if any(not isinstance(raw[field], str) for field in string_fields):
        raise RuntimeError("invalid external IRC adapter field")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", raw["host"]) \
            or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", raw["server_name"]):
        raise RuntimeError("invalid IRC hostname")
    if isinstance(raw["port"], bool) or not isinstance(raw["port"], int) \
            or not 1 <= raw["port"] <= 65535:
        raise RuntimeError("invalid IRC port")
    if not CHANNEL_RE.fullmatch(raw["channel"]):
        raise RuntimeError("invalid IRC channel")
    if not NICK_RE.fullmatch(raw["nick"]):
        raise RuntimeError("invalid IRC nickname")
    for field in optional:
        if field in raw and not isinstance(raw[field], str):
            raise RuntimeError(f"invalid external IRC {field}")
    channel_topic = sanitize_external(raw.get("channel_topic", ""), 300)
    info_message = sanitize_external(raw.get("info_message", ""), 400)
    return Adapter(selected, raw["host"], raw["port"], raw["server_name"],
                   sanitize_external(raw["network"], 64), raw["channel"],
                   room(raw["room"]), raw["nick"], raw["enabled"],
                   raw["require_nick_identification"], channel_topic, info_message)


class ChatLink:
    def __init__(self, path: str, secret: str, adapter: Adapter):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.connect(path)
        self.sock.sendall(encode_packet({
            "v": PROTOCOL_VERSION,
            "op": "bridge_hello",
            "data": {"adapter": adapter.name, "room": adapter.room,
                     "secret": secret},
        }))
        reply = decode_packet(self.sock.recv(MAX_PACKET + 1))
        if reply["op"] != "local_event" or reply["data"].get("kind") != "bridge_ready":
            raise RuntimeError("chat service rejected the external bridge")

    def event(self, kind: str, **data: object) -> None:
        self.sock.sendall(encode_packet({"v": PROTOCOL_VERSION,
                                         "op": "external_event",
                                         "data": {"kind": kind, **data}}))

    def receive(self) -> dict[str, object]:
        packet = decode_packet(self.sock.recv(MAX_PACKET + 1))
        if packet["op"] != "local_event":
            raise RPCError("invalid chat bridge packet")
        return packet["data"]


class State:
    def __init__(self, path: str, adapter: Adapter):
        self.path = path
        self.adapter = adapter
        self.failures = 0
        self.last_identified_utc = 0

    def write(self, status: str, error: str = "") -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        data = {
            "version": 1,
            "adapter": self.adapter.name,
            "network": self.adapter.network,
            "channel": self.adapter.channel,
            "room": self.adapter.room,
            "status": status,
            "failures": self.failures,
            "updated_utc": int(time.time()),
            "last_identified_utc": self.last_identified_utc,
            "last_error": sanitize_external(error, 160),
        }
        # Containers commonly reuse PID 1.  A predictable PID-only name can
        # collide with an orphan left by an interrupted write on the volume.
        temporary = self.path + f".tmp.{os.getpid()}.{secrets.token_hex(8)}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class ExternalBridge:
    def __init__(self, adapter: Adapter, chat: ChatLink | None, state: State,
                 nick_password: str | None, channel_password: str | None,
                 cafile: str | None,
                 identify_interval: float = NICKSERV_REFRESH_SECONDS):
        self.adapter = adapter
        self.chat = chat
        self.state = state
        self.nick_password = nick_password
        self.channel_password = channel_password
        self.context = ssl.create_default_context(cafile=cafile)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.random = random.SystemRandom()
        self.members: dict[str, str] = {}
        self.recent: dict[str, float] = {}
        self.incoming: collections.deque[float] = collections.deque()
        self.buffer = b""
        self.current_nick = adapter.nick
        self.connected_at: float | None = None
        self.last_info_reply = 0.0
        if identify_interval <= 0:
            raise ValueError("nickname-identification interval must be positive")
        self.identify_interval = identify_interval

    def status(self, state: str) -> None:
        if self.chat is not None:
            self.chat.event("status", state=state, channel=self.adapter.channel)

    def external_event(self, kind: str, nick: str, message: str = "",
                       state: str = "") -> None:
        if self.chat is None:
            return
        data: dict[str, object] = {"nick": display_nick(nick)}
        if message:
            data["message"] = sanitize_external(message)
        if state:
            data["state"] = state
        self.chat.event(kind, **data)

    def presence_sync(self) -> None:
        """Replace Chat's IRC roster with the server's bounded NAMES view."""
        if self.chat is not None:
            self.chat.event("presence_sync",
                            nicks=sorted(self.members.values(), key=str.casefold))

    def remember_member(self, nick: str) -> tuple[str, bool]:
        shown = display_nick(nick)
        key = shown.casefold()
        if key == self.current_nick.casefold():
            return shown, False
        added = key not in self.members
        if added and len(self.members) >= 256:
            return shown, False
        self.members[key] = shown
        return shown, added

    def forget_member(self, nick: str) -> str | None:
        return self.members.pop(display_nick(nick).casefold(), None)

    def remember_names(self, raw_names: str) -> None:
        for raw in raw_names.split():
            # RFC channel status prefixes precede the nickname.  Some servers
            # may additionally negotiate userhost-in-names; retain only nick.
            nick = raw.lstrip("~&@%+").split("!", 1)[0]
            if nick:
                self.remember_member(nick)

    def remember(self, sender: str, target: str, message: str) -> bool:
        now = time.monotonic()
        self.recent = {key: when for key, when in self.recent.items() if now - when < 15}
        digest = hashlib.sha256(f"{sender.casefold()}\0{target.casefold()}\0{message}".encode()).hexdigest()
        if digest in self.recent:
            return False
        self.recent[digest] = now
        return True

    def accept_incoming(self) -> bool:
        """Bound public-channel traffic before it reaches local terminals."""
        now = time.monotonic()
        while self.incoming and now - self.incoming[0] >= 5.0:
            self.incoming.popleft()
        if len(self.incoming) >= 8:
            return False
        self.incoming.append(now)
        return True

    def recv_lines(self, remote: ssl.SSLSocket) -> list[str]:
        try:
            block = remote.recv(4096)
        except ssl.SSLWantReadError:
            return []
        if not block:
            raise ConnectionError("server closed the TLS connection")
        self.buffer += block
        if len(self.buffer) > 16384:
            raise RPCError("IRC receive buffer exceeded")
        lines = []
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            raw = raw.rstrip(b"\r")
            if len(raw) + 2 > IRC_LINE_MAX:
                raise RPCError("oversized IRC input")
            lines.append(raw.decode("utf-8", "replace"))
        return lines

    def wait_registration(self, remote: ssl.SSLSocket) -> None:
        deadline = time.monotonic() + 20
        collisions = 0
        irc_send(remote, f"NICK {self.current_nick}")
        irc_send(remote, f"USER {self.current_nick} 0 * :Mars Hotel IRC bridge")
        while time.monotonic() < deadline:
            readable, _, _ = select.select([remote], [], [], 1)
            if not readable:
                continue
            for line in self.recv_lines(remote):
                prefix, parts = parse_irc(line)
                if not parts:
                    continue
                command = parts[0].upper()
                if command == "PING" and len(parts) > 1:
                    irc_send(remote, f"PONG :{sanitize_external(parts[-1], 200)}")
                elif command == "001":
                    return
                elif command in {"432", "433", "436"}:
                    collisions += 1
                    if collisions > 4:
                        raise ConnectionError("IRC nickname collision limit exceeded")
                    suffix = self.random.randrange(10, 100)
                    self.current_nick = (self.adapter.nick[:27] + str(suffix))[:30]
                    irc_send(remote, f"NICK {self.current_nick}")
                elif command == "ERROR" or command in {"451", "463", "464", "465"}:
                    raise ConnectionError("IRC registration rejected")
        raise TimeoutError("IRC registration timed out")

    def nickserv_identification_result(self, prefix: str,
                                       parts: list[str]) -> bool | None:
        """Recognize only explicit NickServ authentication outcomes."""
        if not parts:
            return None
        command = parts[0].upper()
        if command in {"900", "903"}:
            return True
        sender = prefix.split("!", 1)[0]
        if command not in {"NOTICE", "PRIVMSG"} \
                or sender.casefold() != "nickserv":
            return None
        notice = sanitize_external(parts[-1], 240).casefold()
        failures = ("password incorrect", "incorrect password", "password denied",
                    "invalid password", "authentication failed", "access denied",
                    "nickname is not registered", "nick is not registered")
        if any(marker in notice for marker in failures):
            return False
        successes = ("password accepted", "you are now recognized",
                     "already identified", "already recognized",
                     "already logged in")
        if any(marker in notice for marker in successes):
            return True
        return None

    def identify_nickserv(self, remote: ssl.SSLSocket,
                          joined: bool = False) -> None:
        """Identify and require an explicit acknowledgement before proceeding."""
        if not self.adapter.require_nick_identification:
            return
        if not self.nick_password:
            raise RuntimeError("registered nickname password is required")
        irc_send(remote, f"PRIVMSG NickServ :IDENTIFY {self.nick_password}")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            readable, _, _ = select.select([remote], [], [], 0.5)
            if not readable:
                continue
            for line in self.recv_lines(remote):
                prefix, parts = parse_irc(line)
                command = parts[0].upper() if parts else ""
                if command == "PING" and len(parts) > 1:
                    irc_send(remote, f"PONG :{sanitize_external(parts[-1], 200)}")
                    continue
                result = self.nickserv_identification_result(prefix, parts)
                if result is False:
                    raise PermissionError("NickServ identification failed")
                if result is True:
                    self.state.last_identified_utc = int(time.time())
                    return
                if joined:
                    # Do not lose channel traffic that arrived while waiting for
                    # the periodic NickServ acknowledgement.
                    self.handle_remote(remote, line)
        raise PermissionError("NickServ identification was not confirmed")

    def identify_and_join(self, remote: ssl.SSLSocket) -> None:
        self.identify_nickserv(remote)
        if self.channel_password:
            irc_send(remote, f"PRIVMSG ChanServ :IDENTIFY {self.adapter.channel} {self.channel_password}")
        irc_send(remote, f"JOIN {self.adapter.channel}")
        deadline = time.monotonic() + 15
        joined_at: float | None = None
        names_complete = False
        topic_known = not bool(self.adapter.channel_topic)
        topic_set = False
        while time.monotonic() < deadline:
            if joined_at is not None and topic_known \
                    and (names_complete or time.monotonic() - joined_at >= 3.0):
                self.presence_sync()
                return
            readable, _, _ = select.select([remote], [], [], 1)
            if not readable:
                continue
            for line in self.recv_lines(remote):
                prefix, parts = parse_irc(line)
                if not parts:
                    continue
                command = parts[0].upper()
                sender = prefix.split("!", 1)[0]
                if command == "PING" and len(parts) > 1:
                    irc_send(remote, f"PONG :{sanitize_external(parts[-1], 200)}")
                elif command == "JOIN" and sender.casefold() == self.current_nick.casefold():
                    joined_at = time.monotonic()
                    irc_send(remote, f"NAMES {self.adapter.channel}")
                    if self.adapter.channel_topic:
                        irc_send(remote, f"TOPIC {self.adapter.channel}")
                elif command == "331" and len(parts) >= 3 \
                        and parts[-2].casefold() == self.adapter.channel.casefold():
                    topic_known = True
                    if not topic_set:
                        irc_send(remote, truncate_irc(
                            f"TOPIC {self.adapter.channel} :", self.adapter.channel_topic))
                        topic_set = True
                elif command == "332" and len(parts) >= 4 \
                        and parts[-2].casefold() == self.adapter.channel.casefold():
                    # A channel operator has already supplied a topic. Never
                    # overwrite it merely because the bridge reconnected.
                    topic_known = True
                elif command == "353" and len(parts) >= 5 \
                        and parts[-2].casefold() == self.adapter.channel.casefold():
                    self.remember_names(parts[-1])
                elif command == "366" and len(parts) >= 3 \
                        and parts[-2].casefold() == self.adapter.channel.casefold():
                    names_complete = True
                elif command in {"403", "405", "471", "473", "474", "475", "476", "477", "489"}:
                    raise PermissionError("IRC channel join was rejected")
                else:
                    # Do not drop channel activity coalesced into the same TLS
                    # read as JOIN/NAMES completion.
                    self.handle_remote(remote, line)
        raise TimeoutError("IRC channel join timed out")

    def handle_remote(self, remote: ssl.SSLSocket, line: str) -> None:
        prefix, parts = parse_irc(line)
        if not parts:
            return
        command = parts[0].upper()
        sender = prefix.split("!", 1)[0]
        if command == "PING" and len(parts) > 1:
            irc_send(remote, f"PONG :{sanitize_external(parts[-1], 200)}")
        elif command == "353" and len(parts) >= 5 \
                and parts[-2].casefold() == self.adapter.channel.casefold():
            self.remember_names(parts[-1])
        elif command == "366" and len(parts) >= 3 \
                and parts[-2].casefold() == self.adapter.channel.casefold():
            self.presence_sync()
        elif command == "JOIN" and len(parts) >= 2 \
                and parts[-1].casefold() == self.adapter.channel.casefold() \
                and sender.casefold() != self.current_nick.casefold():
            shown, added = self.remember_member(sender)
            if added:
                self.external_event("presence", shown, state="joined")
        elif command == "PRIVMSG" and len(parts) >= 3 \
                and parts[1].casefold() == self.adapter.channel.casefold():
            if sender.casefold() == self.current_nick.casefold():
                return
            message = parts[-1]
            if not self.remember(sender, parts[1], message):
                return
            if not self.accept_incoming():
                return
            clean_message = sanitize_external(message)
            if clean_message.casefold() in {"!info", "!connect", "!motd"} \
                    and self.adapter.info_message:
                now = time.monotonic()
                if now - self.last_info_reply >= 10.0:
                    irc_send(remote, truncate_irc(
                        f"PRIVMSG {self.adapter.channel} :", self.adapter.info_message))
                    self.last_info_reply = now
                return
            shown, added = self.remember_member(sender)
            if added:
                self.external_event("presence", shown, state="joined")
            if message.startswith("\x01ACTION ") and message.endswith("\x01"):
                action = sanitize_external(message[8:-1])
                if action:
                    self.external_event("action", sender, action)
            elif not message.startswith("\x01"):
                message = clean_message
                if message:
                    self.external_event("message", sender, message)
        elif command in {"PART", "QUIT"}:
            shown = self.forget_member(sender)
            if shown is not None:
                self.external_event("presence", shown, state="left")
        elif command == "KICK" and len(parts) >= 3:
            target = parts[2]
            if target.casefold() == self.current_nick.casefold():
                raise ConnectionError("bridge was removed from the IRC channel")
            shown = self.forget_member(target)
            if shown is not None:
                self.external_event("presence", shown, state="left")
        elif command == "NICK" and len(parts) > 1:
            old = self.forget_member(sender)
            if old is not None:
                shown, _ = self.remember_member(parts[-1])
                self.external_event("presence", old, state="left")
                self.external_event("presence", shown, state="joined")
        elif command == "ERROR":
            raise ConnectionError("IRC server error")

    def run_connection(self) -> None:
        raw = socket.create_connection((self.adapter.host, self.adapter.port), timeout=12)
        remote = self.context.wrap_socket(raw, server_hostname=self.adapter.server_name)
        try:
            remote.setblocking(False)
            self.buffer = b""
            self.current_nick = self.adapter.nick
            self.members.clear()
            self.recent.clear()
            self.incoming.clear()
            self.wait_registration(remote)
            self.identify_and_join(remote)
            self.connected_at = time.monotonic()
            self.status("online")
            self.state.failures = 0
            self.state.write("online")
            outbound: collections.deque[str] = collections.deque(maxlen=20)
            next_send = 0.0
            next_state_write = time.monotonic() + 60.0
            next_identify = time.monotonic() + self.identify_interval
            while True:
                now = time.monotonic()
                timeout = 5.0
                if outbound:
                    timeout = min(timeout, max(0.0, next_send - now))
                if self.adapter.require_nick_identification:
                    timeout = min(timeout, max(0.0, next_identify - now))
                sources = [remote]
                if self.chat is not None:
                    sources.append(self.chat.sock)
                readable, _, _ = select.select(sources, [], [], timeout)
                if remote in readable:
                    for line in self.recv_lines(remote):
                        self.handle_remote(remote, line)
                if self.chat is not None and self.chat.sock in readable:
                    event = self.chat.receive()
                    if event.get("kind") == "message" and event.get("room") == self.adapter.room:
                        user = re.sub(r"[^A-Za-z0-9_-]", "_", str(event.get("userid", "")))[:12]
                        message = sanitize_external(str(event.get("message", "")))
                        if user and message and len(outbound) < outbound.maxlen:
                            outbound.append(f"[BBS] <{user}> {message}")
                now = time.monotonic()
                if outbound and now >= next_send:
                    prefix = f"PRIVMSG {self.adapter.channel} :"
                    irc_send(remote, truncate_irc(prefix, outbound.popleft()))
                    next_send = now + 1.0
                if self.adapter.require_nick_identification and now >= next_identify:
                    self.identify_nickserv(remote, joined=True)
                    self.state.write("online")
                    next_identify = time.monotonic() + self.identify_interval
                if now >= next_state_write:
                    self.state.write("online")
                    next_state_write = now + 60.0
        finally:
            remote.close()

    def discard_local_during_backoff(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            timeout = min(1.0, deadline - time.monotonic())
            if self.chat is None:
                time.sleep(timeout)
            else:
                readable, _, _ = select.select([self.chat.sock], [], [], timeout)
                if self.chat.sock in readable:
                    self.chat.receive()  # Deliberately no offline queue or stale replay.

    def run(self) -> None:
        delay = 1.0
        while True:
            self.connected_at = None
            try:
                self.run_connection()
                error = "connection ended"
            except (OSError, ssl.SSLError, RPCError, ValueError, RuntimeError) as exc:
                error = type(exc).__name__
            # A completed registration/join proves the endpoint recovered.
            # Start the next outage at the minimum delay instead of retaining
            # a penalty accumulated during an earlier outage.
            if self.connected_at is not None:
                delay = 1.0
            self.state.failures += 1
            self.state.write("offline", error)
            self.status("offline")
            wait = min(120.0, delay) + self.random.uniform(0.0, min(5.0, delay * 0.25))
            self.discard_local_during_backoff(wait)
            delay = min(120.0, delay * 2.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--chat-socket")
    parser.add_argument("--bridge-secret-file")
    parser.add_argument("--maintenance-only", action="store_true")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--nick-password-file")
    parser.add_argument("--channel-password-file")
    parser.add_argument("--cafile")
    args = parser.parse_args()
    adapter = load_adapter(args.config, args.adapter)
    nick_password = private_text(args.nick_password_file, "nickname password") \
        if args.nick_password_file else None
    channel_password = private_text(args.channel_password_file, "channel password") \
        if args.channel_password_file else None
    if adapter.require_nick_identification and not nick_password:
        raise SystemExit("registered nickname password is required")
    if args.maintenance_only:
        if args.chat_socket or args.bridge_secret_file:
            raise SystemExit("maintenance-only mode does not accept Chat credentials")
        chat = None
    else:
        if not args.chat_socket or not args.bridge_secret_file:
            raise SystemExit("Chat socket and bridge secret are required")
        secret = bridge_secret(args.bridge_secret_file)
        chat = ChatLink(args.chat_socket, secret, adapter)
    state = State(args.state_file, adapter)
    state.write("starting")
    ExternalBridge(adapter, chat, state, nick_password, channel_password,
                   args.cafile).run()


if __name__ == "__main__":
    main()
