"""Constrained embedded-IRC bridge.

The terminal UI never receives the IRC password or a network socket.  The
bridge has no BBS data mounts and accepts only a small, parsed command set.
"""

from __future__ import annotations

import argparse
import os
import re
import select
import socket
import ssl
import stat
import threading
import unicodedata
from dataclasses import dataclass

from . import PROTOCOL_VERSION
from .identity import validate
from .rpc import MAX_PACKET, RPCError, decode_packet, encode_packet, peer_credentials
from .validate import text, userid

CHANNEL = re.compile(r"#[A-Za-z0-9_-]{1,31}\Z")
NICK = re.compile(r"[A-Za-z][A-Za-z0-9_\-\[\]\\`^{}|]{0,30}\Z")
MODE = re.compile(r"[+-][A-Za-z]{1,16}\Z")
LIST_FILTER = re.compile(r"[A-Za-z0-9_*?#,\-]{1,80}\Z")
IRC_LINE_MAX = 512
LIST_RESULT_MAX = 100
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|P[^\x1b]*(?:\x1b\\)|[@-_])"
)
IRC_COLOR_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?")
IRC_FORMAT_RE = re.compile(r"[\x02\x0f\x11\x16\x1d\x1e\x1f]")


def safe_irc_text(value: str, maximum: int = 400) -> str:
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RPCError("invalid IRC text")
    # C1 and Unicode terminal controls are excluded; printable Unicode remains.
    if any(0x80 <= ord(char) <= 0x9f for char in value):
        raise RPCError("invalid IRC text")
    return value


def display_irc_text(value: str, maximum: int = 400) -> str:
    """Sanitize hostile IRC text before it reaches an authenticated terminal."""
    value = ANSI_RE.sub("", value)
    value = IRC_COLOR_RE.sub("", value)
    value = IRC_FORMAT_RE.sub("", value)
    value = unicodedata.normalize("NFC", value)
    value = "".join(char for char in value
                    if ord(char) >= 32 and not 0x7f <= ord(char) <= 0x9f
                    and unicodedata.category(char) != "Cf")
    raw = value.encode("utf-8")[:maximum]
    while True:
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raw = raw[:-1]


def read_secret(path: str) -> str:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError("IRC secret must be a mode-0600 regular file")
    with open(path, encoding="ascii") as stream:
        secret = stream.read().rstrip("\r\n")
    if not re.fullmatch(r"[0-9A-Fa-f]{48}", secret):
        raise RuntimeError("IRC secret has invalid format")
    return secret


def send_event(conn: socket.socket, message: str, kind: str = "message",
               **extra: object) -> None:
    data: dict[str, object] = {"kind": kind,
                              "message": display_irc_text(message, 480)}
    data.update(extra)
    packet = {"v": PROTOCOL_VERSION, "op": "event", "ok": True,
              "data": data}
    conn.sendall(encode_packet(packet))


def irc_send(remote: ssl.SSLSocket, line: str) -> None:
    raw = line.encode("utf-8")
    if len(raw) + 2 > IRC_LINE_MAX or b"\r" in raw or b"\n" in raw or b"\0" in raw:
        raise RPCError("invalid IRC command")
    remote.sendall(raw + b"\r\n")


def parse_line(line: str) -> tuple[str, list[str]]:
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


@dataclass(frozen=True)
class IRCTarget:
    name: str
    host: str
    port: int
    server_name: str
    channel: str
    cafile: str | None = None
    password: str | None = None
    dedicated_channel: bool = False


class Bridge:
    def __init__(self, socket_path: str, session_socket: str, host: str, port: int,
                 server_name: str, cafile: str, password_file: str,
                 public_host: str | None = None, public_port: int = 6697,
                 public_server_name: str | None = None,
                 public_channel: str = "#MarsHotel"):
        self.socket_path = socket_path
        self.session_socket = session_socket
        self.targets = {
            "local": IRCTarget("local", host, port, server_name, "#lobby",
                               cafile, read_secret(password_file)),
        }
        if public_host:
            if not public_server_name:
                raise RuntimeError("public IRC server name is required")
            if not CHANNEL.fullmatch(public_channel):
                raise RuntimeError("invalid public IRC channel")
            self.targets["synchronet"] = IRCTarget(
                "synchronet", public_host, public_port, public_server_name,
                public_channel, dedicated_channel=True)

    @staticmethod
    def connect_remote(target: IRCTarget) -> ssl.SSLSocket:
        context = ssl.create_default_context(cafile=target.cafile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        raw = socket.create_connection((target.host, target.port), timeout=10)
        return context.wrap_socket(raw, server_hostname=target.server_name)

    def client_loop(self, conn: socket.socket) -> None:
        remote: ssl.SSLSocket | None = None
        try:
            peer = peer_credentials(conn)
            req = decode_packet(conn.recv(MAX_PACKET + 1))
            if req["op"] == "health":
                conn.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "response", "ok": True,
                                            "data": {"status": "ok"}}))
                return
            if req["op"] != "connect":
                raise RPCError("connect required")
            ident = validate(self.session_socket, req.get("token"), peer[1])
            token = req.get("token")
            if not isinstance(token, str):
                raise RPCError("authentication required")
            network = req["data"].get("network", "local")
            if not isinstance(network, str) or network not in self.targets:
                raise RPCError("unsupported IRC network")
            target = self.targets[network]
            nick = userid(ident["userid"])
            base_nick = nick
            collision_count = 0
            remote = self.connect_remote(target)
            remote.setblocking(False)
            if target.password:
                irc_send(remote, f"PASS {target.password}")
            irc_send(remote, f"NICK {nick}")
            irc_send(remote, f"USER {nick} 0 * :{nick} via Mars Hotel Revival")
            channel = target.channel
            pending_channel = ""
            buffer = b""
            join_sent = False
            announced = False
            list_results = 0
            while True:
                readable, _, _ = select.select([conn, remote], [], [], 30)
                if not readable:
                    irc_send(remote, f"PING :mars-v2-{nick}")
                    continue
                if conn in readable:
                    packet = conn.recv(MAX_PACKET + 1)
                    if not packet:
                        break
                    command = decode_packet(packet)
                    if command.get("token") != token:
                        raise RPCError("session mismatch")
                    if command["op"] == "quit":
                        irc_send(remote, "QUIT :Leaving Mars Hotel Revival")
                        break
                    if command["op"] != "input":
                        raise RPCError("unknown IRC operation")
                    line = safe_irc_text(text(command["data"].get("line"), "IRC input", 400, 1))
                    if not line.startswith("/"):
                        if not channel:
                            send_event(conn, "Join a channel first with /join #channel.",
                                       "error", channel="", nick=nick)
                            continue
                        irc_send(remote, f"PRIVMSG {channel} :{line}")
                        send_event(conn, f"<{nick}> {line}")
                    elif line.lower() == "/join" or line.lower().startswith("/join "):
                        parts = line.split(None, 1)
                        target = parts[1] if len(parts) == 2 else ""
                        if not CHANNEL.fullmatch(target):
                            send_event(conn, "Usage: /join #channel", "error",
                                       channel=channel, nick=nick)
                            continue
                        if (self.targets[network].dedicated_channel and
                                target.casefold() != self.targets[network].channel.casefold()):
                            send_event(conn, "MarsHotel public IRC is limited to #MarsHotel.",
                                       "error", channel=channel, nick=nick)
                            continue
                        pending_channel = target
                        irc_send(remote, f"JOIN {target}")
                    elif line.lower() == "/part" or line.lower().startswith("/part "):
                        if not channel:
                            send_event(conn, "You are not in a channel.", "error",
                                       channel="", nick=nick)
                            continue
                        reason = line[5:].strip() or "Leaving channel"
                        reason = safe_irc_text(reason, 200)
                        irc_send(remote, f"PART {channel} :{reason}")
                    elif line.lower() == "/msg" or line.lower().startswith("/msg "):
                        parts = line.split(None, 2)
                        if len(parts) != 3:
                            send_event(conn, "Usage: /msg nick text", "error",
                                       channel=channel, nick=nick)
                            continue
                        target = parts[1]
                        if not NICK.fullmatch(target):
                            send_event(conn, "Usage: /msg nick text", "error",
                                       channel=channel, nick=nick)
                            continue
                        message = safe_irc_text(parts[2])
                        irc_send(remote, f"PRIVMSG {target} :{message}")
                        send_event(conn, f"*to {target}* {message}", "private")
                    elif line.lower() == "/notice" or line.lower().startswith("/notice "):
                        parts = line.split(None, 2)
                        if len(parts) != 3 or not NICK.fullmatch(parts[1]):
                            send_event(conn, "Usage: /notice nick text", "error",
                                       channel=channel, nick=nick)
                            continue
                        message = safe_irc_text(parts[2])
                        irc_send(remote, f"NOTICE {parts[1]} :{message}")
                        send_event(conn, f"-to {parts[1]}- {message}", "notice")
                    elif line.lower() == "/me" or line.lower().startswith("/me "):
                        if len(line.split(None, 1)) != 2:
                            send_event(conn, "Usage: /me action", "error",
                                       channel=channel, nick=nick)
                            continue
                        action = safe_irc_text(line[4:])
                        if not channel:
                            send_event(conn, "Join a channel before using /me.", "error",
                                       channel="", nick=nick)
                            continue
                        irc_send(remote, f"PRIVMSG {channel} :\x01ACTION {action}\x01")
                        send_event(conn, f"* {nick} {action}", "action")
                    elif line.lower() == "/names" or line.lower().startswith("/names "):
                        target = line[6:].strip() or channel
                        if not target or not CHANNEL.fullmatch(target):
                            send_event(conn, "Usage: /names [#channel]", "error",
                                       channel=channel, nick=nick)
                            continue
                        if (self.targets[network].dedicated_channel and
                                target.casefold() != self.targets[network].channel.casefold()):
                            send_event(conn, "MarsHotel public IRC is limited to #MarsHotel.",
                                       "error", channel=channel, nick=nick)
                            continue
                        irc_send(remote, f"NAMES {target}")
                    elif line.lower() == "/who" or line.lower().startswith("/who "):
                        target = line[4:].strip() or channel
                        if not target or not CHANNEL.fullmatch(target):
                            send_event(conn, "Usage: /who [#channel]", "error",
                                       channel=channel, nick=nick)
                            continue
                        if (self.targets[network].dedicated_channel and
                                target.casefold() != self.targets[network].channel.casefold()):
                            send_event(conn, "MarsHotel public IRC is limited to #MarsHotel.",
                                       "error", channel=channel, nick=nick)
                            continue
                        irc_send(remote, f"WHO {target}")
                    elif line.lower() == "/whois" or line.lower().startswith("/whois "):
                        target = line[6:].strip()
                        if not NICK.fullmatch(target):
                            send_event(conn, "Usage: /whois nick", "error",
                                       channel=channel, nick=nick)
                            continue
                        irc_send(remote, f"WHOIS {target}")
                    elif line.lower() == "/channels" or line.lower().startswith("/channels "):
                        pattern = line[9:].strip()
                        if pattern and not LIST_FILTER.fullmatch(pattern):
                            send_event(conn, "Usage: /channels [filter]", "error",
                                       channel=channel, nick=nick)
                            continue
                        list_results = 0
                        if self.targets[network].dedicated_channel:
                            irc_send(remote, f"LIST {self.targets[network].channel}")
                        else:
                            irc_send(remote, "LIST" + (f" {pattern}" if pattern else ""))
                    elif line.lower() == "/topic" or line.lower().startswith("/topic "):
                        if not channel:
                            send_event(conn, "Join a channel before using /topic.", "error",
                                       channel="", nick=nick)
                            continue
                        topic = line[6:].strip()
                        if not topic:
                            irc_send(remote, f"TOPIC {channel}")
                        else:
                            topic = "" if topic == "-" else safe_irc_text(topic, 300)
                            irc_send(remote, f"TOPIC {channel} :{topic}")
                    elif line.lower() == "/away" or line.lower().startswith("/away "):
                        message = line[5:].strip() or "Away from the terminal"
                        irc_send(remote, f"AWAY :{safe_irc_text(message, 160)}")
                    elif line.lower() == "/back":
                        irc_send(remote, "AWAY")
                    elif line.lower() == "/motd":
                        irc_send(remote, "MOTD")
                    elif line.lower() == "/time":
                        irc_send(remote, "TIME")
                    elif line.lower() == "/version":
                        irc_send(remote, "VERSION")
                    elif line.lower() == "/invite" or line.lower().startswith("/invite "):
                        parts = line.split()
                        target_channel = parts[2] if len(parts) == 3 else channel
                        if (len(parts) not in (2, 3) or not NICK.fullmatch(parts[1])
                                or not target_channel or not CHANNEL.fullmatch(target_channel)):
                            send_event(conn, "Usage: /invite nick [#channel]", "error",
                                       channel=channel, nick=nick)
                            continue
                        if (self.targets[network].dedicated_channel and
                                target_channel.casefold() !=
                                self.targets[network].channel.casefold()):
                            send_event(conn, "MarsHotel public IRC is limited to #MarsHotel.",
                                       "error", channel=channel, nick=nick)
                            continue
                        irc_send(remote, f"INVITE {parts[1]} {target_channel}")
                    elif line.lower() == "/kick" or line.lower().startswith("/kick "):
                        parts = line.split(None, 2)
                        if not channel or len(parts) < 2 or not NICK.fullmatch(parts[1]):
                            send_event(conn, "Usage: /kick nick [reason]", "error",
                                       channel=channel, nick=nick)
                            continue
                        reason = safe_irc_text(parts[2], 160) if len(parts) == 3 else "Requested"
                        irc_send(remote, f"KICK {channel} {parts[1]} :{reason}")
                    elif line.lower() == "/mode" or line.lower().startswith("/mode "):
                        parts = line.split()
                        if not channel or len(parts) > 3 or (len(parts) >= 2 and
                                not MODE.fullmatch(parts[1])) or (len(parts) == 3 and
                                not NICK.fullmatch(parts[2])):
                            send_event(conn, "Usage: /mode [+-modes [nick]]", "error",
                                       channel=channel, nick=nick)
                            continue
                        irc_send(remote, "MODE " + channel +
                                 ((" " + " ".join(parts[1:])) if len(parts) > 1 else ""))
                    else:
                        send_event(conn, "Unknown command. Type /help for available commands.",
                                   "error", channel=channel, nick=nick)
                if remote in readable:
                    try:
                        part = remote.recv(4096)
                    except ssl.SSLWantReadError:
                        continue
                    if not part:
                        break
                    buffer += part
                    if len(buffer) > 16384:
                        raise RPCError("IRC receive buffer exceeded")
                    while b"\n" in buffer:
                        raw_line, buffer = buffer.split(b"\n", 1)
                        raw_line = raw_line.rstrip(b"\r")
                        if len(raw_line) > IRC_LINE_MAX:
                            raise RPCError("oversized IRC line")
                        try:
                            line = raw_line.decode("utf-8", "strict")
                        except UnicodeDecodeError:
                            continue
                        prefix, parts = parse_line(line)
                        if not parts:
                            continue
                        command = parts[0].upper()
                        sender = prefix.split("!", 1)[0]
                        if command == "PING" and len(parts) > 1:
                            irc_send(remote, f"PONG :{safe_irc_text(parts[-1], 200)}")
                        elif command == "001" and not join_sent:
                            irc_send(remote, f"JOIN {channel}")
                            join_sent = True
                        elif command in ("432", "433", "436") and not announced:
                            collision_count += 1
                            if collision_count > 4:
                                raise RPCError("IRC nickname collision limit exceeded")
                            suffix = "" if collision_count == 1 else str(collision_count)
                            nick = ("MH_" + base_nick[:25] + suffix)[:30]
                            irc_send(remote, f"NICK {nick}")
                        elif command == "PRIVMSG" and len(parts) >= 3:
                            message = parts[-1]
                            if message.startswith("\x01ACTION ") and message.endswith("\x01"):
                                action = display_irc_text(message[8:-1])
                                send_event(conn, f"* {display_irc_text(sender, 32)} {action}", "action")
                            elif not message.startswith("\x01"):
                                message = display_irc_text(message)
                                if not message:
                                    continue
                                kind = "private" if parts[1].lower() == nick.lower() else "message"
                                marker = "*" if kind == "private" else "<"
                                ending = "*" if kind == "private" else ">"
                                send_event(conn, f"{marker}{display_irc_text(sender, 32)}{ending} {message}", kind)
                        elif command == "NOTICE" and len(parts) >= 3:
                            message = parts[-1]
                            if not message.startswith("\x01"):
                                send_event(conn,
                                           f"-{display_irc_text(sender or parts[1], 32)}- "
                                           f"{display_irc_text(message)}", "notice",
                                           channel=channel, nick=nick)
                        elif command == "353" and len(parts) >= 2:
                            send_event(conn, f"Users on {channel}: " +
                                       display_irc_text(parts[-1], 350), "names",
                                       channel=channel, nick=nick)
                        elif command == "352" and len(parts) >= 8:
                            who_nick = display_irc_text(parts[6], 32)
                            send_event(conn, f"Who on {channel}: {who_nick}", "who",
                                       channel=channel, nick=nick)
                        elif command == "315":
                            send_event(conn, f"End of /who for {channel}.", "who_end",
                                       channel=channel, nick=nick)
                        elif command == "322" and len(parts) >= 5:
                            if list_results < LIST_RESULT_MAX:
                                list_results += 1
                                send_event(conn,
                                           f"{display_irc_text(parts[2], 32):32} "
                                           f"{display_irc_text(parts[3], 12):>4}  "
                                           f"{display_irc_text(parts[-1], 380)}", "list",
                                           channel=channel, nick=nick)
                        elif command == "323":
                            suffix = " (display capped)" if list_results >= LIST_RESULT_MAX else ""
                            send_event(conn, f"End of channel list: {list_results} shown{suffix}.",
                                       "list_end", channel=channel, nick=nick)
                        elif command in ("331", "332"):
                            topic_channel = display_irc_text(parts[2], 32) \
                                if len(parts) > 2 else channel
                            topic = "No topic is set." if command == "331" \
                                else display_irc_text(parts[-1], 400)
                            send_event(conn, f"Topic for {topic_channel}: {topic}", "topic",
                                       channel=channel, nick=nick)
                        elif command == "311" and len(parts) >= 7:
                            send_event(conn,
                                       f"WHOIS {display_irc_text(parts[2], 32)}: "
                                       f"{display_irc_text(parts[3], 32)}@"
                                       f"{display_irc_text(parts[4], 120)} "
                                       f"({display_irc_text(parts[-1], 220)})", "whois",
                                       channel=channel, nick=nick)
                        elif command == "312" and len(parts) >= 5:
                            send_event(conn, f"Server: {display_irc_text(parts[3], 120)} - "
                                       f"{display_irc_text(parts[-1], 240)}", "whois",
                                       channel=channel, nick=nick)
                        elif command == "313" and len(parts) >= 3:
                            send_event(conn,
                                       f"{display_irc_text(parts[2], 32)} is an IRC operator.",
                                       "whois", channel=channel, nick=nick)
                        elif command == "317" and len(parts) >= 4:
                            send_event(conn, f"Idle: {display_irc_text(parts[3], 20)} seconds.",
                                       "whois", channel=channel, nick=nick)
                        elif command == "319" and len(parts) >= 4:
                            send_event(conn, "Channels: " + display_irc_text(parts[-1], 350),
                                       "whois", channel=channel, nick=nick)
                        elif command == "318" and len(parts) >= 3:
                            send_event(conn,
                                       f"End of WHOIS for {display_irc_text(parts[2], 32)}.",
                                       "whois_end", channel=channel, nick=nick)
                        elif command in ("305", "306", "341", "351", "372", "375",
                                         "376", "391", "324"):
                            send_event(conn, display_irc_text(" ".join(parts[2:]), 440),
                                       "server", channel=channel, nick=nick)
                        elif command == "JOIN":
                            joined_channel = display_irc_text(parts[-1], 32)
                            if not CHANNEL.fullmatch(joined_channel):
                                continue
                            if sender.lower() == nick.lower():
                                previous_channel = channel
                                channel = joined_channel
                                pending_channel = ""
                                if (announced and previous_channel and
                                        previous_channel.lower() != joined_channel.lower()):
                                    irc_send(remote, f"PART {previous_channel} :Changing rooms")
                                if not announced:
                                    announced = True
                                    send_event(conn,
                                               (("Connected to MarsHotel public IRC as "
                                                 if network == "synchronet" else
                                                 "Connected to local IRC as ") +
                                                f"{nick} on {joined_channel}."),
                                               "connected", nick=nick, channel=joined_channel,
                                               network=network)
                                else:
                                    send_event(conn, f"Now talking in {joined_channel}.",
                                               "channel", nick=nick,
                                               channel=joined_channel)
                            else:
                                send_event(conn,
                                           f"{display_irc_text(sender, 32)} joined {joined_channel}",
                                           "join", channel=joined_channel, nick=nick)
                        elif command == "PART":
                            parted_channel = display_irc_text(parts[1], 32) \
                                if len(parts) > 1 else channel
                            if sender.lower() == nick.lower() and parted_channel.lower() == channel.lower():
                                channel = ""
                                send_event(conn, f"Left {parted_channel}. Use /join #channel to return.",
                                           "channel", channel="", nick=nick)
                            elif sender.lower() != nick.lower():
                                send_event(conn,
                                           f"{display_irc_text(sender, 32)} left {parted_channel}",
                                           "leave", channel=channel, nick=nick)
                        elif command == "KICK" and len(parts) >= 3:
                            kicked = display_irc_text(parts[2], 32)
                            kicked_channel = display_irc_text(parts[1], 32)
                            reason = display_irc_text(parts[-1], 240)
                            if kicked.lower() == nick.lower():
                                channel = ""
                            send_event(conn, f"{kicked} was kicked from {kicked_channel}: {reason}",
                                       "kick", channel=channel, nick=nick)
                        elif command == "TOPIC" and len(parts) >= 3:
                            send_event(conn,
                                       f"{display_irc_text(sender, 32)} changed the topic: "
                                       f"{display_irc_text(parts[-1], 380)}", "topic",
                                       channel=channel, nick=nick)
                        elif command == "MODE" and len(parts) >= 3:
                            send_event(conn, f"{display_irc_text(sender, 32)} set mode "
                                       f"{display_irc_text(' '.join(parts[2:]), 300)} on "
                                       f"{display_irc_text(parts[1], 32)}", "mode",
                                       channel=channel, nick=nick)
                        elif command == "INVITE" and len(parts) >= 3:
                            send_event(conn, f"{display_irc_text(sender, 32)} invited you to "
                                       f"{display_irc_text(parts[-1], 32)}", "invite",
                                       channel=channel, nick=nick)
                        elif command == "NICK" and sender.lower() != nick.lower():
                            send_event(conn, f"{display_irc_text(sender, 32)} is now known as "
                                       f"{display_irc_text(parts[-1], 32)}", "nick",
                                       channel=channel, nick=nick)
                        elif command == "QUIT" and sender.lower() != nick.lower():
                            send_event(conn, f"{display_irc_text(sender, 32)} left IRC",
                                       "leave", channel=channel, nick=nick)
                        elif command.isdigit() and int(command) >= 400:
                            send_event(conn, "IRC error: " +
                                       display_irc_text(" ".join(parts[1:]), 400), "error")
        except (OSError, ssl.SSLError, RPCError, ValueError) as exc:
            try:
                send_event(conn, f"IRC disconnected: {exc}", "error")
            except OSError:
                pass
        finally:
            if remote is not None:
                try:
                    remote.close()
                except OSError:
                    pass
            conn.close()

    def run(self) -> None:
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        listener.listen(32)
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=self.client_loop, args=(conn,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--cafile", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--public-host")
    parser.add_argument("--public-port", type=int, default=6697)
    parser.add_argument("--public-server-name")
    parser.add_argument("--public-channel", default="#MarsHotel")
    args = parser.parse_args()
    Bridge(args.socket, args.session_socket, args.host, args.port, args.server_name,
           args.cafile, args.password_file, args.public_host, args.public_port,
           args.public_server_name, args.public_channel).run()


if __name__ == "__main__":
    main()
