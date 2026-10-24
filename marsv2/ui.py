"""Terminal UI for the compartmentalized Mars Hotel Revival V2."""

from __future__ import annotations

import getpass
import json
import os
import re
import select
import shlex
import signal
import shutil
import socket
import sys
import termios
import textwrap
import time
import tty
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import PROTOCOL_VERSION
from .client import call, socket_path
from .door_client import DoorError, play as play_door
from .rpc import MAX_PACKET, RPCError, decode_packet, encode_packet
from .terminal import (
    EditorBuffer, KeyDecoder, KEY_BACKSPACE, KEY_CHAR, KEY_DELETE, KEY_DOWN,
    KEY_END, KEY_ENTER, KEY_EOF, KEY_ESCAPE, KEY_F2, KEY_F3, KEY_HOME,
    KEY_INTERRUPT, KEY_INVALID, KEY_LEFT, KEY_PAGE_DOWN, KEY_PAGE_UP,
    KEY_RESIZE, KEY_RIGHT, KEY_UP,
)
from .timezones import choose_timezone, current_time_lines


def clear() -> None:
    global _mini_visible
    _mini_visible = False
    print("\033[r\033[2J\033[H", end="", flush=True)


def pause() -> None:
    """Wait for Enter or the system-wide hidden Escape/[X] return action."""
    prompt = "\nPress Enter to continue: "
    while True:
        try:
            choice = key_input(prompt)
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("", "x"):
            return
        # Reject unrelated keys without appending another copy of the prompt.
        sys.stdout.write("\a\r\033[K")
        sys.stdout.flush()


def show_error(exc: Exception) -> None:
    print(f"\nUnable to complete request: {exc}")


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
REVERSE = "\033[7m"
IDLE_AFTER_SECONDS = 300
ACTIVITY_REPORT_INTERVAL = 2.0
MAIL_BODY_MAX = 8192
BOARD_BODY_MAX = 16384
# Leave useful room for new reply/forward text instead of filling the editor
# with imported history. The mail service remains the final size authority.
MAIL_HISTORY_MAX = 6000
_marsamp_view: dict[str, object] = {
    "query": "", "playlist": "", "playlist_name": "", "cursor": 0,
    "mode": "sequential", "track_id": 0, "browse": "playlists",
}


_mini_token = ""
_mini_visible = False
_mini_state: dict[str, object] = {}
_mini_poll_at = 0.0
_mini_started = 0.0
_mini_signature: object = None
_mini_geometry = (0, 0)
_mini_row = 1
_mini_layout = 3


def _mini_text(value: object) -> str:
    # Restrict metadata to one terminal cell per character, including CJK tags.
    return "".join("?" if unicodedata.east_asian_width(c) in ("W", "F") else c
                   for c in safe_text(value) if not unicodedata.combining(c))


def _mini_refresh() -> bool:
    global _mini_state, _mini_poll_at, _mini_started, _mini_signature
    now = time.monotonic()
    if now < _mini_poll_at:
        return bool(_mini_state)
    _mini_poll_at = now + 3.0
    try:
        state = call("music", "status", token=_mini_token, timeout=0.3)
    except (OSError, RPCError):
        _mini_poll_at = now + 30.0
        return bool(_mini_state)
    signature = (state.get("track_id"), state.get("track"))
    if signature != _mini_signature:
        _mini_started = now
        _mini_signature = signature
    _mini_state = state
    return True


def _mini_lines(columns: int, now: float | None = None, *, layout: int | None = None) -> list[str]:
    inner = min(38, columns - 4)
    track = _mini_state.get("track")
    if isinstance(track, dict):
        artist = _mini_text(track.get("artist", ""))
        title = _mini_text(track.get("title", "Untitled"))
        label = (artist + " - " if artist else "") + title
    else:
        label = "Select music in [R] MarsAmp"
    room = inner - 6
    if len(label) > room:
        elapsed = max(0.0, (time.monotonic() if now is None else now) - _mini_started - 1.5)
        loop = label + "   /   "
        offset = int(elapsed / 0.35) % len(loop)
        label = (loop + loop)[offset:offset + room]
    label = label[:room].ljust(room)
    playing = (_mini_state.get("action") == "play"
               and _mini_state.get("browser_active", True))
    controls = f"[B] Back  [P] {'Pause' if playing else 'Play'}  [N] Next"
    if len(controls) > inner:
        controls = "[B]   [P]   [N]"
    caption = " MARSAMP "
    lines = ["┌" + caption.center(inner, "─") + "┐",
             "│ ♪ " + label + " ♪ │",
             "└" + controls.center(inner, "─") + "┘"]
    selected_layout = _mini_state.get("compact_player_lines", 3) if layout is None else layout
    if selected_layout == 4:
        lines = lines[:2] + ["│" + controls.center(inner) + "│", "└" + "─" * inner + "┘"]
    padding = " " * max(0, (columns - inner - 2) // 2)
    return [padding + line for line in lines]


def _mini_heading(title: str = "", *, lobby: bool = False, row: int = 1) -> None:
    global _mini_visible, _mini_geometry, _mini_row, _mini_layout
    size = shutil.get_terminal_size((80, 24))
    if (not _mini_token or not lobby or width() < 34 or size.lines < 24
            or (width() < 60 and size.lines < 32) or not _mini_refresh()):
        return
    _mini_visible = True
    _mini_geometry = (width(), size.lines)
    _mini_row = row
    _mini_layout = 4 if _mini_state.get("compact_player_lines") == 4 else 3
    lines = _mini_lines(width(), layout=_mini_layout)
    for line in lines:
        print(styled(line, CYAN))
    if sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
        # Keep the player stationary even if a long post or a notice scrolls.
        first_content_row = row + len(lines)
        sys.stdout.write(f"\033[{first_content_row};{size.lines}r\033[{first_content_row};1H")
        sys.stdout.flush()


def _mini_tick() -> None:
    if not _mini_visible or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return
    size = shutil.get_terminal_size((80, 24))
    if (width(), size.lines) != _mini_geometry:
        # The old header's coordinates are unsafe after a resize. Redraw when
        # the normal screen loop runs again; never overwrite content below it.
        return
    _mini_refresh()
    sys.stdout.write("\0337")
    for row, line in enumerate(_mini_lines(width(), layout=_mini_layout), _mini_row):
        sys.stdout.write(f"\033[{row};1H" + styled(line, CYAN))
    sys.stdout.write("\0338")
    sys.stdout.flush()


def _mini_control(value: str) -> None:
    """Direct Lobby transport keys; other screen commands are unchanged."""
    global _mini_poll_at
    operation = {"b": "previous", "n": "advance", "p": "pause" if
                 _mini_state.get("action") == "play" and
                 _mini_state.get("browser_active", True) else "resume"}[value]
    try:
        _marsamp_transport(_mini_token, operation)
        _mini_poll_at = 0.0
    except (OSError, RPCError) as exc:
        show_error(exc)
        pause()


_builtin_input: Callable[[str], str] = input
_active_pager: Pager | None = None


class EditorHangup(Exception):
    """Raised only while composing so a dropped SSH PTY can save a draft."""


class InputCancelled(Exception):
    """Raised when a cancellable terminal field receives Esc or Ctrl-C."""


class Pager:
    """Session-wide authenticated paging channel.

    Socket events are consumed by the foreground terminal loop.  Nothing writes
    to the PTY from a background thread, which keeps page notices from corrupting
    an input line.
    """

    def __init__(self, token: str):
        self.token = token
        self.userid = ""
        self.last_activity_report = 0.0
        self.pending: dict[str, dict[str, object]] = {}
        self.available = True
        self.notification = ""
        self.notification_until = 0.0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.connect(socket_path("chat"))
        chat_send(self.sock, token, "join", {"mode": "pager"})
        # The ready event normally arrives first.  A simultaneous caller can
        # legitimately race it, so preserve any early page rather than losing
        # the page or failing login initialization.
        while True:
            event = self.receive()
            if event.get("kind") == "pager_ready":
                self.userid = str(event.get("userid", ""))
                self.available = bool(event.get("available", True))
                break

    def activity(self, mode: str | None = None) -> None:
        """Report real keyboard activity without turning polling into activity."""
        now = time.monotonic()
        if mode is None and now - self.last_activity_report < ACTIVITY_REPORT_INTERVAL:
            return
        try:
            payload = {"mode": mode} if mode else None
            call("session", "activity", payload, self.token)
            self.last_activity_report = now
        except (OSError, RPCError):
            # Presence metadata must never interrupt the user's foreground work.
            pass

    def send(self, op: str, data: dict[str, object] | None = None) -> None:
        chat_send(self.sock, self.token, op, data)

    def receive(self) -> dict[str, object]:
        raw = self.sock.recv(MAX_PACKET + 1)
        if not raw:
            raise EOFError("paging service disconnected")
        event = dict(decode_packet(raw)["data"])
        kind = event.get("kind")
        page_id = event.get("page_id")
        if kind == "page" and isinstance(page_id, str):
            self.pending[page_id] = event
        elif kind in ("page_closed", "page_cancelled") and isinstance(page_id, str):
            self.pending.pop(page_id, None)
        elif kind == "pager_status":
            self.available = bool(event.get("available", True))
        if kind == "monitor" and isinstance(event.get("online"), bool):
            who = "@" + safe_text(event.get("userid", "member"))
            label = "ONLINE" if event["online"] else "OFFLINE"
            action = "signed in" if event["online"] else "signed out"
            self.present_notification(f"{label}: {who} has {action}.")
        return event

    def present_notification(self, message: str) -> None:
        self.notification = safe_text(message)
        self.notification_until = time.monotonic() + 12.0
        sys.stdout.write("\a")
        self.draw_notification()

    def draw_notification(self) -> None:
        """Overlay a banner without moving the user's cursor or consuming input."""
        if not getattr(self, "notification", ""):
            return
        if time.monotonic() > getattr(self, "notification_until", 0.0):
            return
        if sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
            columns = max(1, width() - 1)
            line = clip(" " + self.notification, columns).ljust(columns)
            sys.stdout.write("\0337\033[1;1H" + styled(line, BOLD, REVERSE, YELLOW) + "\0338")
            sys.stdout.flush()

    def close(self) -> None:
        try:
            self.send("quit")
        except OSError:
            pass
        self.sock.close()

    def read_line(self, prompt: str, cancellable: bool = False) -> str:
        """Read an editable line while keeping terminal keys out of its text."""
        fd = sys.stdin.fileno()
        if not sys.stdin.isatty():
            return _builtin_input(prompt)
        old = termios.tcgetattr(fd)
        buffer = ""
        cursor = 0
        decoder = KeyDecoder()

        def redraw() -> None:
            sys.stdout.write("\r\033[K" + prompt + buffer)
            tail = len(buffer) - cursor
            if tail:
                sys.stdout.write(f"\033[{tail}D")
            sys.stdout.flush()

        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            tty.setraw(fd)
            while True:
                ready, _, _ = _notice_select([fd, self.sock], [], [])
                if self.sock in ready:
                    event = self.receive()
                    if event.get("kind") not in ("pager_ready", "monitor"):
                        notice = str(event.get("message", "Pager event"))
                        sys.stdout.write("\a\r\033[K" + safe_text(notice) + "\r\n")
                        redraw()
                if fd in ready:
                    key, value = decoder.read(fd)
                    if key == "pending":
                        continue
                    if key != KEY_EOF:
                        self.activity()
                    if key == KEY_EOF:
                        raise EOFError
                    if key == KEY_ENTER:
                        sys.stdout.write("\r\n")
                        sys.stdout.flush()
                        return buffer
                    if key == KEY_INTERRUPT:
                        if cancellable:
                            raise InputCancelled
                        raise KeyboardInterrupt
                    if key == KEY_ESCAPE and cancellable:
                        raise InputCancelled
                    if key == KEY_BACKSPACE and cursor:
                        buffer = buffer[:cursor - 1] + buffer[cursor:]
                        cursor -= 1
                        tail = buffer[cursor:]
                        sys.stdout.write("\b" + tail + " ")
                        sys.stdout.write("\b" * (len(tail) + 1))
                        sys.stdout.flush()
                    elif key == KEY_DELETE and cursor < len(buffer):
                        buffer = buffer[:cursor] + buffer[cursor + 1:]
                        tail = buffer[cursor:]
                        sys.stdout.write(tail + " ")
                        sys.stdout.write("\b" * (len(tail) + 1))
                        sys.stdout.flush()
                    elif key == KEY_LEFT:
                        if cursor:
                            cursor -= 1
                            sys.stdout.write("\033[D")
                            sys.stdout.flush()
                    elif key == KEY_RIGHT:
                        if cursor < len(buffer):
                            cursor += 1
                            sys.stdout.write("\033[C")
                            sys.stdout.flush()
                    elif key == KEY_HOME:
                        movement = cursor
                        cursor = 0
                        if movement:
                            sys.stdout.write(f"\033[{movement}D")
                            sys.stdout.flush()
                    elif key == KEY_END:
                        movement = len(buffer) - cursor
                        cursor = len(buffer)
                        if movement:
                            sys.stdout.write(f"\033[{movement}C")
                            sys.stdout.flush()
                    elif key == KEY_CHAR and len(buffer) + len(value) <= 16000:
                        buffer = buffer[:cursor] + value + buffer[cursor:]
                        cursor += len(value)
                        tail = buffer[cursor:]
                        sys.stdout.write(value + tail)
                        if tail:
                            sys.stdout.write(f"\033[{len(tail)}D")
                        sys.stdout.flush()
                    elif key in (KEY_INVALID, KEY_UP, KEY_DOWN, KEY_PAGE_UP,
                                 KEY_PAGE_DOWN, KEY_ESCAPE, KEY_F2, KEY_F3):
                        # Navigation and unknown escape sequences are commands,
                        # not persistable field content.
                        sys.stdout.write("\a")
                        sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def read_key(self, prompt: str, escape_command: str = "x") -> str:
        """Read one menu key immediately, including common terminal arrows."""
        fd = sys.stdin.fileno()
        if not sys.stdin.isatty():
            return command(_builtin_input(prompt))
        old = termios.tcgetattr(fd)
        decoder = KeyDecoder()
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            tty.setraw(fd)
            while True:
                ready, _, _ = _notice_select([fd, self.sock], [], [], 0.35 if _mini_visible else None)
                _mini_tick()
                if self.sock in ready:
                    event = self.receive()
                    if event.get("kind") not in ("pager_ready", "monitor"):
                        notice = str(event.get("message", "Pager event"))
                        sys.stdout.write("\a\r\033[K" + notice + "\r\n" + prompt)
                        sys.stdout.flush()
                if fd not in ready:
                    continue
                key, value = decoder.read(fd)
                if key == "pending":
                    continue
                if key != KEY_EOF:
                    self.activity()
                if key == KEY_EOF:
                    raise EOFError
                if key == KEY_ENTER:
                    return ""
                if key == KEY_INTERRUPT:
                    raise KeyboardInterrupt
                mapped = {
                    KEY_UP: "k", KEY_DOWN: "j", KEY_RIGHT: "]", KEY_LEFT: "[",
                    KEY_PAGE_UP: "[", KEY_PAGE_DOWN: "]", KEY_ESCAPE: escape_command,
                }.get(key)
                if mapped is not None:
                    return mapped
                if key == KEY_CHAR and value.isascii():
                    # A menu command is complete as soon as its letter arrives.
                    # Tolerate clients that still send "letter + Enter" by
                    # discarding the already-buffered line ending.  The short
                    # window is below perceptible menu latency and does not
                    # consume an Enter typed later at the destination screen.
                    trailing, _, _ = select.select([fd], [], [], 0.03)
                    if trailing:
                        termios.tcflush(fd, termios.TCIFLUSH)
                    return value.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _notice_select(readers, writers, exceptional, timeout=None):
    """Multiplex global alerts in every foreground terminal input loop."""
    pager = _active_pager
    added = pager is not None and pager.sock not in readers
    sources = list(readers) + ([pager.sock] if added else [])
    if pager is not None:
        pager.draw_notification()
    ready, writable, errors = select.select(sources, writers, exceptional, timeout)
    if added and pager.sock in ready:
        event = pager.receive()
        if event.get("kind") not in ("pager_ready", "monitor"):
            pager.present_notification("PAGER: " + safe_text(event.get("message", "event")))
        ready.remove(pager.sock)
    return ready, writable, errors


def input(prompt: str = "") -> str:
    """Page-aware replacement for the built-in line reader after login."""
    if _active_pager is not None:
        return _active_pager.read_line(prompt)
    return _builtin_input(prompt)


def key_input(prompt: str, escape_command: str = "x") -> str:
    """Read an immediate cursor/menu command when a PTY pager is active."""
    if _active_pager is not None:
        return _active_pager.read_key(prompt, escape_command)
    value = _builtin_input(prompt)
    if value == "\x1b":
        return escape_command
    return command(value)


def styled(value: str, *codes: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return value
    return "".join(codes) + value + RESET


def safe_text(value: object, multiline: bool = False) -> str:
    """Render untrusted stored text without emitting terminal controls."""
    rendered: list[str] = []
    for character in str(value).replace("\r\n", "\n").replace("\r", "\n"):
        code = ord(character)
        if character == "\n" and multiline:
            rendered.append(character)
        elif character == "\t" and multiline:
            rendered.append("    ")
        elif code < 32 or code == 127 or 0x80 <= code <= 0x9f:
            rendered.append("?")
        else:
            rendered.append(character)
    return "".join(rendered)


def terminal_hyperlink(label: object, url: object) -> str:
    """Render an OSC 8 link with an inert label and URL for capable terminals."""
    clean_label = safe_text(label)
    clean_url = safe_text(url)
    if sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
        return f"\033]8;;{clean_url}\033\\{clean_label}\033]8;;\033\\"
    return clean_label


def width() -> int:
    # SSH supplies the PTY dimensions and sends window-change notifications as
    # a phone rotates or its on-screen keyboard opens.  Honor narrow clients;
    # the old 60-column floor made a correctly reported 40-50 column phone
    # wrap every line as though it were a desktop terminal.
    return max(20, min(120, shutil.get_terminal_size((80, 24)).columns))


def clip(value: object, maximum: int) -> str:
    rendered = safe_text(value).replace("\n", " ")
    if len(rendered) <= maximum:
        return rendered
    return rendered[:max(1, maximum - 1)] + "~"


def stamp(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%b %d")
    except (TypeError, ValueError, OSError):
        return "-- ---"


def mail_datetime(value: object) -> str:
    """Return an unambiguous, stable date for quoted mail headers."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        return "date unavailable"


def _signed_body(body: str) -> str:
    """Append the account's bounded signature at the final UI boundary."""
    try:
        signature = str(call("auth", "profile").get("signature", "")).strip()
    except RPCError:
        signature = ""
    return body.rstrip() + ("\n\n-- \n" + signature if signature else "")


def _limited_mail_history(prefix: str, history: str,
                          maximum: int = MAIL_HISTORY_MAX) -> str:
    """Keep mail history bounded while retaining its identifying header."""
    marker = "\n[Earlier message history truncated]"
    clean_prefix = safe_text(prefix, True)
    clean_history = safe_text(history, True)
    if len(clean_prefix) >= maximum:
        return clean_prefix[:maximum]
    available = maximum - len(clean_prefix)
    if len(clean_history) <= available:
        return clean_prefix + clean_history
    available = max(0, available - len(marker))
    return clean_prefix + clean_history[:available].rstrip() + marker


def reply_subject(value: object) -> str:
    subject = safe_text(value).strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def forward_subject(value: object) -> str:
    subject = safe_text(value).strip()
    return subject if subject.lower().startswith(("fwd:", "fw:")) else f"Fwd: {subject}"


def reply_history(message: dict[str, object]) -> str:
    """Build the editable, recursively quoted history for a reply."""
    body = safe_text(message.get("body", ""), True)
    quoted = "\n".join("> " + line if line else ">" for line in body.split("\n"))
    header = ("\n\n"
              f"On {mail_datetime(message.get('created_at'))}, "
              f"{safe_text(message.get('sender_user', 'unknown'))} wrote:\n")
    return _limited_mail_history(header, quoted)


def forward_history(message: dict[str, object]) -> str:
    """Build a conventional forward block without discarding quoted history."""
    header = ("\n\n---------- Forwarded message ----------\n"
              f"From: {safe_text(message.get('sender_user', 'unknown'))}\n"
              f"To: {safe_text(message.get('recipient_user', 'unknown'))}\n"
              f"Date: {mail_datetime(message.get('created_at'))}\n"
              f"Subject: {safe_text(message.get('subject', ''))}\n\n")
    return _limited_mail_history(header, str(message.get("body", "")))


def heading(title: str, subtitle: str = "", right: str = "") -> None:
    clear()
    columns = min(width(), 78)
    line = "=" * columns
    left = safe_text(title)
    right_label = safe_text(right)
    if right_label and len(left) + len(right_label) + 1 <= columns:
        gap = " " * (columns - len(left) - len(right_label))
        print(styled(left, BOLD, CYAN) + gap + styled(right_label, BOLD, CYAN))
    else:
        print(styled(clip(left, columns), BOLD, CYAN))
        if right_label:
            print(styled(clip(right_label, columns), BOLD, CYAN))
    if subtitle:
        print(styled(textwrap.fill(safe_text(subtitle, True), width=columns,
                                   break_long_words=True,
                                   break_on_hyphens=False), DIM))
    print(styled(line, DIM))


def command(value: str) -> str:
    aliases = {
        "\x1b": "x", "\x1b[A": "k", "\x1b[B": "j",
        "\x1b[C": "]", "\x1b[D": "[",
    }
    return aliases.get(value, value.strip().lower())


def page_window(items: list[dict[str, object]], cursor: int) -> tuple[int, int]:
    page_size = max(6, min(15, shutil.get_terminal_size((80, 24)).lines - 12))
    start = (cursor // page_size) * page_size if items else 0
    return start, min(len(items), start + page_size)


def compose_post(token: str, board_id: int, board_name: str,
                 parent: dict[str, object] | None = None) -> None:
    default = ""
    if parent:
        current = str(parent["subject"])
        default = current if current.lower().startswith("re:") else f"Re: {current}"
    subject = _mail_field("Subject", default)
    if subject is None:
        return
    action, body = mail_editor(
        board_name, subject, editor_title="MESSAGE BOARDS - COMPOSE",
        destination_label="Board", submit_label="Post", allow_draft=False,
        maximum=BOARD_BODY_MAX)
    if action != "send":
        clear()
        return
    operation = "reply" if parent else "post"
    data: dict[str, object] = {"board_id": board_id, "subject": subject,
                              "body": _signed_body(body)}
    if parent:
        data["parent_id"] = int(parent["id"])
    result = call("board", operation, data, token)
    print(f"Message posted as #{result['id']}.")
    pause()


def edit_post(token: str, post: dict[str, object]) -> None:
    subject = _mail_field("Subject", str(post["subject"]))
    if subject is None:
        return
    action, body = mail_editor(
        str(post["board_name"]), subject, str(post["body"]),
        editor_title="MESSAGE BOARDS - EDIT POST", destination_label="Board",
        submit_label="Save", allow_draft=False, maximum=BOARD_BODY_MAX)
    if action != "send":
        clear()
        return
    call("board", "edit", {"id": int(post["id"]), "subject": subject, "body": body}, token)
    print("Message updated.")
    pause()


def board_matches(boards_list: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    """Filter boards by number, name, or description with useful ranking."""
    needle = query.casefold().strip()
    if not needle:
        return list(boards_list)

    def rank(board: dict[str, object]) -> tuple[int, int]:
        board_id = str(board.get("id", ""))
        name = str(board.get("name", "")).casefold()
        if board_id == needle:
            category = 0
        elif name.startswith(needle):
            category = 1
        elif needle in name:
            category = 2
        else:
            category = 3
        return category, int(board.get("id", 0))

    matches = [board for board in boards_list if (
        needle in str(board.get("id", "")).casefold()
        or needle in str(board.get("name", "")).casefold()
        or needle in str(board.get("description", "")).casefold())]
    return sorted(matches, key=rank)


def _draw_board_picker(boards_list: list[dict[str, object]], query: EditorBuffer,
                       cursor: int, current_id: int,
                       status: str) -> tuple[list[dict[str, object]], int]:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    rows = max(12, size.lines)
    visible_rows = max(3, rows - 8)
    matches = board_matches(boards_list, query.text)
    cursor = max(0, min(cursor, len(matches) - 1)) if matches else 0
    start = max(0, min(cursor - visible_rows // 2,
                       max(0, len(matches) - visible_rows)))

    sys.stdout.write("\033[?25l\033[2J\033[H")
    sys.stdout.write(styled(clip("MESSAGE BOARDS - CHOOSE BOARD", columns),
                            BOLD, CYAN) + "\r\n")
    sys.stdout.write(styled(clip(
        "Type a board number or filter by name and description.", columns), DIM) + "\r\n")
    sys.stdout.write(("Search: " + safe_text(query.text))[:columns] + "\r\n")
    sys.stdout.write(styled("-" * columns, DIM) + "\r\n")
    if matches:
        for position in range(start, min(len(matches), start + visible_rows)):
            board = matches[position]
            current = "  (current)" if int(board["id"]) == current_id else ""
            new = f"  {int(board.get('unread', 0))} new" if int(
                board.get("unread", 0)) else ""
            label = (f"#{int(board['id']):<3} {safe_text(board['name'])}"
                     f"  {int(board.get('posts', 0))} posts{new}{current}")
            line = clip(label, columns)
            sys.stdout.write((styled(line, REVERSE) if position == cursor else line) + "\r\n")
        for _ in range(visible_rows - min(visible_rows, len(matches) - start)):
            sys.stdout.write("\r\n")
    else:
        sys.stdout.write(styled(clip(
            "No matching boards. Backspace to change the search.", columns), DIM) + "\r\n")
        for _ in range(visible_rows - 1):
            sys.stdout.write("\r\n")
    sys.stdout.write(styled(clip(
        "Arrows/PgUp/PgDn move  Enter chooses  Esc cancels", columns), DIM) + "\r\n")
    detail = safe_text(matches[cursor].get("description", "")) if matches else ""
    summary = status or f"{len(matches)} of {len(boards_list)} shown  /  {detail}"
    sys.stdout.write(clip(summary, columns))
    sys.stdout.write(f"\033[3;{min(columns, 9 + query.cursor)}H\033[?25h")
    sys.stdout.flush()
    return matches, cursor


def choose_board(boards_list: list[dict[str, object]], current_id: int) -> int | None:
    """Immediate scrollable board picker with type-ahead filtering."""
    query = EditorBuffer("", 40)
    decoder = KeyDecoder()
    cursor = next((position for position, board in enumerate(boards_list)
                   if int(board["id"]) == current_id), 0)
    status = ""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    pager = _active_pager
    try:
        tty.setraw(fd)
        while True:
            matches, cursor = _draw_board_picker(
                boards_list, query, cursor, current_id, status)
            status = ""
            sources: list[object] = [fd]
            if pager is not None:
                sources.append(pager.sock)
            ready, _, _ = _notice_select(sources, [], [])
            if pager is not None and pager.sock in ready:
                event = pager.receive()
                if event.get("kind") not in ("pager_ready", "monitor"):
                    status = "Pager: " + safe_text(event.get("message", "event"))
                continue
            key, value = decoder.read(fd)
            if key == "pending" or key in ("paste_start", "paste_end", KEY_RESIZE):
                continue
            if pager is not None and key != KEY_EOF:
                pager.activity()
            if key in (KEY_EOF, KEY_INTERRUPT, KEY_ESCAPE):
                return None
            if key == KEY_ENTER:
                if matches:
                    return int(matches[cursor]["id"])
                status = "No board selected; change the search or press Esc to cancel."
            elif key == KEY_UP:
                cursor = max(0, cursor - 1)
            elif key == KEY_DOWN:
                cursor = min(len(matches) - 1, cursor + 1) if matches else 0
            elif key == KEY_PAGE_UP:
                distance = max(3, shutil.get_terminal_size((80, 24)).lines - 8)
                cursor = max(0, cursor - distance)
            elif key == KEY_PAGE_DOWN:
                distance = max(3, shutil.get_terminal_size((80, 24)).lines - 8)
                cursor = min(len(matches) - 1, cursor + distance) if matches else 0
            elif key == KEY_LEFT:
                query.left()
            elif key == KEY_RIGHT:
                query.right()
            elif key == KEY_HOME:
                query.cursor = 0
            elif key == KEY_END:
                query.cursor = len(query.text)
            elif key == KEY_BACKSPACE:
                query.backspace()
                cursor = 0
            elif key == KEY_DELETE:
                query.delete()
                cursor = 0
            elif key == KEY_CHAR:
                if query.insert(value):
                    cursor = 0
                else:
                    status = "Search is limited to 40 characters."
            elif key == KEY_INVALID:
                status = "Unsupported terminal key ignored."
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (termios.error, OSError):
            pass
        sys.stdout.write("\033[0m\033[?25h")
        sys.stdout.flush()


def read_posts(token: str, rows: list[dict[str, object]], cursor: int) -> int:
    while rows:
        cursor = max(0, min(cursor, len(rows) - 1))
        post = call("board", "read", {"id": int(rows[cursor]["id"])}, token)["post"]
        heading(str(post["subject"]), f"{post['board_name']}  /  {post['author_user']}  /  {stamp(post['created_at'])}")
        print(textwrap.fill(str(post["body"]), width=min(width(), 100), replace_whitespace=False))
        print()
        print(styled("[↓/Enter] next  [↑] previous  [R] reply  [E] edit  [D] delete", DIM))
        print(styled("[M] move  [K] protect/unprotect  [X] exit", DIM))
        try:
            choice = key_input("Read> ")
        except (EOFError, KeyboardInterrupt):
            return cursor
        if choice in ("q", "b", "x"):
            return cursor
        if choice in ("", "j", "]", "n"):
            cursor = min(len(rows) - 1, cursor + 1)
        elif choice in ("k", "[", "p"):
            cursor = max(0, cursor - 1)
        elif choice in ("r", "y"):
            compose_post(token, int(post["board_id"]), str(post["board_name"]), post)
        elif choice == "e":
            edit_post(token, post)
        elif choice == "d":
            if input("Delete this post? [y/N] ").strip().lower() == "y":
                call("board", "delete", {"id": int(post["id"])}, token)
                del rows[cursor]
                if not rows:
                    return 0
                cursor = min(cursor, len(rows) - 1)
        elif choice == "m":
            boards_list = call("board", "boards", token=token)["boards"]
            print("\n" + "  ".join(f"[{item['id']}] {item['name']}" for item in boards_list))
            try:
                destination = int(input("Move to board number (blank cancels): ").strip())
            except ValueError:
                continue
            call("board", "move", {"id": int(post["id"]),
                                     "board_id": destination}, token)
            del rows[cursor]
            if not rows:
                return 0
            cursor = min(cursor, len(rows) - 1)
        elif choice == "k":
            call("board", "protect", {"id": int(post["id"]),
                                        "protected": not bool(post.get("protected"))}, token)


def boards(token: str, unread_only: bool = False) -> None:
    selected = 1
    cursor = 0
    while True:
        try:
            listing = call("board", "boards", token=token)["boards"]
            if not listing:
                heading("MESSAGE BOARDS", "No boards are configured")
                pause()
                return
            if not any(int(board["id"]) == selected for board in listing):
                selected = int(listing[0]["id"])
            result = call("board", "list", {"board_id": selected}, token)
            rows = list(reversed(result["posts"]))
            board = next(item for item in listing if int(item["id"]) == selected)
            if unread_only:
                unread = [item for item in rows if not bool(item.get("is_read"))]
                if unread:
                    rows = unread
                else:
                    unread_only = False
            cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
            heading("MESSAGE BOARDS", f"{board['name']} - {board['description']}")
            board_tabs = []
            for tab_board in listing:
                unread_count = int(tab_board.get("unread", 0))
                label = f"[{tab_board['id']}] {tab_board['name']}"
                if unread_count:
                    label += f" ({unread_count} new)"
                if int(tab_board["id"]) == selected:
                    label = styled(label, BOLD, CYAN)
                board_tabs.append(label)
            print("  ".join(board_tabs))
            print()
            if rows:
                start, end = page_window(rows, cursor)
                print(styled(f"{'':2} {'NEW':3} {'DATE':6} {'FROM':12} SUBJECT", BOLD))
                for position in range(start, end):
                    post = rows[position]
                    new = "NEW" if not bool(post.get("is_read")) else ""
                    reply = " > " if post.get("parent_id") else "   "
                    line = (f"{position + 1:2}. {new:3} {stamp(post['created_at']):6} "
                            f"{clip(post['author_user'], 12):12} {reply}{clip(post['subject'], max(12, width() - 43))}")
                    print(styled(line, REVERSE) if position == cursor else line)
                print(styled(f"Page {start // max(1, end-start) + 1}  /  {len(rows)} message(s)", DIM))
            else:
                print(styled("No messages here yet. Be the first to post.", DIM))
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        print()
        print(styled("[↑/↓] move [Enter] open [S] switch board [P] post [R] reply", DIM))
        print(styled("[E] edit [V] visit/mark read [Z] zap/restore New scan [X] exit", DIM))
        try:
            choice = key_input(f"Board {selected}> ")
        except (EOFError, KeyboardInterrupt):
            return
        try:
            if choice in ("q", "x"):
                return
            if choice == "s":
                chosen = choose_board(listing, selected)
                if chosen is not None:
                    selected = chosen
                    cursor = 0
            elif choice == "j":
                cursor = min(len(rows) - 1, cursor + 1) if rows else 0
            elif choice == "k":
                cursor = max(0, cursor - 1)
            elif choice == "]":
                if rows:
                    cursor = min(len(rows) - 1, page_window(rows, cursor)[1])
            elif choice == "[":
                if rows:
                    start, end = page_window(rows, cursor)
                    cursor = max(0, start - max(1, end - start))
            elif choice == "l":
                pause()
            elif choice == "" and rows:
                cursor = read_posts(token, rows, cursor)
            elif choice == "p":
                compose_post(token, selected, str(board["name"]))
            elif choice in ("r", "y") and rows:
                parent = call("board", "read", {"id": int(rows[cursor]["id"])}, token)["post"]
                compose_post(token, selected, str(board["name"]), parent)
            elif choice == "e" and rows:
                post = call("board", "read", {"id": int(rows[cursor]["id"])}, token)["post"]
                edit_post(token, post)
            elif choice == "v":
                result = call("board", "mark_board_read", {"board_id": selected}, token)
                print(f"Visited {board['name']}; {result['marked']} message(s) marked read.")
                pause()
            elif choice == "z":
                enabled = not bool(board.get("scan_enabled", 1))
                call("board", "set_scan", {"board_id": selected, "enabled": enabled}, token)
                print(f"{board['name']} is " + ("in" if enabled else "out of") + " the New scan.")
                pause()
            elif choice.isdigit() and rows:
                position = int(choice) - 1
                if 0 <= position < len(rows):
                    cursor = position
                    cursor = read_posts(token, rows, cursor)
        except (RPCError, ValueError) as exc:
            show_error(exc)
            pause()


def scan_new_posts(token: str) -> None:
    """Read unread messages across every non-zapped accessible board."""
    try:
        rows = list(call("board", "scan_new", token=token)["posts"])
        if not rows:
            heading("NEW BOARD MESSAGES", "Every message in your selected boards is read")
            print("No unread board messages.")
            pause()
            return
        read_posts(token, rows, 0)
    except RPCError as exc:
        show_error(exc)
        pause()


def _draw_mail_editor(recipient: str, subject: str, buffer: EditorBuffer,
                      scroll: int, status: str, mode: str, editor_title: str,
                      destination_label: str, submit_label: str,
                      allow_draft: bool, previous_frame: list[str] | None = None
                      ) -> tuple[int, int, int, int]:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    screen_lines = max(12, size.lines)
    separator = "-" * columns
    if mode in ("confirm_send", "confirm_cancel"):
        help_text = "Press Y to confirm or N to return to the editor"
    else:
        help_text = (f"F2/Ctrl-S {submit_label}  Esc cancel  Arrows move  "
                     "Home/End  PgUp/PgDn"
                     + ("  F3 draft" if allow_draft else ""))
    help_lines = textwrap.wrap(help_text, width=columns, break_long_words=False,
                               break_on_hyphens=False) or [""]
    body_height = max(3, screen_lines - 7 - len(help_lines))
    wrap_width = max(20, columns - 1)
    visual, cursor_row, cursor_column = buffer.visual_position(wrap_width)
    if cursor_row < scroll:
        scroll = cursor_row
    elif cursor_row >= scroll + body_height:
        scroll = cursor_row - body_height + 1
    scroll = max(0, min(scroll, max(0, len(visual) - body_height)))

    lines = [styled(clip(editor_title, columns), BOLD, CYAN),
             styled(separator, DIM),
             clip(f"{destination_label + ':':8}" + safe_text(recipient), columns),
             clip("Subject: " + safe_text(subject), columns),
             styled(separator, DIM)]
    for index in range(scroll, scroll + body_height):
        if index < len(visual):
            row = visual[index]
            lines.append(safe_text(buffer.text[row.start:row.end]))
        else:
            lines.append("")
    lines.append(styled(separator, DIM))
    lines.extend(clip(line, columns) for line in help_lines)
    lines.append(clip(status, columns))
    previous = previous_frame if previous_frame is not None else []
    updates: list[str] = []
    if not previous:
        updates.append("\033[?25l\033[2J\033[H")
    for row_number, line in enumerate(lines, 1):
        if row_number > len(previous) or previous[row_number - 1] != line:
            if not updates:
                updates.append("\033[?25l")
            updates.append(f"\033[{row_number};1H\033[K" + line)
    if previous_frame is not None:
        previous_frame[:] = lines
    sys.stdout.write("".join(updates))
    terminal_row = 6 + cursor_row - scroll
    terminal_column = 1 + min(cursor_column, wrap_width)
    sys.stdout.write(f"\033[{terminal_row};{terminal_column}H\033[?25h")
    sys.stdout.flush()
    return scroll, wrap_width, body_height, (size.columns << 16) | size.lines


def mail_editor(recipient: str, subject: str, initial: str = "",
                cursor_at_start: bool = False, *,
                editor_title: str = "POST OFFICE - COMPOSE",
                destination_label: str = "To", submit_label: str = "Send",
                allow_draft: bool = True,
                maximum: int = MAIL_BODY_MAX) -> tuple[str, str]:
    """Run the bounded full-screen message editor on the session PTY."""
    fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        raise RuntimeError("full-screen mail editing requires a terminal")
    buffer = EditorBuffer(safe_text(initial, True), maximum)
    if cursor_at_start:
        buffer.cursor = 0
    decoder = KeyDecoder()
    old = termios.tcgetattr(fd)
    scroll = 0
    status = f"{len(buffer.text)} / {buffer.maximum} characters"
    mode = "edit"
    last_size = -1
    frame: list[str] = []
    rendered_state = None
    banner_was_active = False
    pager = _active_pager
    previous_hangup = signal.getsignal(signal.SIGHUP)

    def hangup(_signum: int, _frame: object) -> None:
        raise EditorHangup

    signal.signal(signal.SIGHUP, hangup)
    try:
        tty.setraw(fd)
        while True:
            size = shutil.get_terminal_size((80, 24))
            packed_size = (size.columns << 16) | size.lines
            if last_size != -1 and packed_size != last_size:
                frame.clear()
                status = "Terminal resized; message reflowed."
            banner_active = bool(pager and time.monotonic() <
                                 getattr(pager, "notification_until", 0.0))
            if banner_was_active and not banner_active and frame:
                frame[0] = ""  # Restore the title once a notification expires.
            banner_was_active = banner_active
            state = (buffer.text, buffer.cursor, status, mode, packed_size, banner_active)
            if state != rendered_state:
                scroll, wrap_width, body_height, last_size = _draw_mail_editor(
                    recipient, subject, buffer, scroll, status, mode, editor_title,
                    destination_label, submit_label, allow_draft, previous_frame=frame)
                rendered_state = state
            sources: list[object] = [fd]
            if pager is not None:
                sources.append(pager.sock)
            ready, _, _ = _notice_select(sources, [], [], 0.25)
            size = shutil.get_terminal_size((80, 24))
            packed_size = (size.columns << 16) | size.lines
            if packed_size != last_size:
                status = "Terminal resized; message reflowed."
                continue
            if pager is not None and pager.sock in ready:
                event = pager.receive()
                if event.get("kind") not in ("pager_ready", "monitor"):
                    status = "Pager: " + safe_text(event.get("message", "event"))
                continue
            if fd not in ready:
                continue
            key, value = decoder.read(fd)
            if key == "pending" or key in ("paste_start", "paste_end"):
                continue
            if pager is not None and key != KEY_EOF:
                pager.activity()
            if key == KEY_EOF:
                return "disconnect", buffer.text
            if key == KEY_INTERRUPT:
                return "cancel", buffer.text
            if mode in ("confirm_send", "confirm_cancel"):
                if key == KEY_CHAR and value.lower() == "y":
                    if mode == "confirm_send":
                        if not buffer.text.strip():
                            mode, status = "edit", "Message is blank; type text before sending."
                        else:
                            return "send", buffer.text
                    else:
                        return "cancel", buffer.text
                elif (key == KEY_CHAR and value.lower() == "n") or key == KEY_ESCAPE:
                    mode, status = "edit", "Editing resumed."
                else:
                    status = "Press Y to confirm or N to keep editing."
                continue

            if key == KEY_ESCAPE:
                return "cancel", buffer.text
            elif key == KEY_F2:
                mode = "confirm_send"
                status = f"{submit_label} this message now? [Y/N]"
                continue
            elif key == KEY_F3 and allow_draft:
                return "draft", buffer.text
            elif key == KEY_LEFT:
                buffer.left()
            elif key == KEY_RIGHT:
                buffer.right()
            elif key == KEY_UP:
                buffer.vertical(-1, wrap_width)
            elif key == KEY_DOWN:
                buffer.vertical(1, wrap_width)
            elif key == KEY_PAGE_UP:
                buffer.vertical(-body_height, wrap_width)
            elif key == KEY_PAGE_DOWN:
                buffer.vertical(body_height, wrap_width)
            elif key == KEY_HOME:
                buffer.home(wrap_width)
            elif key == KEY_END:
                buffer.end(wrap_width)
            elif key == KEY_BACKSPACE:
                buffer.backspace()
            elif key == KEY_DELETE:
                buffer.delete()
            elif key == KEY_ENTER:
                if not buffer.newline():
                    status = f"Message limit reached ({buffer.maximum} characters)."
                    continue
            elif key == KEY_CHAR:
                if not buffer.insert(value):
                    status = f"Message limit reached ({buffer.maximum} characters)."
                    continue
            elif key == KEY_INVALID:
                status = "Unsupported terminal key ignored; press Esc to cancel."
                continue
            status = f"{len(buffer.text)} / {buffer.maximum} characters"
    except EditorHangup:
        return "disconnect", buffer.text
    finally:
        signal.signal(signal.SIGHUP, previous_hangup)
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (termios.error, OSError):
            pass
        try:
            sys.stdout.write("\033[0m\033[?25h")
            sys.stdout.flush()
        except OSError:
            pass


def _mail_field(prompt: str, default: str = "", required: bool = True) -> str | None:
    while True:
        rendered_prompt = f"{prompt}{f' [{default}]' if default else ''}: "
        try:
            if _active_pager is not None:
                value = _active_pager.read_line(rendered_prompt, cancellable=True).strip()
            else:
                value = _builtin_input(rendered_prompt).strip()
        except (InputCancelled, KeyboardInterrupt):
            return None
        if value.lower() == "/cancel":
            return None
        value = value or default
        if value or not required:
            return value
        print(f"{prompt} is required. Enter a value, or /cancel to return.")


def recipient_matches(users: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    """Return safe directory matches with userid prefixes ranked first."""
    needle = query.casefold().strip()
    if not needle:
        return list(users)

    def rank(user: dict[str, object]) -> tuple[int, str]:
        name = str(user.get("userid", "")).casefold()
        display = str(user.get("display_name", "")).casefold()
        if name.startswith(needle):
            category = 0
        elif display.startswith(needle):
            category = 1
        elif needle in name:
            category = 2
        else:
            category = 3
        return category, name

    matched = [user for user in users
               if needle in str(user.get("userid", "")).casefold()
               or needle in str(user.get("display_name", "")).casefold()]
    return sorted(matched, key=rank)


def _recipient_directory(token: str) -> list[dict[str, object]]:
    users: list[dict[str, object]] = []
    offset = 0
    while len(users) < 10_000:
        result = call("mail", "recipients", {"offset": offset}, token)
        page = result.get("users")
        if not isinstance(page, list):
            raise RPCError("invalid recipient directory")
        for user in page:
            if (isinstance(user, dict) and isinstance(user.get("userid"), str)
                    and isinstance(user.get("display_name"), str)):
                users.append(user)
        if offset == 0:
            peers = result.get("peers", [])
            if isinstance(peers, list):
                for peer in peers:
                    if (isinstance(peer, dict) and isinstance(peer.get("bbs_id"), str)
                            and isinstance(peer.get("bbs_name"), str)):
                        users.append({
                            "userid": "@" + str(peer["bbs_id"]),
                            "display_name": (str(peer["bbs_name"])
                                             + " — type user@" + str(peer["bbs_id"])),
                            "peer_hint": True,
                        })
        following = result.get("next_offset")
        if following is None:
            return users
        if isinstance(following, bool) or not isinstance(following, int) or following <= offset:
            raise RPCError("invalid recipient directory")
        offset = following
    raise RPCError("recipient directory is too large")


def _monitor_directory() -> list[dict[str, object]]:
    users: list[dict[str, object]] = []
    offset = 0
    own = _active_pager.userid.casefold() if _active_pager is not None else ""
    while len(users) < 10_000:
        result = call("auth", "member_directory", {"offset": offset})
        for user in result.get("users", []):
            if (isinstance(user, dict) and isinstance(user.get("userid"), str)
                    and isinstance(user.get("display_name"), str)
                    and user["userid"].casefold() != own):
                users.append(user)
        following = result.get("next_offset")
        if following is None:
            return users
        if isinstance(following, bool) or not isinstance(following, int) or following <= offset:
            raise RPCError("invalid member directory")
        offset = following
    raise RPCError("member directory is too large")


def _draw_recipient_picker(users: list[dict[str, object]], query: EditorBuffer,
                           cursor: int, status: str, purpose: str = "mail") -> tuple[list[dict[str, object]], int]:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    rows = max(12, size.lines)
    visible_rows = max(3, rows - 8)
    matches = recipient_matches(users, query.text)
    cursor = max(0, min(cursor, len(matches) - 1)) if matches else 0
    start = max(0, min(cursor - visible_rows // 2,
                       max(0, len(matches) - visible_rows)))
    own_user = _active_pager.userid.casefold() if _active_pager is not None else ""

    sys.stdout.write("\033[?25l\033[2J\033[H")
    sys.stdout.write(styled(clip("MONITOR - CHOOSE MEMBER" if purpose == "monitor" else
                                "POST OFFICE - CHOOSE RECIPIENT", columns),
                            BOLD, CYAN) + "\r\n")
    sys.stdout.write(styled(clip("Choose a member for sign-in alerts throughout the BBS." if purpose == "monitor" else
                                "Type a local userid or user@linked-bbs.", columns),
                            DIM) + "\r\n")
    sys.stdout.write(("Search: " + safe_text(query.text))[:columns] + "\r\n")
    sys.stdout.write(styled("-" * columns, DIM) + "\r\n")
    if matches:
        for position in range(start, min(len(matches), start + visible_rows)):
            user = matches[position]
            name = safe_text(user["userid"])
            display = safe_text(user["display_name"])
            suffix = "  (you)" if name.casefold() == own_user else ""
            line = clip(f"{name:<20} {display}{suffix}", columns)
            sys.stdout.write((styled(line, REVERSE) if position == cursor else line) + "\r\n")
        for _ in range(visible_rows - min(visible_rows, len(matches) - start)):
            sys.stdout.write("\r\n")
    else:
        sys.stdout.write(styled(clip(
            "No matching users. Backspace to change the search.", columns), DIM)
            + "\r\n")
        for _ in range(visible_rows - 1):
            sys.stdout.write("\r\n")
    sys.stdout.write(styled(clip(
        "Arrows/PgUp/PgDn move  Enter chooses  Esc cancels", columns), DIM)
        + "\r\n")
    summary = status or f"{len(matches)} of {len(users)} user(s) shown"
    sys.stdout.write(clip(summary, columns))
    # The query is always edited in place at the end of its Search line.
    sys.stdout.write(f"\033[3;{min(columns, 9 + query.cursor)}H\033[?25h")
    sys.stdout.flush()
    return matches, cursor


def choose_mail_recipient(token: str, default: str = "", *, purpose: str = "mail") -> str | None:
    """Select a recipient using immediate filtering and terminal navigation."""
    users = _monitor_directory() if purpose == "monitor" else _recipient_directory(token)
    if not users:
        raise RPCError("No other members are available." if purpose == "monitor" else
                       "No mail recipients are available.")
    query = EditorBuffer(default, 45)
    decoder = KeyDecoder()
    cursor = 0
    status = ""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    pager = _active_pager
    try:
        tty.setraw(fd)
        while True:
            matches, cursor = _draw_recipient_picker(users, query, cursor, status, purpose)
            status = ""
            sources: list[object] = [fd]
            if pager is not None:
                sources.append(pager.sock)
            ready, _, _ = _notice_select(sources, [], [])
            if pager is not None and pager.sock in ready:
                event = pager.receive()
                if event.get("kind") not in ("pager_ready", "monitor"):
                    status = "Pager: " + safe_text(event.get("message", "event"))
                continue
            key, value = decoder.read(fd)
            if key == "pending" or key in ("paste_start", "paste_end"):
                continue
            if pager is not None and key != KEY_EOF:
                pager.activity()
            if key in (KEY_EOF, KEY_INTERRUPT, KEY_ESCAPE):
                return None
            if key == KEY_ENTER:
                if "@" in query.text and purpose == "mail":
                    try:
                        return str(call("mail", "resolve_recipient", {
                            "recipient": query.text}, token)["userid"])
                    except RPCError as exc:
                        status = safe_text(exc)
                elif matches and not matches[cursor].get("peer_hint"):
                    return str(matches[cursor]["userid"])
                elif matches:
                    status = "Type a recipient userid before the linked BBS address."
                else:
                    status = "No user selected; change the search or press Esc to cancel."
            elif key == KEY_UP:
                cursor = max(0, cursor - 1)
            elif key == KEY_DOWN:
                cursor = min(len(matches) - 1, cursor + 1) if matches else 0
            elif key == KEY_PAGE_UP:
                cursor = max(0, cursor - max(3, shutil.get_terminal_size((80, 24)).lines - 8))
            elif key == KEY_PAGE_DOWN:
                cursor = min(len(matches) - 1,
                             cursor + max(3, shutil.get_terminal_size((80, 24)).lines - 8)) \
                    if matches else 0
            elif key == KEY_LEFT:
                query.left()
            elif key == KEY_RIGHT:
                query.right()
            elif key == KEY_HOME:
                query.cursor = 0
            elif key == KEY_END:
                query.cursor = len(query.text)
            elif key == KEY_BACKSPACE:
                query.backspace()
                cursor = 0
            elif key == KEY_DELETE:
                query.delete()
                cursor = 0
            elif key == KEY_CHAR:
                if query.insert(value):
                    cursor = 0
                else:
                    status = "Search is limited to 45 characters."
            elif key == KEY_INVALID:
                status = "Unsupported terminal key ignored."
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (termios.error, OSError):
            pass
        sys.stdout.write("\033[0m\033[?25h")
        sys.stdout.flush()


def send_mail(token: str, recipient: str = "", subject: str = "", body: str = "",
              draft_id: int | None = None, cursor_at_start: bool = False,
              recipient_locked: bool = False) -> bool:
    if recipient_locked:
        if not recipient:
            raise ValueError("reply recipient is missing")
        chosen = recipient
    else:
        chosen = choose_mail_recipient(token, recipient)
        if chosen is None:
            return False
    # Resolve again at the authority boundary so a directory race cannot send
    # to a removed/disabled account.
    chosen = str(call("mail", "resolve_recipient", {"recipient": chosen}, token)["userid"])
    # The picker owns the full screen. Erase its search and directory before
    # presenting the Subject field so the two states never overlap.
    clear()
    chosen_subject = _mail_field("Subject", subject)
    if chosen_subject is None:
        return False
    while True:
        action, body = mail_editor(chosen, chosen_subject, body, cursor_at_start)
        # A delivery error may reopen the existing composition. Only the first
        # entry begins above imported history; subsequent entries resume at end.
        cursor_at_start = False
        if action != "disconnect":
            clear()
        if action == "cancel":
            print("Message cancelled; nothing was sent.")
            pause()
            return False
        if action in ("draft", "disconnect"):
            if chosen or chosen_subject or body:
                payload: dict[str, object] = {
                    "recipient": chosen, "subject": chosen_subject, "body": body,
                }
                if draft_id is not None:
                    payload["id"] = draft_id
                result = call("mail", "save_draft", payload, token)
                draft_id = int(result["id"])
                if action == "draft":
                    print(f"Draft #{draft_id} saved.")
                    pause()
            return False
        try:
            payload = {"recipient": chosen, "subject": chosen_subject,
                       "body": _signed_body(body)}
            if draft_id is not None:
                payload["draft_id"] = draft_id
            result = call("mail", "send", payload, token)
        except RPCError as exc:
            print(f"Delivery failed: {exc}")
            if input("Return to the editor? [Y/n] ").strip().lower() not in ("n", "no"):
                continue
            return False
        if int(result.get("queued", 0)):
            print(f"Mail queued securely for {chosen} as message #{result['id']}.")
        else:
            print(f"Mail delivered to {chosen} as message #{result['id']}.")
        pause()
        return True


def send_group_mail(token: str, initial: list[str] | None = None,
                    subject: str = "", body: str = "") -> bool:
    """Compose one transactionally validated delivery to up to 16 members."""
    recipients = list(dict.fromkeys(initial or []))
    while initial is None and len(recipients) < 16:
        heading("GROUP MAIL", "Choose members one at a time; Escape finishes selection",
                f"{len(recipients)}/16")
        if recipients:
            print("To: " + ", ".join(recipients) + "\n")
        chosen = choose_mail_recipient(token)
        if chosen is None:
            break
        if chosen.casefold() not in {value.casefold() for value in recipients}:
            recipients.append(chosen)
    if not recipients:
        return False
    clear()
    chosen_subject = _mail_field("Subject", subject)
    if chosen_subject is None:
        return False
    action, composed = mail_editor(
        ", ".join(recipients), chosen_subject, body, bool(body),
        editor_title="POST OFFICE - GROUP MAIL", destination_label="To",
        submit_label="Send", allow_draft=False)
    if action != "send":
        return False
    result = call("mail", "send", {"recipients": recipients,
                                     "subject": chosen_subject,
                                     "body": _signed_body(composed)}, token)
    delivered = int(result.get("delivered", 0))
    queued = int(result.get("queued", 0))
    print(f"Group mail: {delivered} local delivery/deliveries, "
          f"{queued} remote delivery/deliveries queued.")
    pause()
    return True


def _wrapped_body(value: object, columns: int) -> list[str]:
    result: list[str] = []
    for logical in safe_text(value, True).split("\n"):
        result.extend(textwrap.wrap(logical, width=max(20, columns),
                                    replace_whitespace=False, drop_whitespace=False)
                      or [""])
    return result


def _draw_mail_message(message: dict[str, object], folder: str, offset: int) -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    help_text = ("[↑/↓] scroll  [PgUp/PgDn] page  [N/P] message  "
                 "[R] reply  [A] reply all  [F] forward  [D] delete  [X] folder")
    help_lines = textwrap.wrap(help_text, width=columns, break_long_words=False,
                               break_on_hyphens=False)
    # Five header rows, one body separator, the help rows, and the prompt must
    # all fit inside the negotiated terminal height. The previous fixed
    # subtraction was one row short even before its help text wrapped.
    delivery_line = int(folder == "sent"
                        and message.get("exchange_state") not in (None, "delivered"))
    body_height = max(1, max(12, size.lines) - 7 - len(help_lines) - delivery_line)
    lines = _wrapped_body(message["body"], columns)
    offset = max(0, min(offset, max(0, len(lines) - body_height)))
    other = message["recipient_user"] if folder == "sent" else message["sender_user"]
    direction = "To" if folder == "sent" else "From"
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.write(styled(clip("POST OFFICE - " + folder.upper(), columns),
                            BOLD, CYAN) + "\r\n")
    sys.stdout.write(styled("=" * columns, DIM) + "\r\n")
    sys.stdout.write(clip(f"{direction}: {other}  /  {stamp(message['created_at'])}", columns) + "\r\n")
    sys.stdout.write(clip("Subject: " + safe_text(message["subject"]), columns) + "\r\n")
    if folder == "sent" and message.get("exchange_state") not in (None, "delivered"):
        status = str(message.get("exchange_state", "")).upper()
        sys.stdout.write(clip("Remote delivery: " + status, columns) + "\r\n")
    sys.stdout.write(styled("-" * columns, DIM) + "\r\n")
    for index in range(offset, offset + body_height):
        sys.stdout.write((lines[index] if index < len(lines) else "") + "\r\n")
    sys.stdout.write(styled("-" * columns, DIM) + "\r\n")
    for help_line in help_lines:
        sys.stdout.write(help_line + "\r\n")
    if columns < 60:
        prompt = (f"Lines {offset + 1}-{min(len(lines), offset + body_height)}"
                  f"/{len(lines)}  Mail> ")
    else:
        prompt = (f"Line {offset + 1}-{min(len(lines), offset + body_height)} "
                  f"of {len(lines)}  Mail read> ")
    sys.stdout.write(clip(prompt, columns))
    sys.stdout.flush()
    return offset, body_height


def read_mail(token: str, rows: list[dict[str, object]], cursor: int,
              folder: str = "inbox") -> tuple[int, bool]:
    changed = False
    offset = 0
    while rows:
        cursor = max(0, min(cursor, len(rows) - 1))
        message = call("mail", "read", {"id": int(rows[cursor]["id"]), "folder": folder}, token)["message"]
        changed = True
        offset, page_lines = _draw_mail_message(message, folder, offset)
        try:
            choice = key_input("")
        except (EOFError, KeyboardInterrupt):
            return cursor, changed
        body_lines = _wrapped_body(message["body"], width())
        maximum = max(0, len(body_lines) - page_lines)
        if choice in ("q", "b", "x"):
            return cursor, changed
        if choice == "j":
            offset = min(maximum, offset + 1)
        elif choice == "k":
            offset = max(0, offset - 1)
        elif choice == "]":
            offset = min(maximum, offset + page_lines)
        elif choice == "[":
            offset = max(0, offset - page_lines)
        elif choice in ("", "n"):
            cursor = min(len(rows) - 1, cursor + 1)
            offset = 0
        elif choice == "p":
            cursor = max(0, cursor - 1)
            offset = 0
        elif choice == "r":
            target = str(message["recipient_user"] if folder == "sent" else message["sender_user"])
            send_mail(token, target, reply_subject(message["subject"]),
                      reply_history(message), cursor_at_start=True,
                      recipient_locked=True)
        elif choice == "a":
            try:
                listed = json.loads(str(message.get("recipient_list") or "[]"))
            except (TypeError, ValueError):
                listed = []
            recipients = ([str(message["sender_user"])] if folder == "inbox" else [])
            recipients.extend(str(value) for value in listed if isinstance(value, str))
            self_name = str(message["recipient_user"] if folder == "inbox" else
                            message["sender_user"]).casefold()
            recipients = list(dict.fromkeys(
                value for value in recipients if value.casefold() != self_name))
            if len(recipients) <= 1:
                target = recipients[0] if recipients else str(message["sender_user"])
                send_mail(token, target, reply_subject(message["subject"]),
                          reply_history(message), cursor_at_start=True,
                          recipient_locked=True)
            else:
                send_group_mail(token, recipients, reply_subject(message["subject"]),
                                reply_history(message))
        elif choice == "f":
            send_mail(token, subject=forward_subject(message["subject"]),
                      body=forward_history(message), cursor_at_start=True)
        elif choice == "d":
            if input("Delete this message from this folder? [y/N] ").strip().lower() == "y":
                call("mail", "delete", {"id": int(message["id"]), "folder": folder}, token)
                del rows[cursor]
                changed = True
                if not rows:
                    return 0, changed
                cursor = min(cursor, len(rows) - 1)
                offset = 0


def _resume_draft(token: str, draft_id: int) -> None:
    draft = call("mail", "read_draft", {"id": draft_id}, token)["draft"]
    send_mail(token, str(draft["recipient_user"]), str(draft["subject"]),
              str(draft["body"]), int(draft["id"]))


def mail(token: str) -> None:
    folder = "inbox"
    cursors = {"inbox": 0, "sent": 0, "drafts": 0}
    while True:
        try:
            folders = {
                name: list(call("mail", "list", {"folder": name}, token)["messages"])
                for name in ("inbox", "sent", "drafts")
            }
            listing = folders[folder]
            cursor = max(0, min(cursors[folder], len(listing) - 1)) if listing else 0
            cursors[folder] = cursor
            unread = sum(1 for item in folders["inbox"] if not item["is_read"])
            heading("POST OFFICE", f"Inbox {len(folders['inbox'])} ({unread} unread)  |  Sent {len(folders['sent'])}  |  Drafts {len(folders['drafts'])}")
            print(styled(f"[I] INBOX   [O] SENT   [V] DRAFTS     Current: {folder.upper()}", BOLD, CYAN))
            print()
            if listing:
                start, end = page_window(listing, cursor)
                correspondent = "TO" if folder in ("sent", "drafts") else "FROM"
                print(styled(f"{'':2} {'NEW':3} {'DATE':6} {correspondent:12} SUBJECT", BOLD))
                for position in range(start, end):
                    message = listing[position]
                    new = "NEW" if folder == "inbox" and not message["is_read"] else ""
                    date = message.get("updated_at", message.get("created_at", 0))
                    person = message.get("recipient_user", "") if folder != "inbox" else message.get("sender_user", "")
                    line = (f"{position + 1:2}. {new:3} {stamp(date):6} "
                            f"{clip(person or '(unset)', 12):12} {clip(message['subject'] or '(no subject)', max(12, width() - 39))}")
                    print(styled(line, REVERSE) if position == cursor else line)
            else:
                print(styled(f"Your {folder} folder is empty.", DIM))
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        print()
        controls = ("[↑/↓] move  [Enter/R] open  [C] compose  [G] group mail  "
                    "[D] delete  [I/O/V] folders  [X/Esc] exit")
        for line in textwrap.wrap(controls, width=max(20, width()),
                                  break_long_words=False, break_on_hyphens=False):
            print(styled(line, DIM))
        try:
            choice = key_input("Mail> ")
            if choice in ("q", "x"):
                return
            if choice in ("i", "o", "v"):
                folder = {"i": "inbox", "o": "sent", "v": "drafts"}[choice]
            elif choice == "j":
                cursors[folder] = min(len(listing) - 1, cursor + 1) if listing else 0
            elif choice == "k":
                cursors[folder] = max(0, cursor - 1)
            elif choice == "]" and listing:
                cursors[folder] = min(len(listing) - 1, page_window(listing, cursor)[1])
            elif choice == "[" and listing:
                start, end = page_window(listing, cursor)
                cursors[folder] = max(0, start - max(1, end - start))
            elif choice in ("", "r") and listing:
                if folder == "drafts":
                    _resume_draft(token, int(listing[cursor]["id"]))
                else:
                    cursors[folder], _ = read_mail(token, list(listing), cursor, folder)
            elif choice == "c":
                send_mail(token)
            elif choice == "g":
                send_group_mail(token)
            elif choice == "d" and listing:
                label = "draft" if folder == "drafts" else "message"
                if input(f"Delete selected {label}? [y/N] ").strip().lower() == "y":
                    operation = "delete_draft" if folder == "drafts" else "delete"
                    payload: dict[str, object] = {"id": int(listing[cursor]["id"])}
                    if folder != "drafts":
                        payload["folder"] = folder
                    call("mail", operation, payload, token)
        except KeyboardInterrupt:
            return
        except (RPCError, ValueError, EOFError) as exc:
            show_error(exc)
            pause()


def _move_cursor(choice: str, cursor: int,
                 rows: list[dict[str, object]]) -> int:
    """Apply the common immediate-key list navigation contract."""
    if not rows:
        return 0
    if choice == "j":
        return min(len(rows) - 1, cursor + 1)
    if choice == "k":
        return max(0, cursor - 1)
    if choice == "]":
        return min(len(rows) - 1, page_window(rows, cursor)[1])
    if choice == "[":
        start, end = page_window(rows, cursor)
        return max(0, start - max(1, end - start))
    return cursor


def _library_safe_text(value: object) -> str:
    """Keep layout characters while making active terminal bytes visible."""
    rendered: list[str] = []
    for character in str(value).replace("\r\n", "\n").replace("\r", "\n"):
        code = ord(character)
        if character in ("\n", "\t"):
            rendered.append(character)
        elif code < 32 or code == 127 or 0x80 <= code <= 0x9f:
            rendered.append("?")
        else:
            rendered.append(character)
    return "".join(rendered)


def _text_viewer(title: str, subtitle: str, content: object,
                 preserve_columns: bool = False) -> None:
    """Page safe stored text without ever emitting stored terminal controls."""
    columns = max(20, min(width(), 100))
    logical_lines = [line.expandtabs(8) for line in _library_safe_text(content).split("\n")]
    rendered: list[str] = []
    if preserve_columns:
        rendered = logical_lines
    else:
        for logical in logical_lines:
            rendered.extend(textwrap.wrap(logical, width=columns,
                                          replace_whitespace=False,
                                          drop_whitespace=False) or [""])
    offset = 0
    horizontal = 0
    while True:
        page_size = max(4, shutil.get_terminal_size((80, 24)).lines - 7)
        offset = max(0, min(offset, max(0, len(rendered) - page_size)))
        heading(title, subtitle)
        for line in rendered[offset:offset + page_size]:
            print(line[horizontal:horizontal + columns] if preserve_columns else line)
        controls = "[↑/↓] scroll  [PgUp/PgDn] page"
        if preserve_columns:
            controls += f"  [H/L] horizontal ({horizontal})"
        print(styled(
            f"Lines {offset + 1 if rendered else 0}-{min(len(rendered), offset + page_size)} "
            f"of {len(rendered)}  {controls}  [X] exit", DIM))
        choice = key_input("View> ")
        if choice in ("e", "q", "x", ""):
            return
        if choice == "j":
            offset = min(max(0, len(rendered) - page_size), offset + 1)
        elif choice == "k":
            offset = max(0, offset - 1)
        elif choice == "]":
            offset = min(max(0, len(rendered) - page_size), offset + page_size)
        elif choice == "[":
            offset = max(0, offset - page_size)
        elif preserve_columns and choice == "h":
            horizontal = max(0, horizontal - max(8, columns // 2))
        elif preserve_columns and choice == "l":
            longest = max((len(line) for line in rendered), default=0)
            horizontal = min(max(0, longest - columns),
                             horizontal + max(8, columns // 2))


def _view_library_file(token: str, file_id: int) -> None:
    item = call("file", "info", {"id": file_id}, token)["file"]
    content_parts: list[str] = []
    offset = 0
    expected = int(item["preview_length"])
    while offset < expected:
        response = call("file", "read_chunk", {
            "id": file_id, "offset": offset, "limit": 12_000}, token)
        chunk = str(response["chunk"])
        if not chunk and not response["eof"]:
            raise RPCError("file service returned an empty partial response")
        content_parts.append(chunk)
        offset = int(response["next_offset"])
        if response["eof"]:
            break
    if offset != expected:
        raise RPCError("file changed while it was being read")
    details = [str(item["area"]), f"{int(item['original_size'])} bytes"]
    if item.get("original_date"):
        details.append(str(item["original_date"]))
    policy = str(item.get("rendering_policy", "wrapped"))
    if policy == "fixed" and int(item.get("max_line_columns", 0)) > width():
        details.append(f"fixed width {int(item['max_line_columns'])} cols")
    _text_viewer(str(item["name"]), "  /  ".join(details),
                 "".join(content_parts), preserve_columns=policy == "fixed")


def _export_library_file(token: str, file_id: int) -> None:
    """Emit a bounded, inert Base64 transfer stream with an integrity digest."""
    info = call("file", "export_info", {"id": file_id}, token)
    heading("TERMINAL FILE EXPORT", str(info["name"]))
    print(f"Size: {int(info['size'])} bytes")
    print(f"SHA-256: {info['sha256']}")
    print("This compatibility transfer is Base64 text. Capture only the data")
    print("between BEGIN/END, decode it locally, then verify SHA-256.")
    if input("Type EXPORT to begin (anything else cancels): ").strip() != "EXPORT":
        return
    print(f"-----BEGIN MARS FILE {safe_text(info['name'])}-----")
    offset = 0
    carry = ""
    while True:
        response = call("file", "export_chunk", {"id": file_id, "offset": offset}, token)
        encoded = carry + str(response["data"])
        while len(encoded) >= 76:
            print(encoded[:76])
            encoded = encoded[76:]
        carry = encoded
        offset = int(response["next_offset"])
        if response["eof"]:
            break
    if carry:
        print(carry)
    print(f"-----END MARS FILE {safe_text(info['name'])}-----")
    print(f"SHA-256: {info['sha256']}")
    pause()


def _download_library_file(token: str, file_id: int) -> None:
    """Issue a one-use ticket for a byte-exact download over forced SSH."""
    info = call("file", "issue_download", {"id": file_id}, token)
    host = os.environ.get("MARS_V2_DOWNLOAD_HOST", "mars.ee.msstate.edu.pl")
    port_text = os.environ.get("MARS_V2_DOWNLOAD_PORT", "2223")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        host = "mars.ee.msstate.edu.pl"
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        port_text = "2223"
    name = str(info["name"])
    local_name = safe_text(name).replace("/", "_").replace("\\", "_") or "mars-file"
    command = (f"ssh -p {port_text} bbs@{host} download {info['ticket']} > "
               f"{shlex.quote(local_name)}")
    heading("SSH FILE DOWNLOAD", name)
    print(f"Size: {int(info['size'])} bytes")
    print(f"SHA-256: {info['sha256']}")
    print()
    print("Run this command on your own computer within five minutes:")
    print()
    print(command)
    print()
    print("The ticket transfers this file once, then expires. It does not grant")
    print("shell, SFTP, file-listing, or access to any other Mars data.")
    pause()


def _library_area(token: str, area: str) -> None:
    cursor = 0
    while True:
        listing = list(call("file", "list", {"area": area}, token)["files"])
        cursor = max(0, min(cursor, len(listing) - 1)) if listing else 0
        heading(area, "Read-only text library")
        if listing:
            start, end = page_window(listing, cursor)
            print(styled(f"{'':2} {'FILE':32} {'BYTES':>7}  DESCRIPTION", BOLD))
            for position in range(start, end):
                item = listing[position]
                line = (f"{position + 1:2}. {clip(item['name'], 32):32} "
                        f"{int(item['size']):7}  "
                        f"{clip(item.get('description', ''), max(10, width() - 48))}")
                print(styled(line, REVERSE) if position == cursor else line)
        else:
            print(styled("This area has no published files.", DIM))
        print()
        print(styled("[↑/↓] move  [Enter] view  [D] download  [B] Base64  [X] exit", DIM))
        choice = key_input("Files> ")
        if choice in ("e", "q", "x"):
            return
        if choice == "" and listing:
            _view_library_file(token, int(listing[cursor]["id"]))
        elif choice == "d" and listing:
            _download_library_file(token, int(listing[cursor]["id"]))
        elif choice == "b" and listing:
            _export_library_file(token, int(listing[cursor]["id"]))
        else:
            cursor = _move_cursor(choice, cursor, listing)


CATALOG_STATUS = {
    "historical_listing": "LISTED",
    "recovered_verified": "RECOVERED",
    "possible_match": "REVIEW",
    "unavailable": "NOT FOUND",
    "quarantined": "QUARANTINE",
    "redistribution_not_approved": "RESTRICTED",
}


def _catalog_detail(token: str, catalog_id: int) -> None:
    item = call("file", "catalog_read", {"id": catalog_id}, token)["file"]
    while True:
        heading(str(item["filename"]), f"Historical catalog / {item['category']}")
        print(f"Listing status : {CATALOG_STATUS.get(str(item['recovery_status']), 'UNKNOWN')}")
        print(f"Original size  : {int(item['listed_size'])} bytes")
        print(f"Original date  : {safe_text(item['listed_date'])}")
        print(f"Listing line   : {int(item['listing_line'])}")
        if item.get("sha256"):
            print(f"SHA-256        : {safe_text(item['sha256'])}")
        if item.get("license_status") and item["license_status"] != "unknown":
            print(f"Rights review  : {safe_text(item['license_status'])}")
        if item.get("source_url"):
            print(f"Provenance     : {safe_text(item['source_url'])}")
        if item.get("notes"):
            print()
            print(textwrap.fill(safe_text(item["notes"]), width=min(width(), 100)))
        print()
        if item.get("published_file_id") is not None:
            print(styled("[V] view recovered file   [X] exit", DIM))
        else:
            print(styled("Catalog record only; no downloadable payload.  [X] exit", DIM))
        choice = key_input("Catalog> ")
        if choice in ("e", "q", "x", ""):
            return
        if choice == "v" and item.get("published_file_id") is not None:
            _view_library_file(token, int(item["published_file_id"]))


def _catalog_results(token: str, category: str = "", query: str = "") -> None:
    cursor = 0
    offset = 0
    while True:
        result = call("file", "catalog_list", {
            "category": category, "query": query, "offset": offset, "limit": 50}, token)
        listing = list(result["files"])
        total = int(result["total"])
        cursor = max(0, min(cursor, len(listing) - 1)) if listing else 0
        label = f"Category: {category}" if category else f"Search: {query}"
        heading("MARS HISTORICAL CATALOG", f"{label}  /  {total} record(s)")
        if listing:
            start, end = page_window(listing, cursor)
            if category:
                print(styled(f"{'':2} {'FILE':31} {'SIZE':>7}  STATUS", BOLD))
            else:
                print(styled(f"{'':2} {'FILE':24} {'CATEGORY':16} {'SIZE':>7}  STATUS", BOLD))
            for position in range(start, end):
                item = listing[position]
                if category:
                    line = (f"{offset + position + 1:4}. {clip(item['filename'], 31):31} "
                            f"{int(item['listed_size']):7}  "
                            f"{CATALOG_STATUS.get(str(item['recovery_status']), 'UNKNOWN')}")
                else:
                    line = (f"{offset + position + 1:4}. {clip(item['filename'], 24):24} "
                            f"{clip(item['category'], 16):16} {int(item['listed_size']):7}  "
                            f"{CATALOG_STATUS.get(str(item['recovery_status']), 'UNKNOWN')}")
                print(styled(line, REVERSE) if position == cursor else line)
        else:
            print(styled("No historical catalog entries match.", DIM))
        print()
        print(styled("[↑/↓] move  [Enter] details  [N/P] next/previous  [X] exit", DIM))
        choice = key_input("Catalog> ")
        if choice in ("e", "q", "x"):
            return
        if choice == "" and listing:
            _catalog_detail(token, int(listing[cursor]["id"]))
        elif choice == "n" and result.get("next_offset") is not None:
            offset = int(result["next_offset"])
            cursor = 0
        elif choice == "p" and offset:
            offset = max(0, offset - 50)
            cursor = 0
        else:
            cursor = _move_cursor(choice, cursor, listing)


def _historical_catalog(token: str) -> None:
    cursor = 0
    while True:
        areas = list(call("file", "catalog_areas", token=token)["areas"])
        cursor = max(0, min(cursor, len(areas) - 1)) if areas else 0
        heading("MARS HISTORICAL CATALOG",
                "Original 1990 directory listing; catalog entries are not downloads")
        start, end = page_window(areas, cursor)
        print(styled(f"{'':2} {'ORIGINAL CATEGORY':24} {'FILES':>6} {'FOUND':>6}", BOLD))
        for position in range(start, end):
            row = areas[position]
            line = (f"{position + 1:2}. {clip(row['category'], 24):24} "
                    f"{int(row['files']):6} {int(row['recovered'] or 0):6}")
            print(styled(line, REVERSE) if position == cursor else line)
        print()
        print(styled("[↑/↓] move  [Enter] browse  [S] search filenames  [X] exit", DIM))
        choice = key_input("Catalog> ")
        if choice in ("e", "q", "x"):
            return
        if choice == "" and areas:
            _catalog_results(token, category=str(areas[cursor]["category"]))
        elif choice == "s":
            query = _mail_field("Filename contains", required=False)
            if query is not None and query:
                _catalog_results(token, query=query)
        else:
            cursor = _move_cursor(choice, cursor, areas)


def files(token: str) -> None:
    """Browsable approved library plus a clearly separate historic catalog."""
    cursor = 0
    while True:
        try:
            areas = list(call("file", "areas", token=token)["areas"])
            cursor = max(0, min(cursor, len(areas) - 1)) if areas else 0
            heading("FILE LIBRARY", "Approved read-only files; C opens the historical catalog")
            print(styled(f"{'':2} {'AREA':28} {'FILES':>6} {'BYTES':>9}", BOLD))
            start, end = page_window(areas, cursor)
            for position in range(start, end):
                row = areas[position]
                line = (f"{position + 1:2}. {clip(row['area'], 28):28} "
                        f"{int(row['files']):6} {int(row['size'] or 0):9}")
                print(styled(line, REVERSE) if position == cursor else line)
            print()
            print(styled("[↑/↓] move  [Enter] open  [C] catalog  [X] exit", DIM))
            choice = key_input("Files> ")
            if choice in ("e", "q", "x"):
                return
            if choice == "" and areas:
                _library_area(token, str(areas[cursor]["area"]))
            elif choice == "c":
                _historical_catalog(token)
            else:
                cursor = _move_cursor(choice, cursor, areas)
        except KeyboardInterrupt:
            return
        except (RPCError, ValueError, EOFError) as exc:
            show_error(exc)
            pause()


def chat_send(sock: socket.socket, token: str, op: str, data: dict[str, object] | None = None) -> None:
    sock.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": op,
                                "token": token, "data": data or {}}))


class LiveCommandLine:
    """Bounded, in-memory history for the native Chat and IRC prompts.

    History deliberately belongs to one foreground UI invocation.  It is not
    persisted, shared with another login, or sent to a service.  KeyDecoder
    consumes terminal escape sequences before this object ever sees text.
    """

    def __init__(self, maximum: int, history_maximum: int = 50):
        self.maximum = maximum
        self.history_maximum = history_maximum
        self.buffer = ""
        self.history: list[str] = []
        self.history_index: int | None = None
        self.draft = ""

    def append(self, value: str) -> bool:
        if not value or len(self.buffer) + len(value) > self.maximum:
            return False
        self.buffer += value
        return True

    def backspace(self) -> None:
        self.buffer = self.buffer[:-1]

    def recall(self, direction: int) -> bool:
        if not self.history:
            return False
        if direction < 0:
            if self.history_index is None:
                self.draft = self.buffer
                self.history_index = len(self.history) - 1
            elif self.history_index > 0:
                self.history_index -= 1
            else:
                return False
            self.buffer = self.history[self.history_index]
            return True
        if self.history_index is None:
            return False
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.buffer = self.history[self.history_index]
        else:
            self.history_index = None
            self.buffer = self.draft
        return True

    def submit(self) -> str:
        line = self.buffer
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
            del self.history[:-self.history_maximum]
        self.buffer = ""
        self.history_index = None
        self.draft = ""
        return line


def _live_prompt(prompt: str, line: LiveCommandLine) -> None:
    sys.stdout.write("\r\033[K" + prompt + line.buffer)
    sys.stdout.flush()


def _live_notice(message: str, prompt: str, line: LiveCommandLine,
                 connected: bool) -> None:
    sys.stdout.write("\r\033[K" + message + "\r\n")
    if connected:
        sys.stdout.write(prompt + line.buffer)
    sys.stdout.flush()


def _live_help(kind: str, room_name: str = "") -> list[str]:
    if kind == "irc":
        return [
            "IRC commands: /help [command], /join #channel, /channels [filter]",
            "              /names [#channel], /who [#channel], /whois nick",
            "              /msg nick text, /notice nick text, /me action",
            "              /topic [text], /away [message], /back, /exit",
            "Use /help all for server and channel-operator commands.",
        ]
    lines = [
        "Commands: /rooms, /join room, /who, /whois user, /msg user text",
        "          /me action, /nick chatid, /ignore user, /unignore user",
        "          /motd, /invite user, /lock on|off, /secret on|off",
        "          /op user, /kick user, /help, /exit",
        "Type ordinary text to speak in the current room.",
        "Native Chat is local. Use Lobby > IRC > MarsHotel for public IRC.",
    ]
    return lines


IRC_HELP = {
    "join": "/join #channel              Join and switch to a channel",
    "part": "/part [reason]              Leave the current channel",
    "channels": "/channels [filter]         List up to 100 visible channels",
    "names": "/names [#channel]          List channel nicknames",
    "who": "/who [#channel]            Show channel presence",
    "whois": "/whois nick                Show public IRC identity details",
    "msg": "/msg nick text             Send a private message",
    "notice": "/notice nick text          Send a private notice",
    "me": "/me action                 Send a channel action",
    "topic": "/topic [text|-]            View, set, or clear the topic",
    "away": "/away [message]            Mark yourself away",
    "back": "/back                      Clear away status",
    "motd": "/motd                      Show the server message of the day",
    "time": "/time                      Show IRC server time",
    "version": "/version                   Show IRC server version",
    "invite": "/invite nick [#channel]    Invite a user; server permissions apply",
    "kick": "/kick nick [reason]        Remove a user; channel-op permission required",
    "mode": "/mode [+-modes [nick]]     View/change channel modes; server permissions apply",
    "exit": "/exit                      Return to the IRC Center",
}


def _irc_help(argument: str = "") -> list[str]:
    """Return bounded local help without forwarding help text to IRC."""
    topic = argument.strip().lower().lstrip("/")
    if topic in IRC_HELP:
        return [IRC_HELP[topic]]
    if topic == "all":
        return [
            "CHANNELS: " + ", ".join(f"/{name}" for name in
                                      ("join", "part", "channels", "names", "who", "topic")),
            "PEOPLE:   " + ", ".join(f"/{name}" for name in
                                      ("whois", "msg", "notice", "me", "away", "back")),
            "SERVER:   /motd, /time, /version",
            "OPS:      /invite, /kick, /mode (the IRC server enforces privileges)",
            "SESSION:  /exit",
            "Type /help command for syntax. /nick and arbitrary raw commands are disabled.",
        ]
    if topic:
        return ["No help for that command. Type /help all for the command index."]
    return _live_help("irc")


def native_chat(token: str, initial_room: str = "lobby") -> None:
    if _active_pager is not None:
        _active_pager.activity("Native Chat")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sock.connect(socket_path("chat"))
        chat_send(sock, token, "join", {"room": initial_room})
    except OSError as exc:
        show_error(exc)
        pause()
        return
    clear()
    print("Connecting to MarsHotel..." if initial_room == "marshotel"
          else "Connecting to native Chat...")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    decoder = KeyDecoder()
    line_editor = LiveCommandLine(512)
    userid = ""
    room_name = "lobby"
    connected = False
    prompt = ""
    try:
        tty.setraw(fd)
        running = True
        while running:
            ready, _, _ = _notice_select([fd, sock], [], [])
            if sock in ready:
                raw = sock.recv(MAX_PACKET + 1)
                if not raw:
                    break
                event = decode_packet(raw)["data"]
                kind = str(event.get("kind", "message"))
                if kind in ("joined", "kicked") and event.get("room"):
                    room_name = str(event.get("room", room_name))
                if kind == "joined":
                    userid = str(event.get("userid", userid))
                    prompt = f"[{room_name}] {userid}: " if userid else f"[{room_name}] > "
                message = str(event.get("message", ""))
                first_connection = kind == "joined" and not connected
                if first_connection:
                    connected = True
                    _live_notice(message, prompt, line_editor, False)
                    for help_line in _live_help("chat", room_name):
                        _live_notice(help_line, prompt, line_editor, False)
                    _live_notice("Up/Down recalls this session's command history.",
                                 prompt, line_editor, False)
                    _live_prompt(prompt, line_editor)
                else:
                    _live_notice(message, prompt, line_editor, connected)
            if fd in ready:
                key, value = decoder.read(fd)
                if key != KEY_EOF and _active_pager is not None:
                    _active_pager.activity()
                if key in (KEY_INTERRUPT, KEY_EOF, KEY_ESCAPE):
                    if key == KEY_ESCAPE:
                        chat_send(sock, token, "quit")
                    break
                if not connected:
                    continue
                if key == KEY_ENTER:
                    line = line_editor.submit()
                    sys.stdout.write("\r\n")
                    if line:
                        if line.lower() in ("/exit", "/quit"):
                            chat_send(sock, token, "quit")
                            running = False
                        elif line.lower() == "/who":
                            chat_send(sock, token, "who")
                        elif line.lower() in ("/rooms", "/list"):
                            chat_send(sock, token, "rooms")
                        elif line.lower() == "/motd":
                            chat_send(sock, token, "motd")
                        elif line.lower() in ("/help", "/?"):
                            for help_line in _live_help("chat", room_name):
                                sys.stdout.write(help_line + "\r\n")
                        elif line.lower().startswith("/join "):
                            chat_send(sock, token, "join_room", {"room": line[6:].strip()})
                        elif line.lower().startswith("/whois "):
                            chat_send(sock, token, "whois", {"userid": line[7:].strip()})
                        elif line.lower().startswith("/nick "):
                            chat_send(sock, token, "nick", {"chatid": line[6:].strip()})
                        elif line.lower().startswith("/ignore "):
                            chat_send(sock, token, "ignore", {"userid": line[8:].strip()})
                        elif line.lower().startswith("/unignore "):
                            chat_send(sock, token, "unignore", {"userid": line[10:].strip()})
                        elif line.lower().startswith("/me "):
                            chat_send(sock, token, "action", {"message": line[4:].strip()})
                        elif line.lower().startswith("/invite "):
                            chat_send(sock, token, "invite", {"userid": line[8:].strip()})
                        elif line.lower().startswith("/op "):
                            chat_send(sock, token, "room_op", {"userid": line[4:].strip()})
                        elif line.lower().startswith("/kick "):
                            chat_send(sock, token, "kick", {"userid": line[6:].strip()})
                        elif line.lower().startswith(("/lock ", "/secret ")):
                            command_name, setting = line[1:].split(None, 1)
                            if setting.lower() in ("on", "off"):
                                chat_send(sock, token, "room_flags", {
                                    "flag": "locked" if command_name.lower() == "lock" else "secret",
                                    "enabled": setting.lower() == "on",
                                })
                        elif line.lower().startswith("/msg "):
                            parts = line.split(None, 2)
                            if len(parts) == 3:
                                chat_send(sock, token, "private",
                                          {"userid": parts[1], "message": parts[2]})
                        else:
                            chat_send(sock, token, "message", {"message": line})
                    if running:
                        _live_prompt(prompt, line_editor)
                elif key == KEY_BACKSPACE:
                    line_editor.backspace()
                    _live_prompt(prompt, line_editor)
                elif key == KEY_UP:
                    if line_editor.recall(-1):
                        _live_prompt(prompt, line_editor)
                elif key == KEY_DOWN:
                    if line_editor.recall(1):
                        _live_prompt(prompt, line_editor)
                elif key == KEY_CHAR and line_editor.append(value):
                    _live_prompt(prompt, line_editor)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sock.close()
    print("\nLeaving Chat.")
    pause()


def embedded_irc(token: str, network: str = "local") -> None:
    """Run the restricted IRC UI over the credential-owning bridge service."""
    if _active_pager is not None:
        _active_pager.activity("IRC " + ("Public" if network == "public" else "Local"))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        sock.connect(socket_path("irc"))
        sock.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "connect",
                                    "token": token, "data": {"network": network}}))
    except OSError as exc:
        show_error(exc)
        pause()
        return
    clear()
    print("Connecting to MarsHotel public IRC..." if network == "synchronet"
          else "Connecting to local IRC...")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    decoder = KeyDecoder()
    line_editor = LiveCommandLine(400)
    nick = ""
    channel = "#lobby"
    connected = False
    prompt = ""
    try:
        tty.setraw(fd)
        running = True
        while running:
            ready, _, _ = _notice_select([fd, sock], [], [])
            if sock in ready:
                raw = sock.recv(MAX_PACKET + 1)
                if not raw:
                    break
                event = decode_packet(raw)["data"]
                kind = str(event.get("kind", "message"))
                if kind == "connected":
                    nick = str(event.get("nick", nick))
                    channel = str(event.get("channel", channel))
                elif kind == "channel":
                    channel = str(event.get("channel", channel))
                prompt = f"[{channel or 'no-channel'}] {nick}> " if nick else "> "
                message = str(event.get("message", ""))
                first_connection = kind == "connected" and not connected
                if first_connection:
                    connected = True
                    _live_notice(message, prompt, line_editor, False)
                    for help_line in _live_help("irc"):
                        _live_notice(help_line, prompt, line_editor, False)
                    _live_notice("Up/Down recalls this session's command history.",
                                 prompt, line_editor, False)
                    _live_prompt(prompt, line_editor)
                else:
                    _live_notice(message, prompt, line_editor, connected)
            if fd in ready:
                key, value = decoder.read(fd)
                if key != KEY_EOF and _active_pager is not None:
                    _active_pager.activity()
                if key in (KEY_INTERRUPT, KEY_EOF, KEY_ESCAPE):
                    if key == KEY_ESCAPE:
                        sock.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "quit",
                                                    "token": token, "data": {}}))
                    break
                if not connected:
                    continue
                if key == KEY_ENTER:
                    line = line_editor.submit()
                    sys.stdout.write("\r\n")
                    if line.lower() in ("/exit", "/quit"):
                        sock.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "quit",
                                                    "token": token, "data": {}}))
                        running = False
                    elif (line.lower() in ("/help", "/?") or
                          line.lower().startswith("/help ")):
                        argument = line[5:] if line.lower().startswith("/help") else ""
                        for help_line in _irc_help(argument):
                            sys.stdout.write(help_line + "\r\n")
                    elif line:
                        sock.sendall(encode_packet({"v": PROTOCOL_VERSION, "op": "input",
                                                    "token": token, "data": {"line": line}}))
                    if running:
                        _live_prompt(prompt, line_editor)
                elif key == KEY_BACKSPACE:
                    line_editor.backspace()
                    _live_prompt(prompt, line_editor)
                elif key == KEY_UP:
                    if line_editor.recall(-1):
                        _live_prompt(prompt, line_editor)
                elif key == KEY_DOWN:
                    if line_editor.recall(1):
                        _live_prompt(prompt, line_editor)
                elif key == KEY_CHAR and line_editor.append(value):
                    _live_prompt(prompt, line_editor)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sock.close()
    print("\nLeaving IRC.")
    pause()


def irc_center(token: str) -> None:
    """Use one IRC interface for the private and public IRC networks."""
    while True:
        heading("IRC CENTER", "One IRC client, private or public network")
        print("[L]ocal IRC       Embedded IRC on the private Mars network")
        print("[M]arsHotel       Direct IRC connection to Synchronet #MarsHotel")
        print("[X] Exit           Return to the Lobby")
        try:
            choice = key_input("IRC> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            continue
        if choice in ("e", "x", "q"):
            return
        if choice == "l":
            embedded_irc(token, "local")
        elif choice == "m":
            embedded_irc(token, "synchronet")


def _pane_lines(messages: list[str], columns: int, rows: int) -> list[str]:
    rendered: list[str] = []
    for message in messages:
        rendered.extend(textwrap.wrap(message, width=columns, replace_whitespace=False,
                                      drop_whitespace=False) or [""])
    return rendered[-rows:]


def _draw_direct_talk(peer: str, incoming: list[str], outgoing: list[str],
                      buffer: str, status: str, peer_typing: bool = False) -> None:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    pane_rows = max(3, (max(18, size.lines) - 8) // 2)
    top = _pane_lines(incoming, columns - 2, pane_rows)
    bottom = _pane_lines(outgoing, columns - 2, pane_rows)
    divider = "-" * columns
    sys.stdout.write("\033[2J\033[H")
    title = (f"TALK < {peer}  (/help)" if columns < 60 else
             f"DIRECT TALK  < {peer}   (type /help for commands)")
    sys.stdout.write(styled(clip(title, columns), BOLD, CYAN) + "\r\n")
    for row in top:
        sys.stdout.write(row + "\r\n")
    for _ in range(pane_rows - len(top)):
        sys.stdout.write("\r\n")
    sys.stdout.write(styled(divider, DIM) + "\r\n")
    sys.stdout.write(styled("YOU", BOLD, YELLOW) + "\r\n")
    for row in bottom:
        sys.stdout.write(row + "\r\n")
    for _ in range(pane_rows - len(bottom)):
        sys.stdout.write("\r\n")
    sys.stdout.write(styled(divider, DIM) + "\r\n")
    status_line = f"[{peer} is typing]" if peer_typing else status
    sys.stdout.write(clip(status_line, columns) + "\r\n")
    sys.stdout.write("> " + buffer)
    sys.stdout.flush()


def direct_talk(pager: Pager, peer: str, page_id: str | None = None,
                answering: bool = False) -> tuple[bool, str]:
    """Run accepted or ringing one-to-one Talk on the session pager socket."""
    if answering:
        if page_id is None:
            return False, ""
        pager.pending.pop(page_id, None)
        pager.send("answer_page", {"page_id": page_id})
        status = f"Answering {peer}..."
    else:
        pager.send("page", {"userid": peer})
        status = f"Paging {peer}..."
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buffer = ""
    incoming: list[str] = []
    outgoing: list[str] = []
    connected = False
    local_typing = False
    peer_typing = False
    active_page_id = page_id
    final_notice = ""
    _draw_direct_talk(peer, incoming, outgoing, buffer, status, peer_typing)
    try:
        tty.setraw(fd)
        running = True
        while running:
            ready, _, _ = _notice_select([fd, pager.sock], [], [])
            if pager.sock in ready:
                event = pager.receive()
                kind = str(event.get("kind", ""))
                message = str(event.get("message", ""))
                if kind == "page_sent":
                    value = event.get("page_id")
                    if isinstance(value, str):
                        active_page_id = value
                    status = message
                elif kind == "talk_started":
                    connected = True
                    peer = str(event.get("userid", peer))
                    status = f"[{peer} connected]"
                    if buffer:
                        pager.send("talk_typing", {"typing": True})
                        local_typing = True
                elif kind == "talk_typing":
                    peer_typing = bool(event.get("typing"))
                elif kind == "talk_message":
                    sender = str(event.get("userid", peer))
                    if event.get("own"):
                        outgoing.append(message)
                    else:
                        peer_typing = False
                        incoming.append(f"{sender}: {message}")
                elif kind in ("talk_ended", "page_declined", "page_expired",
                              "page_cancelled"):
                    peer_typing = False
                    if not connected:
                        final_notice = message
                    running = False
                elif kind == "error":
                    status = message
                    if not connected:
                        final_notice = message
                        running = False
                elif kind == "page":
                    status = message
                if running:
                    _draw_direct_talk(peer, incoming, outgoing, buffer, status, peer_typing)
                    if kind == "talk_started":
                        status = ""
            if fd in ready:
                chunk = os.read(fd, 1)
                if chunk:
                    pager.activity()
                if not chunk or chunk in (b"\x03", b"\x04"):
                    if connected:
                        pager.send("talk_end")
                    elif active_page_id:
                        pager.send("cancel_page", {"page_id": active_page_id})
                    break
                if chunk in (b"\r", b"\n"):
                    line = buffer
                    buffer = ""
                    if local_typing:
                        pager.send("talk_typing", {"typing": False})
                        local_typing = False
                    if line.lower() in ("/exit", "/quit", "/cancel"):
                        if connected:
                            pager.send("talk_end")
                        elif active_page_id:
                            pager.send("cancel_page", {"page_id": active_page_id})
                        status = ""
                    elif line.lower() in ("/help", "/?"):
                        status = "Commands: /exit ends the call; /help shows this help."
                    elif line and connected:
                        pager.send("talk_message", {"message": line})
                    elif line:
                        status = "Still ringing; type /cancel to stop paging."
                    _draw_direct_talk(peer, incoming, outgoing, buffer, status, peer_typing)
                elif chunk in (b"\x7f", b"\x08"):
                    buffer = buffer[:-1]
                    if connected and not buffer and local_typing:
                        pager.send("talk_typing", {"typing": False})
                        local_typing = False
                    _draw_direct_talk(peer, incoming, outgoing, buffer, status, peer_typing)
                elif 32 <= chunk[0] <= 126 and len(buffer) < 512:
                    buffer += chunk.decode("ascii")
                    if connected and not local_typing:
                        pager.send("talk_typing", {"typing": True})
                        local_typing = True
                    _draw_direct_talk(peer, incoming, outgoing, buffer, status, peer_typing)
    finally:
        if connected and local_typing:
            try:
                pager.send("talk_typing", {"typing": False})
            except OSError:
                pass
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return connected, final_notice


def _choose_page(pager: Pager) -> tuple[str, dict[str, object]] | None:
    pages = list(pager.pending.items())
    if not pages:
        print("\nThere are no unanswered pages.")
        pause()
        return None
    if len(pages) == 1:
        return pages[0]
    print()
    for position, (_, event) in enumerate(pages, 1):
        print(f" {position}. {event.get('userid', 'unknown')}  "
              f"{event.get('display_name', '')}")
    try:
        selected = int(input("Page number: ").strip()) - 1
    except (ValueError, EOFError):
        return None
    if 0 <= selected < len(pages):
        return pages[selected]
    return None


def _choose_online_user(token: str, pager: Pager) -> str | None:
    """Choose a Direct Talk peer with immediate keys or type a userid."""
    cursor = 0
    selected_user = ""
    profiles: dict[str, str] = {}
    while True:
        try:
            users = [str(item) for item in call("session", "online", token=token)["users"]
                     if str(item) != pager.userid]
            if selected_user in users:
                cursor = users.index(selected_user)
            else:
                cursor = max(0, min(cursor, len(users) - 1)) if users else 0
            selected_user = users[cursor] if users else ""
            rows: list[dict[str, object]] = []
            for name in users:
                if name not in profiles:
                    try:
                        profiles[name] = str(call("auth", "lookup", {"userid": name})
                                             .get("display_name", ""))
                    except RPCError:
                        profiles[name] = ""
                rows.append({"userid": name, "display_name": profiles[name]})
        except RPCError as exc:
            show_error(exc)
            pause()
            return None

        heading("DIRECT TALK", "Choose an online user to page")
        if rows:
            start, end = page_window(rows, cursor)
            print(styled(f"{'':2} {'USERID':12} DISPLAY NAME", BOLD))
            for position in range(start, end):
                row = rows[position]
                line = (f"{position + 1:2}. {clip(row['userid'], 12):12} "
                        f"{clip(row['display_name'], max(16, width() - 19))}")
                print(styled(line, REVERSE) if position == cursor else line)
        else:
            print(styled("No other users are currently online.", DIM))
        print()
        print(styled("[↑/↓] move   [Enter] page   [N] type userid   [X] cancel", DIM))
        try:
            choice = key_input("Call> ")
        except EOFError:
            return None
        if choice in ("x", "q"):
            return None
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
            selected_user = str(rows[cursor]["userid"]) if rows else ""
        elif choice == "k":
            cursor = max(0, cursor - 1)
            selected_user = str(rows[cursor]["userid"]) if rows else ""
        elif choice == "]" and rows:
            cursor = min(len(rows) - 1, page_window(rows, cursor)[1])
            selected_user = str(rows[cursor]["userid"])
        elif choice == "[" and rows:
            start, end = page_window(rows, cursor)
            cursor = max(0, start - max(1, end - start))
            selected_user = str(rows[cursor]["userid"])
        elif choice == "" and rows:
            return str(rows[cursor]["userid"])
        elif choice == "n":
            name = input("Userid to page: ").strip().lower()
            return name or None
        elif choice.isdigit() and rows:
            position = int(choice) - 1
            if 0 <= position < len(rows):
                return str(rows[position]["userid"])


def elapsed(seconds: int) -> str:
    """Render a compact, stable elapsed time for an 80-column roster."""
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600:02}h"


def online_user_rows(token: str) -> list[dict[str, object]]:
    """Fetch and validate the privacy-appropriate presence view."""
    rows = call("session", "online_details", token=token).get("users")
    if not isinstance(rows, list):
        raise RPCError("invalid online-user directory")
    validated: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("userid"), str):
            raise RPCError("invalid online-user entry")
        values = (row.get("sessions"), row.get("connected_seconds"),
                  row.get("idle_seconds"))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values):
            raise RPCError("invalid online-user entry")
        validated.append(row)
    return validated


def render_online_users(rows: list[dict[str, object]]) -> None:
    """Render presence without network addresses or other connection metadata."""
    print(styled(f"{'USERID':12} {'SESS':>4} {'CONNECTED':>10} {'IDLE':>10}  ACTIVITY", BOLD))
    for row in rows:
        idle = int(row["idle_seconds"])
        status = "IDLE" if idle >= IDLE_AFTER_SECONDS else str(row.get("activity", "Online"))
        print(f"{clip(row['userid'], 12):12} {int(row['sessions']):4} "
              f"{elapsed(int(row['connected_seconds'])):>10} {elapsed(idle):>10}  {status}")
    if not rows:
        print(styled("No users are currently online.", DIM))


def show_online_users(token: str) -> None:
    """Clear the terminal and display the complete authenticated presence view."""
    rows = online_user_rows(token)
    heading("ACTIVE USERS", f"{len(rows)} user(s) online  /  idle after 5 minutes")
    render_online_users(rows)


def member_directory(token: str) -> None:
    """Browsable public member directory with live activity and last check-in."""
    query = ""
    cursor = 0
    while True:
        result = call("auth", "member_directory", {"query": query, "offset": 0})
        rows = list(result["users"])
        online = {str(row["userid"]).casefold(): row for row in online_user_rows(token)}
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
        heading("MEMBER DIRECTORY", "Public profiles and recent activity",
                f"{len(online)} ONLINE")
        print(styled(f"{'':2} {'USERID':12} {'DISPLAY NAME':20} ACTIVITY / LAST LOGIN", BOLD))
        start, end = page_window(rows, cursor)
        for position in range(start, end):
            row = rows[position]
            active = online.get(str(row["userid"]).casefold())
            if active:
                activity = str(active.get("activity", "Online"))
                if int(active.get("idle_seconds", 0)) >= IDLE_AFTER_SECONDS:
                    activity = "Idle " + elapsed(int(active["idle_seconds"]))
            else:
                last = int(row.get("last_login", 0))
                activity = "Last " + (mail_datetime(last) if last else "never")
            line = (f"{position + 1:2}. {clip(row['userid'], 12):12} "
                    f"{clip(row['display_name'], 20):20} "
                    f"{clip(activity, max(12, width() - 39))}")
            print(styled(line, REVERSE) if position == cursor else line)
        if query:
            print(styled(f"Filter: {safe_text(query)}", DIM))
        if not rows:
            print(styled("No members match this filter.", DIM))
        print("\n[↑/↓] move  [Enter] profile  [F] filter  [O] online only  [X] exit")
        choice = key_input("Members> ")
        if choice in ("x", "q"):
            return
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
        elif choice == "k":
            cursor = max(0, cursor - 1)
        elif choice == "f":
            query = input("Userid begins with (blank shows all): ").strip()
            cursor = 0
        elif choice == "o":
            show_online_users(token)
            pause()
        elif choice == "" and rows:
            row = rows[cursor]
            content = (f"Userid: {row['userid']}\nDisplay name: {row['display_name']}\n"
                       f"Last login: " + (mail_datetime(row["last_login"])
                                           if int(row.get("last_login", 0)) else "never") +
                       "\n\n" + (str(row.get("profile_text", "")) or "No public profile."))
            _text_viewer("MEMBER PROFILE", "Public account information", content)


def talk(token: str, pager: Pager) -> None:
    notice = ""
    while True:
        roster_available = True
        try:
            roster = online_user_rows(token)
        except RPCError:
            roster = []
            roster_available = False
        active_count = str(len(roster)) if roster_available else "?"
        heading("TALK CENTER", "Native conversation, presence, and profiles",
                f"ACTIVE: {active_count} / PAGER: {'ON' if pager.available else 'DND'}")
        if notice:
            print(notice)
            notice = ""
        print("[C]hat       Enter the local native Chat lobby")
        print("[T]alk       Page a user for split-screen Direct Talk")
        print(f"[A]nswer     Answer an incoming page ({len(pager.pending)} waiting)")
        print("[R]eject     Decline an incoming page")
        print("[U]sers      Full active-user page")
        print("[P]rofile    View a user's public profile")
        print("[S]tatus     Toggle pager Available / Do Not Disturb")
        print("[M]onitor    Notify when a member comes online")
        print("[D]ate       Show UTC and account date/time")
        print("[X] Exit")
        try:
            choice = key_input("Talk> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            continue
        if choice in ("e", "x"):
            return
        if choice == "c":
            native_chat(token)
        elif choice == "t":
            try:
                name = _choose_online_user(token, pager)
                if name:
                    connected, call_notice = direct_talk(pager, name)
                    notice = "[Direct Talk Closed]" if connected else call_notice
            except (RPCError, ValueError, EOFError) as exc:
                show_error(exc)
                pause()
        elif choice == "a":
            selected = _choose_page(pager)
            if selected is not None:
                page_id, event = selected
                connected, call_notice = direct_talk(
                    pager, str(event.get("userid", "unknown")), page_id, True)
                notice = "[Direct Talk Closed]" if connected else call_notice
        elif choice == "r":
            selected = _choose_page(pager)
            if selected is not None:
                page_id, event = selected
                pager.pending.pop(page_id, None)
                pager.send("decline_page", {"page_id": page_id})
                print(f"\nPage from {event.get('userid', 'unknown')} declined.")
                pause()
        elif choice in ("u", "l", "o"):
            try:
                show_online_users(token)
            except RPCError as exc:
                show_error(exc)
            pause()
        elif choice in ("p", "q"):
            try:
                name = input("Userid: ").strip()
                profile = call("auth", "lookup", {"userid": name})
                print(f"\n{profile['userid']} - {profile['display_name']}")
                about = str(profile.get("profile_text", ""))
                if about:
                    print(textwrap.fill(about, width=min(width(), 100)))
            except RPCError as exc:
                show_error(exc)
            pause()
        elif choice == "s":
            pager.send("pager_status", {"available": not pager.available})
            try:
                event = pager.receive()
                notice = str(event.get("message", "Pager status updated."))
            except EOFError:
                return
        elif choice == "m":
            try:
                name = choose_mail_recipient(token, purpose="monitor")
                if name:
                    pager.send("monitor", {"userid": name})
                    notice = f"Monitoring @{name}; sign-in alerts follow you throughout the BBS this session."
            except (OSError, RPCError) as exc:
                notice = "Monitor: " + safe_text(exc)
        elif choice == "d":
            try:
                profile = call("auth", "profile")
                utc_line, local_line = current_time_lines(str(profile["timezone"]))
                print("\n" + utc_line)
                print(local_line)
            except (RPCError, ValueError) as exc:
                show_error(exc)
            pause()


def board_counts(token: str) -> None:
    try:
        listing = call("board", "boards", token=token)["boards"]
        heading("BOARD ACTIVITY", "Message totals and unread activity")
        print(styled(f"{'BOARD':24} {'POSTS':>7} {'UNREAD':>8}", BOLD))
        for board in listing:
            print(f"{clip(board['name'], 24):24} {int(board['posts']):7} {int(board.get('unread', 0)):8}")
    except RPCError as exc:
        show_error(exc)
    pause()


def welcome_screen() -> None:
    """Browsable About/history center; retained name for menu compatibility."""
    while True:
        heading("MARS HOTEL REVIVAL", "About the project and the BBS that inspired it")
        print("Explore what this Revival is, what is known about the original Mars")
        print("Hotel, and which details are recovered evidence versus reconstruction.")
        print()
        print("[A]bout       About this revival")
        print("[H]istory     The Mars Hotel story and timeline")
        print("[E]vidence    Sources, confidence, and honest gaps")
        print("[X] Exit       Return to the Lobby")
        try:
            choice = key_input("Revival> ")
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("x", "q"):
            return
        if choice == "a":
            _information_page("ABOUT MARS HOTEL REVIVAL", "What you are using", "about.txt")
        elif choice == "h":
            _information_page("THE MARS HOTEL", "Verified history and context", "mars-history.txt")
        elif choice == "e":
            _information_page("HISTORY NOTES", "Evidence, reconstruction, and open questions",
                              "historical-evidence.txt")


def _information_asset(filename: str) -> str:
    """Read a shipped, administrator-owned information page."""
    if not filename.isascii() or not filename.replace("-", "").replace(".", "").isalnum():
        raise ValueError("invalid information page")
    asset = Path(__file__).resolve().parent.parent / "assets" / "info" / filename
    return asset.read_text(encoding="utf-8")


def _information_page(title: str, subtitle: str, filename: str) -> None:
    try:
        content = _information_asset(filename)
    except (OSError, UnicodeError, ValueError) as exc:
        show_error(exc)
        pause()
        return
    _text_viewer(title, subtitle, content)


def help_center() -> None:
    """Topic-oriented command help using the normal safe text viewer."""
    topics = {
        "g": ("GETTING AROUND", "Menus, navigation, and common keys", "help-general.txt"),
        "b": ("BOARDS & MAIL", "Public discussions and private post", "help-boards-mail.txt"),
        "t": ("TALK & NATIVE CHAT", "Presence, paging, and local conversation", "help-live.txt"),
        "i": ("IRC & PUBLIC ROOM", "Local IRC and the public MarsHotel room", "help-irc.txt"),
        "f": ("FILE LIBRARY", "Curated files and the Mars catalog", "help-files.txt"),
        "n": ("INFORMATION NETWORK", "Weather, wires, radio, quakes, and orbit data",
              "help-information.txt"),
        "r": ("GALACTIC ARCADE", "Games, saves, scores, and safe exits", "help-games.txt"),
        "p": ("PROFILE & TERMINAL", "Profile, password, time zone, and terminal", "help-account.txt"),
    }
    while True:
        heading("HELP CENTER", "Choose a topic; every page is safe to leave with X or Escape")
        print("[G]eneral      Navigation and keyboard reference")
        print("[B]oards       Boards, editor, and Post Office")
        print("[T]alk         Native Chat, paging, and Direct Talk")
        print("[I]RC          Local IRC and the public MarsHotel room")
        print("[F]iles        Library, reader, and historical catalog")
        print("[N]etwork      Live Information Network and data freshness")
        print("[R]etro games  Galactic Arcade, WOPR, and Oregon Trail")
        print("[P]rofile      Account, security, terminal, and time zone")
        print("[X] Exit        Return to the Lobby")
        try:
            choice = key_input("Help> ")
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("x", "q"):
            return
        topic = topics.get(choice)
        if topic is not None:
            _information_page(*topic)


def change_password(identity: dict[str, object]) -> bool:
    old = getpass.getpass("Old password: ")
    new = getpass.getpass("New password: ")
    again = getpass.getpass("Retype new password: ")
    if new != again:
        print("Passwords do not match.")
        changed = False
    else:
        try:
            call("auth", "change_password", {"userid": identity["userid"],
                                                "old_password": old, "new_password": new})
            print("Password changed.")
            identity["password_change_required"] = False
            changed = True
        except RPCError as exc:
            show_error(exc)
            changed = False
    pause()
    return changed


def required_password_change(identity: dict[str, object]) -> bool:
    """Block access after an administrator reset until the user changes it."""
    for _attempt in range(3):
        heading("PASSWORD CHANGE REQUIRED",
                "An administrator issued a temporary password; choose your own before entry")
        try:
            new = getpass.getpass("New password (12-64 characters): ")
            again = getpass.getpass("Retype new password: ")
        except (EOFError, KeyboardInterrupt):
            return False
        if new != again:
            print("Passwords do not match. Please try again.")
            continue
        try:
            call("auth", "change_password", {"userid": identity["userid"],
                                               "new_password": new})
            identity["password_change_required"] = False
            print("Password changed. Continuing to the Mars Hotel Lobby.")
            return True
        except RPCError as exc:
            show_error(exc)
    print("Password was not changed; this session will close.")
    return False


def show_account(identity: dict[str, object]) -> None:
    try:
        profile = call("auth", "profile")
        identity["display_name"] = profile["display_name"]
        print(f"\nUserid:       {profile['userid']}")
        print(f"Display name: {profile['display_name']}")
        print(f"Email:        {profile['email'] or '(not set)'}  [private]")
        print(f"Time zone:    {profile['timezone']}")
        roles = profile.get("roles", ["Sysop" if profile.get("is_admin") else "Basic-1"])
        print("Roles:        " + ", ".join(str(role) for role in roles))
        print("About you:    " + (str(profile["profile_text"]) or "(not set)"))
        print("Signature:    " + (str(profile.get("signature", "")) or "(not set)"))
        last_login = int(profile.get("last_login", 0))
        print("Last login:   " + (mail_datetime(last_login) if last_login else "first visit"))
    except RPCError as exc:
        show_error(exc)
    pause()


def edit_account(identity: dict[str, object]) -> None:
    try:
        profile = call("auth", "profile")
        current_display = str(profile["display_name"])
        current_email = str(profile["email"])
        current_about = str(profile["profile_text"])
        current_timezone = str(profile["timezone"])
        current_signature = str(profile.get("signature", ""))
        display = input(f"Display name [{current_display}]: ").strip() or current_display
        email_value = input(
            f"Email [{current_email or 'not set'}] (blank keeps, '-' clears): ").strip()
        email = "" if email_value == "-" else (email_value or current_email)
        if current_about:
            print("Current About you: " + current_about)
        about_value = input("About you (blank keeps, '-' clears; 240 characters): ").strip()
        about = "" if about_value == "-" else (about_value or current_about)
        if current_signature:
            print("Current signature:\n" + current_signature)
        signature_value = input(
            "Signature (blank keeps, '-' clears; use \\n for lines): ").strip()
        signature = ("" if signature_value == "-" else
                     signature_value.replace("\\n", "\n") if signature_value else
                     current_signature)
        timezone = choose_timezone(current_timezone)
        if timezone is None:
            print("Account changes cancelled; nothing was saved.")
            pause()
            return
        updated = call("auth", "update_profile",
                       {"display_name": display, "email": email,
                        "profile_text": about, "timezone": timezone,
                        "signature": signature})
        identity["display_name"] = updated["display_name"]
        print("Account profile updated. Email remains private.")
    except RPCError as exc:
        show_error(exc)
    pause()


def utilities(identity: dict[str, object]) -> None:
    while True:
        heading("UTILITIES", "Account and terminal settings")
        print("[A]ccount     Show your account profile")
        print("[M]odify      Edit display name, email, About you, and time zone")
        print("[P]assword    Change your password")
        print("[T]erminal    Show negotiated terminal settings")
        print("[I]nfo        Version and restoration information")
        print("[X] Exit")
        try:
            choice = key_input("Utilities> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            continue
        if choice in ("e", "x"):
            return
        if choice == "a":
            show_account(identity)
        elif choice == "m":
            edit_account(identity)
        elif choice == "p":
            change_password(identity)
        elif choice == "t":
            size = shutil.get_terminal_size((80, 24))
            print(f"\nTERM={os.environ.get('TERM', 'unknown')}  {size.columns} columns x {size.lines} rows")
            try:
                profile = call("auth", "profile")
                utc_line, local_line = current_time_lines(str(profile["timezone"]))
                print("Time zone: " + str(profile["timezone"]))
                print(utc_line)
                print(local_line)
            except (RPCError, ValueError) as exc:
                show_error(exc)
            pause()
        elif choice == "i":
            print("\nMars Hotel Revival V2 - compartmentalized preview")
            print("Mars Hotel-inspired; reconstructed behavior and locally created content.")
            pause()


ADMIN_CAPABILITIES = {
    "account.view", "account.manage", "account.password_reset", "account.delete",
    "account.roles", "board.manage", "board.moderate", "session.manage",
    "audit.read", "system.health", "federation.manage", "music.manage",
}


def _admin_identity(identity: dict[str, object]) -> dict[str, object]:
    """Refresh roles at the authority before drawing privileged controls."""
    profile = call("auth", "profile")
    identity["roles"] = list(profile.get("roles", []))
    identity["capabilities"] = list(profile.get("capabilities", []))
    identity["is_admin"] = bool(profile.get("is_admin"))
    return identity


def _can(identity: dict[str, object], capability: str) -> bool:
    values = identity.get("capabilities", [])
    return isinstance(values, list) and capability in values


def _admin_matches(rows: list[dict[str, object]], query: str,
                   fields: tuple[str, ...]) -> list[dict[str, object]]:
    """Filter bounded authority results without trusting values for output."""
    needle = query.strip().casefold()
    if not needle:
        return rows
    return [row for row in rows if any(
        needle in (", ".join(str(value) for value in row.get(field, []))
                   if isinstance(row.get(field), list) else str(row.get(field, ""))).casefold()
        for field in fields)]


def _selected(rows: list[dict[str, object]], cursor: int) -> dict[str, object] | None:
    return rows[cursor] if rows and 0 <= cursor < len(rows) else None


def _admin_role_picker(userid: object, definitions: list[dict[str, object]],
                       current_roles: object) -> list[str] | None:
    """Select effective roles without accepting free-form authorization names."""
    if not definitions:
        return None
    ordered = [str(role["name"]) for role in definitions]
    selected = {str(role) for role in current_roles} if isinstance(
        current_roles, (list, tuple, set)) else {"Basic-1"}
    selected.intersection_update(ordered)
    if not selected:
        selected = {"Basic-1"}
    cursor = 0
    notice = ""
    while True:
        heading("ASSIGN ROLES", f"Account: {safe_text(userid)}  /  server-enforced authorization",
                f"{len(selected)} selected")
        start, end = page_window(definitions, cursor)
        for position in range(start, end):
            name = ordered[position]
            marker = "X" if name in selected else " "
            line = f"{position + 1:2}. [{marker}] {name}"
            print(styled(line, REVERSE) if position == cursor else line)
        active = definitions[cursor]
        print("\n" + textwrap.fill(safe_text(active["description"]),
                                   width=max(20, min(width(), 78))))
        if notice:
            print(styled(notice, YELLOW))
        print("\n[↑/↓] move   [Space/Enter] toggle   [S] Save   [X] Cancel")
        try:
            choice = key_input("Roles> ")
        except (EOFError, KeyboardInterrupt):
            return None
        if choice in ("x", "q"):
            return None
        if choice == "j":
            cursor = min(len(definitions) - 1, cursor + 1)
            continue
        if choice == "k":
            cursor = max(0, cursor - 1)
            continue
        if choice in (" ", ""):
            role = ordered[cursor]
            if role in selected:
                selected.remove(role)
                if not selected:
                    selected.add("Basic-1")
                    notice = "Every account needs an effective role; Basic-1 retained."
                else:
                    notice = ""
            elif role == "Basic-1":
                selected = {"Basic-1"}
                notice = "Basic-1 is the ordinary-member role."
            elif role == "Sysop":
                selected = {"Sysop"}
                notice = "Sysop is exclusive because it includes every authority."
            else:
                selected.discard("Basic-1")
                selected.discard("Sysop")
                selected.add(role)
                notice = ""
            continue
        if choice == "s":
            return [name for name in ordered if name in selected]


def _admin_member_picker(token: str, title: str,
                         selected_names: list[str]) -> list[str] | None:
    selected = {name.casefold(): name for name in selected_names}
    query = ""
    cursor = 0
    while True:
        rows = list(call("auth", "member_directory", {"query": query, "offset": 0})["users"])
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
        heading(title, "Filter and toggle members; no free-form account names",
                f"{len(selected)} SELECTED")
        start, end = page_window(rows, cursor)
        for position in range(start, end):
            row = rows[position]
            marker = "X" if str(row["userid"]).casefold() in selected else " "
            line = f"{position + 1:2}. [{marker}] {clip(row['userid'], 12):12} {row['display_name']}"
            print(styled(line, REVERSE) if position == cursor else line)
        if query:
            print(styled(f"Filter: {query}", DIM))
        print("\n[↑/↓] move  [Space/Enter] toggle  [F] filter  [S] save  [X] cancel")
        choice = key_input("Members> ")
        if choice in ("x", "q"):
            return None
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
        elif choice == "k":
            cursor = max(0, cursor - 1)
        elif choice == "f":
            query = input("Userid begins with (blank shows all): ").strip()
            cursor = 0
        elif choice in ("", " ") and rows:
            name = str(rows[cursor]["userid"])
            key = name.casefold()
            if key in selected:
                del selected[key]
            elif len(selected) < 32:
                selected[key] = name
        elif choice == "s":
            return sorted(selected.values(), key=str.casefold)


def _admin_accounts(token: str, identity: dict[str, object]) -> None:
    notice = ""
    query = ""
    cursor = 0
    while True:
        try:
            all_rows = list(call("auth", "admin_accounts")["accounts"])
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        rows = _admin_matches(all_rows, query, ("userid", "display_name", "roles"))
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
        heading("ACCOUNT MANAGEMENT", "Select an account; credentials stay private",
                f"{len(rows)}/{len(all_rows)}")
        print(styled(f"{'':2} {'USERID':12} {'STATUS':9} ROLES / DISPLAY NAME", BOLD))
        start, end = page_window(rows, cursor)
        for position in range(start, end):
            row = rows[position]
            if row.get("deletion_pending"):
                status = "DELETING"
            elif row["login_enabled"]:
                status = "ENABLED"
            else:
                status = "DISABLED"
            suffix = ", ".join(str(value) for value in row["roles"])
            if row.get("password_change_required"):
                suffix += " / RESET PENDING"
            line = (f"{position + 1:2}. {clip(row['userid'], 12):12} {status:9} "
                    f"{clip(suffix + ' / ' + str(row['display_name']), max(12, width() - 29))}")
            print(styled(line, REVERSE) if position == cursor else line)
        if query:
            print(styled(f"Filter: {safe_text(query)}", DIM))
        if not rows:
            print(styled("No accounts match the filter.", DIM))
        if notice:
            print("\n" + styled(notice, YELLOW))
        print("\n[↑/↓] move  [F]ilter  [C]reate  [S]tatus  [P]assword reset  [D]elete")
        if _can(identity, "account.manage"):
            print("[I]nactive report   [K]eep/exempt from cleanup")
        if _can(identity, "account.roles"):
            print("[R]oles     Assign effective roles")
        print("[X] Exit")
        choice = key_input("Accounts> ")
        if choice in ("x", "q"):
            return
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
            continue
        if choice == "k":
            cursor = max(0, cursor - 1)
            continue
        if choice == "f":
            query = input("Filter userid, name, or role (blank shows all): ").strip()
            cursor = 0
            continue
        if choice == "c" and _can(identity, "account.manage"):
            name = input("New userid: ").strip()
            display = input("Display name: ").strip()
            first = getpass.getpass("Temporary password (12-64 characters): ")
            second = getpass.getpass("Retype temporary password: ")
            if first != second:
                notice = "Passwords do not match; account not created."
                continue
            password_value = getpass.getpass("Your password (reauthorization): ")
            try:
                result = call("auth", "admin_create_account", {
                    "userid": name, "display_name": display,
                    "new_password": first, "password": password_value})
                notice = (f"Created {result['userid']} with a temporary password; "
                          "change required at first login.")
                query = str(result["userid"])
                cursor = 0
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
            continue
        if choice == "i" and _can(identity, "account.manage"):
            raw_days = input("Inactive for at least how many days? [90]: ").strip()
            try:
                days = int(raw_days or "90")
                report = call("auth", "admin_inactive", {"days": days})["accounts"]
                lines = []
                for item in report:
                    last = int(item["last_login"] or item["created_at"])
                    lines.append(f"{item['userid']:12} {mail_datetime(last)}  "
                                 f"{'enabled' if item['login_enabled'] else 'disabled'}")
                _text_viewer("INACTIVE ACCOUNTS", f"No login in {days} days; exemptions omitted",
                             "\n".join(lines) or "No matching accounts.", preserve_columns=True)
            except (ValueError, RPCError) as exc:
                notice = "Request rejected: " + safe_text(exc)
            continue
        current = _selected(rows, cursor)
        if current is None:
            notice = "Select an account first."
            continue
        if choice == "k" and _can(identity, "account.manage"):
            desired = not bool(current.get("cleanup_exempt"))
            password_value = getpass.getpass("Your password (reauthorization): ")
            try:
                result = call("auth", "admin_set_cleanup_exempt", {
                    "userid": current["userid"], "exempt": desired,
                    "password": password_value})
                notice = (f"{result['userid']} is " +
                          ("exempt from" if desired else "eligible for") +
                          " inactive-account cleanup.")
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        if choice == "s" and _can(identity, "account.manage"):
            if current.get("deletion_pending"):
                notice = "That account is pending deletion and cannot be re-enabled."
                continue
            desired = not bool(current["login_enabled"])
            verb = "enable" if desired else "disable"
            confirm = input(f"Type {verb.upper()} to {verb} {current['userid']}: ").strip()
            if confirm.upper() != verb.upper():
                notice = "Account status unchanged."
                continue
            password_value = getpass.getpass("Your password (reauthorization): ")
            try:
                result = call("auth", "admin_set_login",
                              {"userid": current["userid"], "enabled": desired,
                               "password": password_value})
                ended = 0
                if not desired:
                    ended = int(call("session", "admin_end_account",
                                     {"account_id": int(result["account_id"])}, token)["ended"])
                notice = f"{current['userid']} is now {verb}d."
                if ended:
                    notice += f" {ended} active session(s) ended."
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "r" and _can(identity, "account.roles"):
            definitions = list(call("auth", "admin_roles")["roles"])
            selected = _admin_role_picker(current["userid"], definitions, current["roles"])
            if selected is None:
                notice = "Roles unchanged."
                continue
            password_value = getpass.getpass("Your password (reauthorization): ")
            try:
                result = call("auth", "admin_set_roles",
                              {"userid": current["userid"], "roles": selected,
                               "password": password_value})
                notice = f"Roles for {result['userid']}: {', '.join(result['roles'])}"
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "p" and _can(identity, "account.password_reset"):
            first = getpass.getpass("Temporary password (12-64 characters): ")
            second = getpass.getpass("Retype temporary password: ")
            if first != second:
                notice = "Passwords do not match; nothing changed."
                continue
            password_value = getpass.getpass("Your password (reauthorization): ")
            try:
                result = call("auth", "admin_reset_password", {
                    "userid": current["userid"], "new_password": first,
                    "password": password_value,
                })
                ended = call("session", "admin_end_account",
                             {"account_id": int(result["account_id"])}, token)["ended"]
                notice = (f"Temporary password set for {result['userid']}; "
                          f"{ended} active session(s) ended. Change required at next login.")
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "d" and _can(identity, "account.delete"):
            exact = f"DELETE {current['userid']}"
            confirm = input(f"Type {exact} to permanently delete this account: ").strip()
            if confirm != exact:
                notice = "Account deletion cancelled."
                continue
            password_value = getpass.getpass("Your password (reauthorization): ")
            prepared = False
            try:
                target = call("auth", "admin_prepare_delete", {
                    "userid": current["userid"], "password": password_value,
                })
                prepared = True
                account_id = int(target["account_id"])
                call("session", "admin_end_account", {"account_id": account_id}, token)
                call("mail", "admin_delete_account", {"account_id": account_id}, token)
                call("board", "admin_delete_account", {
                    "account_id": account_id, "userid": current["userid"]}, token)
                call("game", "admin_delete_account", {"account_id": account_id}, token)
                call("auth", "admin_finalize_delete", {
                    "userid": current["userid"], "password": password_value,
                })
                notice = (f"Deleted {current['userid']}: credentials, profile, mailbox, drafts, "
                          "and read state removed; public posts retained.")
                cursor = max(0, cursor - 1)
            except RPCError as exc:
                prefix = "Deletion paused; account remains disabled. " if prepared else ""
                notice = prefix + "Request rejected: " + safe_text(exc)


def _admin_boards(token: str) -> None:
    notice = ""
    query = ""
    cursor = 0
    selected_id: int | None = None
    while True:
        try:
            all_rows = list(call("board", "admin_boards", token=token)["boards"])
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        rows = _admin_matches(all_rows, query, ("name", "description"))
        if selected_id is not None:
            cursor = next((position for position, row in enumerate(rows)
                           if int(row["id"]) == selected_id), cursor)
            selected_id = None
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
        heading("BOARD MANAGEMENT", "Select a board; archiving preserves posts and read state",
                f"{len(rows)}/{len(all_rows)}")
        print(styled(f"{'':2} {'ID':>3} {'BOARD':22} {'POSTS':>5} {'MODE':10} STATUS", BOLD))
        start, end = page_window(rows, cursor)
        for position in range(start, end):
            row = rows[position]
            mode = "READ ONLY" if row["read_only"] else "READ/WRITE"
            status = "ARCHIVED" if row["archived"] else "ACTIVE"
            line = (f"{position + 1:2}. {int(row['id']):3} {clip(row['name'], 22):22} "
                    f"{int(row['posts']):5} {mode:10} {status}")
            print(styled(line, REVERSE) if position == cursor else line)
        if query:
            print(styled(f"Filter: {safe_text(query)}", DIM))
        if not rows:
            print(styled("No boards match the filter.", DIM))
        if notice:
            print("\n" + styled(notice, YELLOW))
        print("\n[↑/↓] move [F]ilter [C]reate [M/Enter] modify [G] managers [X] Exit")
        choice = key_input("Boards admin> ")
        if choice in ("x", "q"):
            return
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
        elif choice == "k":
            cursor = max(0, cursor - 1)
        elif choice == "f":
            query = input("Filter board name or description (blank shows all): ").strip()
            cursor = 0
        elif choice == "c":
            name = input("Board name: ").strip()
            description = input("Description: ").strip()
            read_only = input("Read-only? [y/N]: ").strip().lower() == "y"
            no_zap = input("Required in every member's New scan? [y/N]: ").strip().lower() == "y"
            try:
                result = call("board", "admin_create",
                              {"name": name, "description": description,
                               "read_only": read_only, "no_zap": no_zap}, token)
                notice = f"Created board #{result['id']} ({result['name']})."
                selected_id = int(result["id"])
                query = ""
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice in ("m", ""):
            current = _selected(rows, cursor)
            if current is None:
                notice = "Select a board first."
                continue
            name = input(f"Name [{current['name']}]: ").strip() or str(current["name"])
            description = (input(f"Description [{current['description']}]: ").strip()
                           or str(current["description"]))
            read_value = input(
                f"Read-only [{'Y' if current['read_only'] else 'N'}] (Y/N): ").strip().lower()
            archived_value = input(
                f"Archived [{'Y' if current['archived'] else 'N'}] (Y/N): ").strip().lower()
            no_zap_value = input(
                f"Required in New scan [{'Y' if current.get('no_zap') else 'N'}] (Y/N): ").strip().lower()
            read_only = bool(current["read_only"]) if not read_value else read_value == "y"
            archived = bool(current["archived"]) if not archived_value else archived_value == "y"
            no_zap = bool(current.get("no_zap")) if not no_zap_value else no_zap_value == "y"
            definitions = list(call("auth", "admin_roles")["roles"])
            current_read = [part for part in str(current.get("read_roles", "")).split(",")
                            if part]
            current_post = [part for part in str(current.get("post_roles", "")).split(",")
                            if part]
            if input("Restrict readers by role? [y/N]: ").strip().lower() == "y":
                picked = _admin_role_picker("board readers", definitions,
                                            current_read or ["Basic-1"])
                if picked is None:
                    notice = "Board settings unchanged."
                    continue
                read_roles = ",".join(picked)
            else:
                read_roles = ""
            if input("Restrict posters by role? [y/N]: ").strip().lower() == "y":
                picked = _admin_role_picker("board posters", definitions,
                                            current_post or ["Basic-1"])
                if picked is None:
                    notice = "Board settings unchanged."
                    continue
                post_roles = ",".join(picked)
            else:
                post_roles = ""
            try:
                call("board", "admin_update",
                     {"board_id": int(current["id"]), "name": name,
                      "description": description,
                      "read_only": read_only, "archived": archived, "no_zap": no_zap,
                      "read_roles": read_roles, "post_roles": post_roles}, token)
                notice = f"Updated {name}."
                selected_id = int(current["id"])
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "g":
            current = _selected(rows, cursor)
            if current is None:
                notice = "Select a board first."
                continue
            try:
                existing = call("board", "admin_managers", {
                    "board_id": int(current["id"])}, token)["managers"]
                selected = _admin_member_picker(
                    token, f"MANAGERS - {current['name']}", list(existing))
                if selected is None:
                    notice = "Board managers unchanged."
                else:
                    call("board", "admin_managers", {
                        "board_id": int(current["id"]), "managers": selected}, token)
                    notice = ("Board managers: " + ", ".join(selected)) if selected else \
                        "Board has no delegated managers."
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)


def _admin_sessions(token: str) -> None:
    notice = ""
    query = ""
    cursor = 0
    while True:
        try:
            all_rows = list(call("session", "admin_sessions", token=token)["sessions"])
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        rows = _admin_matches(
            all_rows, query, ("userid", "display_name", "session_id", "session_type"))
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
        heading("SESSION MANAGEMENT", "Select a live session; current session is protected",
                f"{len(rows)}/{len(all_rows)}")
        print(styled(
            f"{'':2} {'ID':16} {'USERID':12} {'TYPE':8} {'CONNECTED':>10} {'IDLE':>10}",
            BOLD))
        start, end = page_window(rows, cursor)
        for position in range(start, end):
            row = rows[position]
            suffix = " *" if row["current"] else ""
            line = (f"{position + 1:2}. {row['session_id']:16} {clip(row['userid'], 12):12} "
                    f"{clip(row.get('session_type', 'SSH'), 8):8} "
                    f"{elapsed(int(row['connected_seconds'])):>10} "
                    f"{elapsed(int(row['idle_seconds'])):>10}{suffix}")
            print(styled(line, REVERSE) if position == cursor else line)
        if query:
            print(styled(f"Filter: {safe_text(query)}", DIM))
        if not rows:
            print(styled("No sessions match the filter.", DIM))
        if notice:
            print("\n" + styled(notice, YELLOW))
        print("\n[↑/↓] move  [F]ilter  [T]erminate selected  [X] Exit   * current session")
        choice = key_input("Sessions> ")
        if choice in ("x", "q"):
            return
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
        elif choice == "k":
            cursor = max(0, cursor - 1)
        elif choice == "t" and rows:
            current = rows[cursor]
            session_id = str(current["session_id"])
            label = safe_text(current.get("session_type", "SSH"))
            confirm = input(
                f"Type KICK to terminate {current['userid']} {label} session: ").strip()
            if confirm != "KICK":
                notice = "Session unchanged."
                continue
            try:
                result = call("session", "admin_end", {"session_id": session_id}, token)
                notice = f"Ended the selected {result['userid']} {label} session."
            except RPCError as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "t":
            notice = "Select a session first."
        elif choice == "f":
            query = input(
                "Filter userid, type, display name, or session ID (blank shows all): "
            ).strip()
            cursor = 0


def _admin_audit(token: str) -> None:
    query = ""
    cursor = 0
    while True:
        try:
            auth_events = call("auth", "admin_audit")["events"]
            board_events = call("board", "admin_audit", token=token)["events"]
            mail_events = call("mail", "admin_mail_audit", token=token)["events"]
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        events: list[dict[str, object]] = []
        for event in auth_events:
            item = dict(event)
            item["service"] = "AUTH"
            events.append(item)
        for event in board_events:
            item = dict(event)
            item["service"] = "BOARD"
            item.setdefault("result", "success")
            events.append(item)
        for event in mail_events:
            item = dict(event)
            item["service"] = "MAIL"
            events.append(item)
        events.sort(key=lambda event: int(event["created_at"]), reverse=True)
        rows = _admin_matches(events[:400], query,
                              ("service", "actor_user", "action", "target", "detail"))
        cursor = max(0, min(cursor, len(rows) - 1)) if rows else 0
        heading("ADMINISTRATION AUDIT", "Newest actions; Enter opens full detail",
                f"{len(rows)}/{min(400, len(events))}")
        print(styled(f"{'':2} {'WHEN (UTC)':16} {'SVC':5} {'ACTOR':12} ACTION / TARGET", BOLD))
        start, end = page_window(rows, cursor)
        for position in range(start, end):
            event = rows[position]
            when = datetime.fromtimestamp(int(event["created_at"]), timezone.utc).strftime(
                "%m-%d %H:%M")
            summary = f"{event['action']} / {event['target']}"
            line = (f"{position + 1:2}. {when:16} {event['service']:5} "
                    f"{clip(event['actor_user'], 12):12} "
                    f"{clip(summary, max(12, width() - 43))}")
            print(styled(line, REVERSE) if position == cursor else line)
        if query:
            print(styled(f"Filter: {safe_text(query)}", DIM))
        if not rows:
            print(styled("No audit events match the filter.", DIM))
        print("\n[↑/↓] move  [F]ilter  [Enter] details  [X] Exit")
        choice = key_input("Log> ")
        if choice in ("x", "q"):
            return
        if choice == "j":
            cursor = min(len(rows) - 1, cursor + 1) if rows else 0
        elif choice == "k":
            cursor = max(0, cursor - 1)
        elif choice == "f":
            query = input("Filter actor, action, target, or service (blank shows all): ").strip()
            cursor = 0
        elif choice == "" and rows:
            event = rows[cursor]
            when = datetime.fromtimestamp(int(event["created_at"]), timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC")
            content = (f"Time: {when}\nService: {event['service']}\n"
                       f"Actor: {event['actor_user']}\nAction: {event['action']}\n"
                       f"Target: {event['target']}\nResult: {event.get('result', 'success')}\n"
                       f"Detail: {event.get('detail') or '(none)'}")
            _text_viewer("AUDIT EVENT", "Recorded administrative action", content)


def _admin_health(token: str) -> None:
    lines = []
    for service in ("auth", "session", "mail", "board", "file", "music", "chat",
                    "game", "door", "information", "irc"):
        try:
            result = call(service, "health")
            state = str(result.get("status", "ok")).upper()
        except (OSError, RPCError):
            state = "UNAVAILABLE"
        lines.append(f"{service.upper():12} {state}")
    lines.extend(("", "Host and container control deliberately remain outside the BBS."))
    _text_viewer("SYSTEM HEALTH", "Authority reachability from this authenticated session",
                 "\n".join(lines))


def _admin_marsamp_artwork_track(token: str, track: dict[str, object]) -> str:
    """Review local art and optionally confirm a MusicBrainz release cover."""
    track_id = int(track["id"])
    notice = ""
    while True:
        try:
            status = call("music", "admin_artwork_status", {"track_id": track_id}, token,
                          timeout=12.0)
        except (OSError, RPCError) as exc:
            return "Artwork check failed: " + safe_text(exc)
        source = safe_text(status.get("source", "marsamp"))
        source_label = {
            "confirmed": "ADMIN-CONFIRMED WEB COVER",
            "embedded": "EMBEDDED IN AUDIO FILE",
            "folder": "ALBUM-FOLDER IMAGE",
            "marsamp": "MARSAMP FALLBACK",
        }.get(source, source.upper())
        heading("MARSAMP COVER ART", "Review and confirm artwork without changing NAS files")
        print("\nTitle:  " + safe_text(track.get("title", "")))
        print("Artist: " + safe_text(track.get("artist", "")))
        print("Album:  " + safe_text(track.get("album", "")))
        print("Source: " + source_label)
        print(f"Size:   {int(status.get('bytes', 0)):,} bytes")
        if notice:
            print(styled("\n" + notice, YELLOW))
        print("\n[S] Search MusicBrainz / Cover Art Archive")
        if status.get("confirmed"):
            print("[D] Delete confirmed cover and return to local artwork")
        print("[X] Return to artwork catalog search")
        choice = key_input("Cover art> ")
        if choice in ("x", "q"):
            return notice
        if choice == "d" and status.get("confirmed"):
            answer = input("Type REMOVE to delete this album's confirmed cover: ").strip()
            if answer != "REMOVE":
                notice = "Confirmed cover unchanged."
                continue
            try:
                call("music", "admin_artwork_remove", {
                    "track_id": track_id, "confirm": "REMOVE"}, token)
                notice = "Confirmed cover removed; local embedded or folder art is active."
            except (OSError, RPCError) as exc:
                notice = "Removal rejected: " + safe_text(exc)
            continue
        if choice != "s":
            continue
        default_artist = safe_text(track.get("artist", ""))[:160]
        default_album = safe_text(track.get("album", ""))[:160]
        artist = input(f"Artist search [{default_artist}]: ").strip() or default_artist
        album = input(f"Album search [{default_album}]: ").strip() or default_album
        if not artist or not album:
            notice = "Artist and album are required for a reviewed match."
            continue
        try:
            result = call("music", "admin_artwork_search", {
                "track_id": track_id, "artist": artist, "album": album,
            }, token, timeout=12.0)
        except (OSError, RPCError) as exc:
            notice = "Search failed: " + safe_text(exc)
            continue
        candidates = list(result.get("candidates", []))
        if not candidates:
            notice = "No MusicBrainz release with confirmed front artwork matched."
            continue
        cursor = 0
        while True:
            heading("CONFIRM ALBUM COVER",
                    "Choose the exact release; nothing is imported until confirmation",
                    f"{len(candidates)} MATCHES")
            print(styled(f"{'':4}{'RELEASE GROUP':38} {'DATE':10} TYPE", BOLD))
            for position, candidate in enumerate(candidates):
                date = safe_text(candidate.get("date", ""))[:10]
                release_type = safe_text(candidate.get("type", ""))[:12]
                line = (f" {position + 1:2}. "
                        f"{clip(candidate.get('title', ''), 38):38} {date:10} {release_type}")
                print(styled(clip(line, width()), REVERSE) if position == cursor else
                      clip(line, width()))
                if position == cursor:
                    print(styled("     " + clip(candidate.get("artist", ""),
                                                  max(12, width() - 5)), DIM))
            print("\n[↑/↓] move  [Enter] review/import  [X] cancel")
            selected_choice = key_input("Release> ")
            if selected_choice in ("x", "q"):
                notice = "Web artwork search cancelled."
                break
            if selected_choice in ("j", "k", "[", "]"):
                cursor = _move_cursor(selected_choice, cursor, candidates)
                continue
            if selected_choice != "":
                continue
            selected = candidates[cursor]
            heading("CONFIRM COVER IMPORT", "The 1200px front cover will be cached locally")
            print("\nLibrary album: " + default_artist + " — " + default_album)
            print("Web release:   " + safe_text(selected.get("artist", "")) +
                  " — " + safe_text(selected.get("title", "")))
            print("Release date:  " + safe_text(selected.get("date", "")))
            print("Release type:  " + safe_text(selected.get("type", "")))
            print("MusicBrainz release-group ID: " + safe_text(selected.get("id", "")))
            answer = input("\nType IMPORT to use this cover for the library album: ").strip()
            if answer != "IMPORT":
                notice = "Cover import cancelled."
                continue
            try:
                saved = call("music", "admin_artwork_import", {
                    "track_id": track_id, "mbid": selected["id"], "confirm": "IMPORT",
                }, token, timeout=15.0)
                notice = ("Confirmed cover saved locally (" +
                          f"{int(saved.get('bytes', 0)):,} bytes).")
                break
            except (OSError, RPCError) as exc:
                notice = "Import failed: " + safe_text(exc)


def _admin_marsamp_artwork(token: str) -> None:
    """Find a library album and manage its confirmed cover artwork."""
    query = ""
    notice = ""
    while True:
        heading("MARSAMP ARTWORK LIBRARY",
                "Embedded and folder art are automatic; web covers require confirmation")
        if notice:
            print(styled("\n" + notice, YELLOW))
        value = input("\nArtist, album, or song (blank returns): ").strip()
        if not value:
            return
        query = value[:80]
        try:
            tracks = list(call("music", "search", {
                "query": query, "offset": 0, "limit": 40}, token).get("tracks", []))
        except (OSError, RPCError) as exc:
            notice = "Catalog search failed: " + safe_text(exc)
            continue
        if not tracks:
            notice = "No catalog tracks matched that search."
            continue
        cursor = 0
        while True:
            heading("SELECT ALBUM FOR ARTWORK", "Enter opens artwork review",
                    f"{len(tracks)} RESULTS")
            start, end = page_window(tracks, cursor)
            for position in range(start, end):
                track = tracks[position]
                line = (f"{position + 1:3}. " + safe_text(track.get("artist", "")) +
                        " — " + safe_text(track.get("album", "")) +
                        " — " + safe_text(track.get("title", "")))
                print(styled(clip(line, width()), REVERSE) if position == cursor else
                      clip(line, width()))
            print("\n[↑/↓] move  [Enter] review  [F] new search  [X] return")
            choice = key_input("Artwork> ")
            if choice in ("x", "q"):
                return
            if choice == "f":
                break
            if choice in ("j", "k", "[", "]"):
                cursor = _move_cursor(choice, cursor, tracks)
            elif choice == "":
                notice = _admin_marsamp_artwork_track(token, tracks[cursor])


def _admin_marsamp_metadata(token: str) -> None:
    """Review canonical display names without renaming read-only NAS files."""
    notice = ""
    while True:
        heading("MARSAMP METADATA REVIEW",
                "Canonical names are reversible; original files and paths stay unchanged")
        if notice:
            print(styled("\n" + notice, YELLOW))
        query = input("\nArtist, album, or song (blank returns): ").strip()
        if not query:
            return
        try:
            tracks = list(call("music", "search", {
                "query": query[:80], "offset": 0, "limit": 40}, token).get("tracks", []))
        except (OSError, RPCError) as exc:
            notice = "Catalog search failed: " + safe_text(exc)
            continue
        if not tracks:
            notice = "No catalog tracks matched that search."
            continue
        cursor = 0
        while True:
            heading("SELECT TRACK METADATA", "Enter compares raw and canonical names",
                    f"{len(tracks)} RESULTS")
            start, end = page_window(tracks, cursor)
            for position in range(start, end):
                track = tracks[position]
                line = (f"{position + 1:3}. " + safe_text(track.get("artist", "")) +
                        " — " + safe_text(track.get("album", "")) +
                        " — " + safe_text(track.get("title", "")))
                print(styled(clip(line, width()), REVERSE) if position == cursor else
                      clip(line, width()))
            print("\n[↑/↓] move  [Enter] inspect/edit  [F] new search  [X] return")
            choice = key_input("Metadata> ")
            if choice in ("x", "q"):
                return
            if choice == "f":
                break
            if choice in ("j", "k", "[", "]"):
                cursor = _move_cursor(choice, cursor, tracks)
                continue
            if choice != "":
                continue
            track_id = int(tracks[cursor]["id"])
            try:
                status = call("music", "admin_metadata_status", {
                    "track_id": track_id}, token)
            except (OSError, RPCError) as exc:
                notice = "Metadata check failed: " + safe_text(exc)
                continue
            raw = dict(status.get("raw", {}))
            current = dict(status.get("canonical", {}))
            heading("REVIEW TRACK NAMES", "Raw catalog values are retained for rollback")
            print("\nRaw path-derived values:")
            print("  Title:  " + safe_text(raw.get("title", "")))
            print("  Artist: " + safe_text(raw.get("artist", "")))
            print("  Album:  " + safe_text(raw.get("album", "")))
            print("\nCanonical display values:")
            print("  Title:  " + safe_text(current.get("title", "")))
            print("  Artist: " + safe_text(current.get("artist", "")))
            print("  Album:  " + safe_text(current.get("album", "")))
            print("  Source: " + safe_text(current.get("metadata_source", "path")) +
                  " / confidence " + str(int(current.get("metadata_confidence", 25))))
            action = input(
                "\nType EDIT to enter reviewed names, RESET to use tags, or blank to cancel: "
            ).strip()
            if action == "RESET":
                try:
                    reset = call("music", "admin_metadata_reset", {
                        "track_id": track_id, "confirm": "RESET"}, token)
                    tracks[cursor] = dict(reset.get("canonical", tracks[cursor]))
                    notice = "Reviewed metadata removed; current embedded tags are active."
                except (OSError, RPCError) as exc:
                    notice = "Metadata reset rejected: " + safe_text(exc)
                continue
            if action != "EDIT":
                continue
            title = input("Title [" + safe_text(current.get("title", "")) + "]: ").strip() \
                or str(current.get("title", ""))
            artist = input("Artist [" + safe_text(current.get("artist", "")) + "]: ").strip() \
                or str(current.get("artist", ""))
            album = input("Album [" + safe_text(current.get("album", "")) + "]: ").strip() \
                or str(current.get("album", ""))
            if input("Type SAVE to store this reviewed display metadata: ").strip() != "SAVE":
                notice = "Metadata unchanged."
                continue
            try:
                saved = call("music", "admin_metadata_update", {
                    "track_id": track_id, "title": title, "artist": artist,
                    "album": album, "confirm": "SAVE"}, token)
                tracks[cursor] = dict(saved.get("canonical", tracks[cursor]))
                notice = "Reviewed metadata saved; the NAS file was not changed."
            except (OSError, RPCError) as exc:
                notice = "Metadata update rejected: " + safe_text(exc)


def _admin_marsamp(token: str) -> None:
    """Manage catalog sources and the site-wide compact player layout."""
    global _mini_poll_at
    notice = ""
    cursor = 0
    while True:
        try:
            stats = call("music", "stats", token=token)
            sources = list(call("music", "admin_sources", token=token).get("sources", []))
        except (OSError, RPCError) as exc:
            heading("MARSAMP ADMINISTRATION", "Catalog status and source maintenance")
            print("\nMarsAmp is unavailable: " + safe_text(exc))
            pause()
            return
        heading("MARSAMP ADMINISTRATION", "Catalog status and source maintenance",
                "SCANNING" if stats.get("scan_running") else "READY")
        print(f"\n  Indexed tracks: {int(stats.get('tracks', 0))}")
        print(f"  Confirmed covers: {int(stats.get('artwork_overrides', 0))}")
        metadata = stats.get("metadata", {})
        if isinstance(metadata, dict):
            print(f"  Canonical names: {int(metadata.get('canonical', 0)):,} "
                  f"({int(metadata.get('fallback', 0)):,} path fallbacks)")
        layout = 4 if stats.get("compact_player_lines") == 4 else 3
        print(f"  Compact player: {layout} lines")
        last_scan = int(stats.get("last_scan", 0))
        if last_scan:
            stamp = datetime.fromtimestamp(last_scan, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC")
            print("  Last scan:      " + stamp)
        else:
            print("  Last scan:      none recorded")
        result = stats.get("last_scan_result", {})
        if isinstance(result, dict):
            errors = result.get("errors", [])
            if isinstance(errors, list) and errors:
                print(styled("  Source status:  " + "; ".join(
                    safe_text(item) for item in errors), YELLOW))
            else:
                print("  Source status:  available at last scan")
        cursor = max(0, min(cursor, len(sources) - 1)) if sources else 0
        print(styled("\n" + f"{'':5}{'SOURCE':24} {'TYPE':10} STATUS", BOLD))
        for position, source in enumerate(sources):
            kind = "BASE" if source.get("kind") == "base" else "ADDITIONAL"
            state = "AVAILABLE" if source.get("available") else "OFFLINE"
            location = (" / " + safe_text(source.get("location"))) \
                if source.get("kind") == "additional" else ""
            line = (f" {position + 1:2}. {clip(source.get('name', ''), 24):24} "
                    f"{kind:10} {state}{location}")
            print(styled(clip(line, width()), REVERSE) if position == cursor else
                  clip(line, width()))
        if notice:
            print(styled("\n" + notice, YELLOW))
        print("\n[↑/↓] move  [A] add location  [D] remove selected  [R] rescan  [X] return")
        print("[C] Cover artwork: review local art or confirm a web match")
        print("[M] Metadata names: compare raw tags and save reviewed corrections")
        print(f"[L] Compact player: switch to {7 - layout} lines")
        choice = key_input("MarsAmp admin> ")
        if choice in ("x", "q"):
            return
        if choice in ("j", "k", "[", "]"):
            cursor = _move_cursor(choice, cursor, sources)
        elif choice == "l":
            try:
                result = call("music", "admin_player_layout", {"lines": 7 - layout}, token)
                _mini_poll_at = 0.0
                notice = f"Compact player set to {result['compact_player_lines']} lines; applies on Lobby redraw."
            except (OSError, RPCError) as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "c":
            _admin_marsamp_artwork(token)
        elif choice == "m":
            _admin_marsamp_metadata(token)
        elif choice == "a":
            name = input("Short source name (lowercase, no spaces; blank cancels): ").strip()
            if not name:
                continue
            location = input(
                "Directory below the approved additional-music root (blank cancels): "
            ).strip()
            if not location:
                continue
            try:
                result = call("music", "admin_add_source", {
                    "name": name, "location": location}, token)
                notice = ("Added " + safe_text(result.get("name")) +
                          "; catalog scan started.")
                cursor = max(0, len(sources))
            except (OSError, RPCError) as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "d" and sources:
            selected = sources[cursor]
            name = str(selected.get("name", ""))
            if selected.get("kind") == "base":
                notice = "NAS01 and UNR01 are protected base sources."
                continue
            if input(f"Type REMOVE {name} to remove this catalog source: ").strip() \
                    != f"REMOVE {name}":
                notice = "Source unchanged."
                continue
            try:
                call("music", "admin_remove_source", {"name": name}, token)
                notice = f"Removed {name}. Files at the read-only location were untouched."
                cursor = max(0, cursor - 1)
            except (OSError, RPCError) as exc:
                notice = "Request rejected: " + safe_text(exc)
        elif choice == "r":
            try:
                response = call("music", "rescan", token=token)
                notice = ("Catalog scan started." if response.get("started") else
                          safe_text(response.get("message", "Scan is already running.")))
            except (OSError, RPCError) as exc:
                notice = "Request rejected: " + safe_text(exc)


def _show_link_invitation(invitation: object, title: str) -> None:
    heading(title, "Administrator-only pairing material; transfer it through a secure channel")
    print("\nThis code contains the shared link credential and is shown only now.\n")
    print(safe_text(invitation))
    print("\nThe other BBS imports this code, then returns a reciprocal invitation.")
    pause()


def _admin_peer_list(token: str) -> None:
    cursor = 0
    notice = ""
    while True:
        try:
            peers = list(call("mail", "admin_peers", token=token)["peers"])
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        cursor = max(0, min(cursor, len(peers) - 1)) if peers else 0
        heading("TRUSTED BBS LINKS", "Endpoints and credentials are visible only to administrators",
                f"{len(peers)} LINKED")
        print(styled(f"{'':2} {'BBS ID':20} {'STATE':8} {'QUEUE':5} NAME", BOLD))
        start, end = page_window(peers, cursor)
        for position in range(start, end):
            peer = peers[position]
            state = "ENABLED" if peer["enabled"] else "DISABLED"
            queue = int(peer.get("queued", 0))
            line = (f"{position + 1:2}. {clip(peer['bbs_id'], 20):20} {state:8} "
                    f"{queue:5} {clip(peer['bbs_name'], max(8, width() - 43))}")
            print(styled(line, REVERSE) if position == cursor else line)
        if notice:
            print(styled("\n" + notice, YELLOW))
        if not peers:
            print("\nNo BBS links are configured.")
        print("\n[↑/↓] move  [Enter] details  [E] enable/disable  [R] reciprocal code")
        print("[D] unlink  [X] return")
        choice = key_input("Links> ")
        if choice in ("x", "q"):
            return
        if choice in ("j", "k", "[", "]"):
            cursor = _move_cursor(choice, cursor, peers)
            continue
        if not peers:
            notice = "Import a peer invitation from the Federation menu first."
            continue
        selected = peers[cursor]
        peer_id = str(selected["bbs_id"])
        try:
            if choice == "":
                _text_viewer(
                    "BBS LINK DETAILS", "Administrator-only routing information",
                    f"Name: {selected['bbs_name']}\nNetwork ID: {peer_id}\n"
                    f"State: {'enabled' if selected['enabled'] else 'disabled'}\n"
                    f"Endpoint: {selected['endpoint']}\n"
                    f"TLS SHA-256: {selected['certificate_sha256']}\n"
                    f"Queued: {int(selected.get('queued', 0))}\n"
                    f"Failed: {int(selected.get('failed', 0))}\n\n"
                    "The shared credential is never returned by the service.")
            elif choice == "e":
                enabled = not bool(selected["enabled"])
                call("mail", "admin_peer_enabled", {
                    "bbs_id": peer_id, "enabled": enabled}, token)
                notice = f"{peer_id} is now {'enabled' if enabled else 'disabled'}."
            elif choice == "r":
                result = call("mail", "admin_peer_invitation", {"bbs_id": peer_id}, token)
                _show_link_invitation(result["invitation"], "RECIPROCAL LINK INVITATION")
            elif choice == "d":
                if input(f"Type UNLINK {peer_id} to remove this trust relationship: ").strip() \
                        == f"UNLINK {peer_id}":
                    call("mail", "admin_peer_delete", {"bbs_id": peer_id}, token)
                    notice = f"Unlinked {peer_id}."
                    cursor = max(0, cursor - 1)
                else:
                    notice = "Link unchanged."
        except RPCError as exc:
            notice = "Request rejected: " + safe_text(exc)


def _admin_federation(token: str) -> None:
    notice = ""
    while True:
        try:
            site = call("mail", "admin_site", token=token)
            peers = list(call("mail", "admin_peers", token=token)["peers"])
        except RPCError as exc:
            show_error(exc)
            pause()
            return
        heading("BBS IDENTITY & MAIL EXCHANGE",
                "Trusted, text-only private mail between linked BBS installations",
                "READY" if site.get("ready") else "LOCAL ONLY")
        print(f"\n  BBS name:        {safe_text(site['bbs_name'])}")
        print(f"  Network ID:      {safe_text(site['bbs_id'])}")
        print(f"  Public endpoint: "
              f"{safe_text(site['public_endpoint']) if site['public_endpoint'] else '(not set)'}")
        certificate = str(site.get("certificate_sha256", ""))
        print(f"  TLS fingerprint: {certificate if certificate else 'unavailable'}")
        print(f"  Trusted peers:   {len(peers)}")
        print("\nOrdinary users see peer names and addresses only. Server addresses, TLS")
        print("fingerprints, and shared credentials remain in this administrator console.")
        if notice:
            print(styled("\n" + notice, YELLOW))
        print("\n[N] Name and identity   [C] Create first invitation")
        print("[I] Import invitation   [P] Trusted peers   [X] Return")
        choice = key_input("Federation> ")
        if choice in ("x", "q"):
            return
        try:
            if choice == "n":
                name = input(f"BBS display name [{site['bbs_name']}]: ").strip() \
                    or str(site["bbs_name"])
                network_id = input(f"Stable network ID [{site['bbs_id']}]: ").strip() \
                    or str(site["bbs_id"])
                public_endpoint = input(
                    f"Public HTTPS exchange origin [{site['public_endpoint'] or 'none'}] "
                    "('-' clears): ").strip()
                if public_endpoint == "-":
                    public_endpoint = ""
                elif not public_endpoint:
                    public_endpoint = str(site["public_endpoint"])
                result = call("mail", "admin_site_update", {
                    "bbs_name": name, "bbs_id": network_id,
                    "public_endpoint": public_endpoint}, token)
                notice = (f"BBS identity saved as {result['bbs_name']} "
                          f"({result['bbs_id']}).")
            elif choice == "c":
                result = call("mail", "admin_create_invitation", token=token)
                _show_link_invitation(result["invitation"], "NEW BBS LINK INVITATION")
            elif choice == "i":
                invitation = input("Paste MHX1 invitation (blank cancels): ").strip()
                if not invitation:
                    continue
                try:
                    result = call("mail", "admin_import_invitation", {
                        "invitation": invitation}, token)
                except RPCError as exc:
                    if "peer already exists" not in str(exc):
                        raise
                    if input("Peer exists. Type ROTATE to replace its credential: ").strip() \
                            != "ROTATE":
                        notice = "Existing peer was not changed."
                        continue
                    result = call("mail", "admin_import_invitation", {
                        "invitation": invitation, "replace": True}, token)
                notice = (f"Linked {result['bbs_name']} as @{result['bbs_id']}. "
                          "Send its administrator a reciprocal invitation.")
            elif choice == "p":
                _admin_peer_list(token)
        except RPCError as exc:
            notice = "Request rejected: " + safe_text(exc)


def administration(token: str, identity: dict[str, object]) -> None:
    """Capability-filtered historical administration console."""
    try:
        _admin_identity(identity)
    except RPCError as exc:
        show_error(exc)
        pause()
        return
    if not ADMIN_CAPABILITIES.intersection(identity.get("capabilities", [])):
        show_error(RPCError("administrative capability required"))
        pause()
        return
    while True:
        roles = ", ".join(identity.get("roles", []))
        heading("BBS ADMINISTRATION", "Historically named roles with V2 service enforcement",
                clip(roles, 30))
        if _can(identity, "account.view"):
            print("[A]ccounts     Status and historical role assignments")
        if _can(identity, "board.manage"):
            print("[B]oards       Create, configure, and archive boards")
        if _can(identity, "session.manage"):
            print("[S]essions     Inspect and terminate authenticated sessions")
        if _can(identity, "audit.read"):
            print("[L]og          Review administrative audit events")
        if _can(identity, "system.health"):
            print("[H]ealth       Check application authority availability")
        if _can(identity, "federation.manage"):
            print("[F]ederation   Name this BBS and manage trusted mail links")
        if _can(identity, "music.manage"):
            print("[M]arsAmp      Review and rescan the music catalog")
        print("[R]oles        Explain historical and modern roles")
        print("[X] Exit        Return to the Lobby")
        choice = key_input("Admin> ")
        if choice in ("x", "q"):
            return
        if choice == "a" and _can(identity, "account.view"):
            _admin_accounts(token, identity)
            _admin_identity(identity)
        elif choice == "b" and _can(identity, "board.manage"):
            _admin_boards(token)
        elif choice == "s" and _can(identity, "session.manage"):
            _admin_sessions(token)
        elif choice == "l" and _can(identity, "audit.read"):
            _admin_audit(token)
        elif choice == "h" and _can(identity, "system.health"):
            _admin_health(token)
        elif choice == "f" and _can(identity, "federation.manage"):
            _admin_federation(token)
        elif choice == "m" and _can(identity, "music.manage"):
            _admin_marsamp(token)
        elif choice == "r":
            definitions = call("auth", "admin_roles")["roles"]
            content = []
            for role in definitions:
                caps = ", ".join(role["capabilities"]) or "ordinary member access"
                content.extend((role["name"], "  " + str(role["description"]),
                                "  Capabilities: " + caps, ""))
            _text_viewer("ADMINISTRATIVE ROLES",
                         "Only roles with an implemented, server-enforced effect are listed",
                         "\n".join(content))


def _info_stamp(cache: dict[str, object] | None) -> str:
    if not cache:
        return "UNAVAILABLE"
    try:
        label = datetime.fromtimestamp(int(cache["fetched_at"]), timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    except (KeyError, TypeError, ValueError, OSError):
        label = "timestamp unavailable"
    return ("STALE - " if cache.get("stale") else "UPDATED: ") + label


def _fmt_number(value: object, suffix: str = "") -> str:
    if value is None:
        return "UNKNOWN"
    try:
        number = float(value)
        rendered = f"{number:.1f}" if not number.is_integer() else str(int(number))
        return rendered + suffix
    except (TypeError, ValueError):
        return safe_text(value) + suffix


def _wait_for_information(op: str, data: dict[str, object], token: str,
                          attempts: int = 30) -> dict[str, object]:
    last: RPCError | None = None
    for _ in range(attempts):
        try:
            return call("information", op, data, token)
        except RPCError as exc:
            last = exc
            if "pending" not in str(exc):
                raise
            time.sleep(1)
    raise last or RPCError("data refresh timed out")


def _choose_information_location(token: str) -> bool:
    heading("MARS LOCATION DIRECTORY", "City, state/province, country, or postal code")
    try:
        if _active_pager is not None:
            query = _active_pager.read_line(
                "Location search (Esc cancels): ", cancellable=True).strip()
        else:
            query = input("Location search (blank cancels): ").strip()
    except (EOFError, KeyboardInterrupt, InputCancelled):
        return False
    if not query:
        return False
    response: dict[str, object] = {}
    for _ in range(10):
        response = call("information", "location_search", {"query": query}, token)
        if response.get("results"):
            break
        if not response.get("pending"):
            break
        print("\rSearching the location directory...", end="", flush=True)
        time.sleep(1)
    rows = list(response.get("results", []))
    if not rows:
        print("\nNo matching locations were found.")
        pause()
        return False
    cursor = 0
    while True:
        heading("MARS LOCATION DIRECTORY", f"Search: {safe_text(query)}")
        start, end = page_window(rows, cursor)
        for index in range(start, end):
            marker = ">" if index == cursor else " "
            print(f"{marker} {index + 1:2}. {clip(rows[index].get('name', ''), width() - 8)}")
        print("\n[↑/↓] move  [Enter] select  [X] cancel")
        choice = key_input("Location> ")
        if choice in ("q", "e", "x", "\x1b"):
            return False
        if choice in ("j", "k", "[", "]"):
            cursor = _move_cursor(choice, cursor, rows)
        elif choice in ("\r", "\n", ""):
            call("information", "location_set", {"query": query, "index": cursor}, token)
            return True
        elif choice.isdigit():
            selected = int(choice) - 1
            if 0 <= selected < len(rows):
                call("information", "location_set", {"query": query, "index": selected}, token)
                return True


def _weather_report(token: str) -> None:
    while True:
        try:
            response = _wait_for_information("weather", {}, token)
        except RPCError as exc:
            heading("MARS WEATHER CENTER")
            print(f"Weather data: {safe_text(exc)}")
            print("\n[C] choose location  [X] return")
            choice = key_input("Weather> ")
            if choice == "c":
                _choose_information_location(token)
                continue
            return
        if not response.get("location"):
            if not _choose_information_location(token):
                return
            continue
        cache = response.get("cache")
        if not cache:
            return
        report = dict(cache["data"])
        current = dict(report.get("current", {}))
        heading("MARS WEATHER CENTER", str(report.get("location", "Selected location")))
        print(_info_stamp(cache))
        print(f"SOURCE: {safe_text(cache.get('source', 'Open-Meteo; NOAA/NWS'))}")
        print(styled("\nCURRENT CONDITIONS", BOLD, YELLOW))
        print(f"  Temperature : {_fmt_number(current.get('temperature'), ' F')}")
        print(f"  Feels Like  : {_fmt_number(current.get('feels_like'), ' F')}")
        print(f"  Humidity    : {_fmt_number(current.get('humidity'), ' %')}")
        print(f"  Wind        : {safe_text(current.get('wind_direction', '?'))} "
              f"{_fmt_number(current.get('wind_speed'), ' mph')}")
        print(f"  Conditions  : {safe_text(current.get('conditions', 'Unknown'))}")
        print(styled("\nFORECAST", BOLD, YELLOW))
        for day in list(report.get("daily", []))[:3]:
            print(f"  {str(day.get('time', '')):10}  {clip(day.get('conditions', ''), 24):24} "
                  f"{_fmt_number(day.get('high'), 'F'):>6} / {_fmt_number(day.get('low'), 'F'):>6}")
        alert_count = len(list(report.get("alerts", [])))
        print(f"\n[1] Hourly  [2] 7-Day  [3] Alerts ({alert_count})  "
              "[4] Change Location  [X] Return")
        choice = key_input("Weather> ")
        if choice in ("x", "q", "e"):
            return
        if choice == "4":
            _choose_information_location(token)
        elif choice == "1":
            lines = [f"{item.get('time', '')}  {_fmt_number(item.get('temperature'), ' F'):>7}  "
                     f"rain {_fmt_number(item.get('precipitation'), '%'):>5}  "
                     f"{safe_text(item.get('conditions', ''))}"
                     for item in list(report.get("hourly", []))]
            _text_viewer("MARS WEATHER - HOURLY", _info_stamp(cache), "\n".join(lines))
        elif choice == "2":
            lines = [f"{item.get('time', '')}  {safe_text(item.get('conditions', ''))}\n"
                     f"    High {_fmt_number(item.get('high'), ' F')}  "
                     f"Low {_fmt_number(item.get('low'), ' F')}  "
                     f"Precipitation {_fmt_number(item.get('precipitation'), '%')}"
                     for item in list(report.get("daily", []))]
            _text_viewer("MARS WEATHER - 7-DAY", _info_stamp(cache), "\n\n".join(lines))
        elif choice == "3":
            alerts = list(report.get("alerts", []))
            body = "No active alerts for this location." if not alerts else "\n\n".join(
                f"{item.get('event', 'Weather alert')} [{item.get('severity', 'Unknown')}]\n"
                f"{item.get('headline', '')}\n{item.get('description', '')}\n"
                f"{item.get('instruction', '')}" for item in alerts)
            _text_viewer("MARS WEATHER ALERTS", "Source: NOAA/National Weather Service", body)


def _news_wire(token: str, category: str, title: str) -> None:
    try:
        response = _wait_for_information("news", {"category": category}, token)
    except RPCError as exc:
        heading(title)
        print(f"News wire unavailable: {safe_text(exc)}")
        pause()
        return
    cache = dict(response["cache"])
    rows = list(cache.get("data", []))
    cursor = 0
    while True:
        heading(title, _info_stamp(cache))
        print(f"SOURCE FEEDS: {safe_text(cache.get('source', ''))}")
        start, end = page_window(rows, cursor)
        for index in range(start, end):
            marker = ">" if index == cursor else " "
            item_type = str(rows[index].get("type", "SUMMARY"))
            badge = "FULL" if item_type == "FULL TEXT" else (
                "PART" if item_type == "EXCERPT" else "SUM")
            print(f"{marker} {index + 1:02}. [{badge}] "
                  f"{clip(rows[index].get('title', ''), width() - 15)}")
        print("\n[↑/↓] move  [Enter] read  [X] return")
        choice = key_input("Wire> ")
        if choice in ("x", "q", "e"):
            return
        if choice in ("j", "k", "[", "]"):
            cursor = _move_cursor(choice, cursor, rows)
        elif choice in ("", "\r", "\n") and rows:
            item = rows[cursor]
            body = str(item.get("summary", "No summary supplied."))
            item_type = str(item.get("type", "SUMMARY"))
            article_id = str(item.get("article_id", ""))
            if article_id and item_type in ("FULL TEXT", "EXCERPT"):
                pieces: list[str] = []
                offset = 0
                try:
                    for _ in range(32):
                        article = _wait_for_information(
                            "article", {"category": category,
                                        "article_id": article_id, "offset": offset},
                            token)
                        pieces.append(str(article.get("body", "")))
                        next_offset = int(article.get("next_offset", offset))
                        if article.get("complete"):
                            break
                        if next_offset <= offset:
                            raise RPCError("article transfer did not advance")
                        offset = next_offset
                    else:
                        raise RPCError("article exceeds the reader transfer limit")
                    body = "".join(pieces)
                    item_type = str(article.get("type", item_type))
                except (RPCError, TypeError, ValueError) as exc:
                    body = (body + "\n\n[Full text is temporarily unavailable: "
                            + safe_text(exc) + "]")
                    item_type = "SUMMARY"
            subtitle = (f"SOURCE: {item.get('source', '')}  TYPE: {item_type}\n"
                        f"PUBLISHED: {datetime.fromtimestamp(int(item.get('published', 0)), timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if item.get('published') else 'unknown'}")
            if item.get("author"):
                subtitle += f"\nBY: {safe_text(item.get('author', ''))}"
            if item.get("rights"):
                subtitle += f"  RIGHTS: {safe_text(item.get('rights', ''))}"
            _text_viewer(str(item.get("title", "NEWS ITEM")), subtitle,
                         body)


def _entertainment_list(token: str, kind: str, title: str,
                        tv_kind: str = "") -> None:
    try:
        response = _wait_for_information(
            "entertainment", {"kind": kind}, token, attempts=30)
    except RPCError as exc:
        heading(title)
        print(f"Release calendar unavailable: {safe_text(exc)}")
        pause()
        return
    cache = dict(response["cache"])
    payload = dict(cache.get("data", {}))
    raw_rows = payload.get("items", [])
    rows = ([dict(row) for row in raw_rows if isinstance(row, dict)]
            if isinstance(raw_rows, list) else [])
    if tv_kind:
        rows = [row for row in rows if str(row.get("kind", "")) == tv_kind]
    cursor = 0
    while True:
        heading(title, _info_stamp(cache))
        print(f"SOURCE: {safe_text(cache.get('source', ''))}")
        if payload.get("window_end"):
            print(f"CALENDAR THROUGH: {safe_text(payload.get('window_end', ''))}")
        if not rows:
            print("\n  No announced releases are listed in this window.")
            print("\n[X] return")
            if key_input("Releases> ") in ("x", "q", "e", "", "\r", "\n"):
                return
            continue
        start, end = page_window(rows, cursor)
        for index in range(start, end):
            marker = ">" if index == cursor else " "
            item = rows[index]
            if kind == "movies":
                label = f"{item.get('date', '')}  {item.get('title', '')}"
            else:
                badge = "SERIES" if item.get("kind") == "NEW SERIES" else "SEASON"
                label = (f"{item.get('date', '')} [{badge}] "
                         f"{item.get('show', '')}")
            print(f"{marker} {index + 1:02}. {clip(label, width() - 7)}")
        print("\n[↑/↓] move  [Enter] details  [X] return")
        choice = key_input("Releases> ")
        if choice in ("x", "q", "e"):
            return
        if choice in ("j", "k", "[", "]"):
            cursor = _move_cursor(choice, cursor, rows)
        elif choice in ("", "\r", "\n"):
            item = rows[cursor]
            if kind == "movies":
                subtitle = (f"RELEASE: {item.get('date', '')}\n"
                            f"COMPANY: {safe_text(item.get('company', '') or 'Not listed')}")
                body = (safe_text(item.get("details", "")) or
                        "No cast or production details were listed.")
                release_year = str(item.get("date", ""))[:4]
                body += ("\n\nSource: Wikipedia contributors, List of American films.\n"
                         "License: Creative Commons Attribution-ShareAlike 4.0.\n"
                         f"https://en.wikipedia.org/wiki/List_of_American_films_of_{release_year}")
                revisions = payload.get("revisions", [])
                if isinstance(revisions, list) and revisions:
                    body += "\nSource revision: " + ", ".join(
                        safe_text(revision) for revision in revisions)
                item_title = str(item.get("title", "MOVIE RELEASE"))
            else:
                time_label = str(item.get("time", "")) or "time TBA"
                subtitle = (f"{item.get('kind', '')}  {item.get('date', '')} {time_label}\n"
                            f"NETWORK: {safe_text(item.get('network', 'TBA'))}  "
                            f"SEASON: {item.get('season', '')}")
                body = (f"PREMIERE EPISODE: {safe_text(item.get('episode', 'Episode 1'))}\n"
                        f"GENRES: {safe_text(item.get('genres', '') or 'Not listed')}\n\n"
                        f"{safe_text(item.get('summary', '') or 'No series summary supplied.')}\n\n"
                        f"TVmaze: {safe_text(item.get('url', ''))}\n"
                        "Source: TVmaze public API (CC BY-SA).")
                item_title = str(item.get("show", "TV PREMIERE"))
            _text_viewer(item_title, subtitle, body)


def _entertainment_center(token: str) -> None:
    while True:
        heading("MARS SCREENING ROOM", "New movies and television premieres")
        print("\n  [1] NEW & UPCOMING MOVIES")
        print("  [2] NEW TELEVISION SERIES")
        print("  [3] RETURNING TELEVISION SEASONS")
        print("\n  [X] RETURN TO INFORMATION NETWORK")
        choice = key_input("\nSCREENING ROOM> ")
        if choice in ("x", "q", "e"):
            return
        if choice == "1":
            _entertainment_list(token, "movies", "MARS MOVIE RELEASES")
        elif choice == "2":
            _entertainment_list(token, "television", "NEW TELEVISION SERIES",
                                "NEW SERIES")
        elif choice == "3":
            _entertainment_list(token, "television", "RETURNING TELEVISION SEASONS",
                                "NEW SEASON")


def _solar_center(token: str) -> None:
    while True:
        try:
            response = _wait_for_information("solar", {}, token)
            cache = dict(response["cache"])
            data = dict(cache["data"])
            outlook = (dict(response["outlook"])
                       if isinstance(response.get("outlook"), dict) else None)
        except RPCError as exc:
            heading("MARS AMATEUR RADIO CENTER")
            print(f"Space weather unavailable: {safe_text(exc)}")
            pause()
            return
        narrow = width() < 60
        heading("MARS AMATEUR RADIO CENTER", "NOAA observations + local HF estimate")
        print(_info_stamp(cache))
        if narrow:
            print(f"OBSERVED: SFI {_fmt_number(data.get('flux'))}  "
                  f"Kp {_fmt_number(data.get('kp'))}")
            print(f"NOAA: G {safe_text(data.get('geomagnetic', 'unknown'))}  "
                  f"R {safe_text(data.get('radio_blackout', 'unknown'))}  "
                  f"S {safe_text(data.get('solar_radiation', 'unknown'))}")
        else:
            print(f"OBSERVED: Solar Flux Index {_fmt_number(data.get('flux'))}  "
                  f"Planetary Kp {_fmt_number(data.get('kp'))}")
            print(f"NOAA SCALES: G {safe_text(data.get('geomagnetic', 'unknown'))}  "
                  f"R {safe_text(data.get('radio_blackout', 'unknown'))}  "
                  f"S {safe_text(data.get('solar_radiation', 'unknown'))}")
        if outlook:
            if narrow:
                print(f"LOCATION: {clip(outlook.get('location_name', ''), max(8, width() - 10))}")
                print(f"{safe_text(outlook.get('period', ''))}  "
                      f"SUN {float(outlook.get('solar_elevation', 0.0)):+.1f} deg")
            else:
                print(f"LOCATION: {clip(outlook.get('location_name', ''), 39)}  "
                      f"{safe_text(outlook.get('period', ''))}  "
                      f"SUN {float(outlook.get('solar_elevation', 0.0)):+.1f} deg")
            print(styled("\nLOCAL HF OUTLOOK (ESTIMATE)", BOLD, YELLOW))
            rows = list(outlook.get("rows", []))
            if narrow:
                print(" BAND    NOW    DAY    NIGHT")
                for row in rows:
                    print(f" {str(row.get('band', '')):>4}   "
                          f"{str(row.get('now', '')):<5}  "
                          f"{str(row.get('day', '')):<5}  "
                          f"{str(row.get('night', '')):<5}")
            else:
                print("  BAND    NOW     DAY    NIGHT       BAND    NOW     DAY    NIGHT")
                for left_index in range(5):
                    fields = []
                    for index in (left_index, left_index + 5):
                        if index < len(rows):
                            row = rows[index]
                            fields.append(f"{str(row.get('band', '')):>5}  "
                                          f"{str(row.get('now', '')):<5}  "
                                          f"{str(row.get('day', '')):<5}  "
                                          f"{str(row.get('night', '')):<5}")
                    print("  " + "       ".join(fields))
            print(textwrap.fill(
                "LOW-CONFIDENCE operating guide; not a point-to-point path forecast.",
                width=width()))
        else:
            print(styled("\nLOCAL HF OUTLOOK", BOLD, YELLOW))
            print("Set your ZIP/city location to add daylight-aware band estimates.")
        if narrow:
            print("\n[1] Alerts  [2] Details  [3] Method")
            print("[L] Location  [X] Return")
        else:
            print("\n[1] NOAA alerts  [2] Details  [3] Method  [L] Location  [X] Return")
        choice = key_input("Radio> ")
        if choice in ("x", "q", "e"):
            return
        if choice == "1":
            alerts = list(data.get("alerts", []))
            body = "No recent NOAA alerts." if not alerts else "\n\n".join(
                f"ISSUED: {item.get('issued', '')}\n{item.get('message', '')}" for item in alerts)
            _text_viewer("NOAA SPACE WEATHER ALERTS", _info_stamp(cache), body)
        elif choice == "2":
            forecast_kp = _fmt_number(data.get("forecast_kp_24h"))
            forecast_flux = _fmt_number(data.get("forecast_flux"))
            body = (f"OBSERVED SOLAR FLUX: {_fmt_number(data.get('flux'))}\n"
                    f"OBSERVED PLANETARY Kp: {_fmt_number(data.get('kp'))}\n"
                    f"FORECAST F10.7: {forecast_flux}\n"
                    f"HIGHEST FORECAST Kp (NEXT 24H): {forecast_kp}\n"
                    f"GEOMAGNETIC: {safe_text(data.get('geomagnetic', 'unknown'))}\n"
                    f"RADIO BLACKOUT: {safe_text(data.get('radio_blackout', 'unknown'))}\n"
                    f"SOLAR RADIATION: {safe_text(data.get('solar_radiation', 'unknown'))}\n\n"
                    f"SOURCE: {safe_text(cache.get('source', 'NOAA SWPC'))}\n"
                    f"{_info_stamp(cache)}")
            _text_viewer("HF CONDITIONS DETAIL", "Observed and forecast NOAA SWPC data", body)
        elif choice == "3":
            cautions = list(outlook.get("cautions", [])) if outlook else []
            _text_viewer(
                "HF OUTLOOK METHOD", "A local operating estimate, not circuit prediction",
                "Your Weather Center ZIP/city supplies latitude, longitude, and local time. "
                "Mars combines local sunlight with NOAA-observed solar flux, planetary Kp, "
                "NOAA radio-blackout and solar-radiation scales, plus the next 24-hour Kp "
                "and F10.7 outlook. It then grades each amateur HF band GOOD, FAIR, or POOR.\n\n"
                "This is a transparent, conservative heuristic. It does not calculate MUF, "
                "signal strength, contact probability, or a specific circuit. A true ITU-R "
                "P.533/VOACAP-style prediction also needs a destination, month, time, power, "
                "mode, antennas, and noise assumptions.\n\n" + "\n".join(cautions))
        elif choice == "l":
            _choose_information_location(token)


def _earthquake_center(token: str) -> None:
    try:
        cache = dict(_wait_for_information("earthquakes", {}, token)["cache"])
    except RPCError as exc:
        heading("MARS EARTHQUAKE MONITOR")
        print(f"Earthquake feed unavailable: {safe_text(exc)}")
        pause()
        return
    groups = dict(cache["data"])
    selection = "significant"
    cursor = 0
    while True:
        rows = list(groups.get(selection, []))
        heading("MARS EARTHQUAKE MONITOR", f"{selection.upper()}  /  {_info_stamp(cache)}")
        print(f"SOURCE: {safe_text(cache.get('source', 'U.S. Geological Survey'))}")
        start, end = page_window(rows, cursor)
        for index in range(start, end):
            item = rows[index]
            marker = ">" if index == cursor else " "
            print(f"{marker} {index + 1:02}. M{_fmt_number(item.get('magnitude')):>4}  "
                  f"{clip(item.get('place', ''), width() - 17)}")
        print("\n[↑/↓] move  [Enter] details  [1] Significant  [2] 24h  [3] 7d  [X] Return")
        choice = key_input("Quakes> ")
        if choice in ("x", "q", "e"):
            return
        if choice == "1": selection, cursor = "significant", 0
        elif choice == "2": selection, cursor = "day", 0
        elif choice == "3": selection, cursor = "week", 0
        elif choice in ("j", "k", "[", "]"):
            cursor = _move_cursor(choice, cursor, rows)
        elif choice in ("", "\r", "\n") and rows:
            item = rows[cursor]
            when = datetime.fromtimestamp(int(item.get("time", 0)), timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC")
            _text_viewer("EARTHQUAKE DETAIL", "Source: U.S. Geological Survey",
                         f"Magnitude: {_fmt_number(item.get('magnitude'))}\n"
                         f"Location: {item.get('place', '')}\nTime: {when}\n"
                         f"Depth: {_fmt_number(item.get('depth_km'), ' km')}\n"
                         f"Coordinates: {item.get('latitude')}, {item.get('longitude')}\n"
                         f"USGS event: {item.get('url', '')}")


def _iss_center(token: str) -> None:
    try:
        response = _wait_for_information("iss", {}, token)
    except RPCError as exc:
        heading("MARS ISS TRACKING")
        print(f"ISS data unavailable: {safe_text(exc)}")
        pause()
        return
    cache = dict(response["cache"])
    position = dict(response.get("position", {}))
    heading("MARS ISS & SATELLITE TRACKING", "Calculated from CelesTrak orbital elements")
    print(_info_stamp(cache))
    if position:
        print(f"\n  Latitude       : {_fmt_number(position.get('latitude'), ' deg')}")
        print(f"  Longitude      : {_fmt_number(position.get('longitude'), ' deg')}")
        print(f"  Altitude       : {_fmt_number(position.get('altitude_km'), ' km')}")
        print(f"  Orbital speed  : {_fmt_number(position.get('velocity_km_s'), ' km/s')}")
        print(f"  Element epoch  : {safe_text(position.get('element_epoch', ''))}")
    else:
        print("\nPosition calculation unavailable: " +
              safe_text(response.get("calculation_error", "unknown error")))
    passes = list(response.get("passes", []))
    if response.get("location"):
        print(f"\nUPCOMING PASSES FOR {safe_text(response['location'].get('location_name', ''))}")
        for item in passes[:5]:
            start = datetime.fromtimestamp(int(item["start"]), timezone.utc).strftime("%b %d %H:%M")
            end = datetime.fromtimestamp(int(item["end"]), timezone.utc).strftime("%H:%M UTC")
            print(f"  {start}-{end}  max {item['maximum_elevation']} deg  "
                  f"az {item['start_azimuth']}->{item['end_azimuth']}")
        if not passes:
            print("  No passes above 10 degrees found in the next 48 hours.")
    else:
        print("\nChoose a Weather Center location to calculate local passes.")
    pause()


def _market_value(value: object) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "UNKNOWN"


def _market_detail(cache: dict[str, object]) -> None:
    data = dict(cache.get("data", {}))
    heading("MARKET QUOTE", safe_text(data.get("symbol", "")))
    if not data.get("available", True):
        print("\n  No end-of-day price was available for that symbol.")
        print("  " + safe_text(data.get("error", "Check the ticker and try again.")))
        pause()
        return
    print(f"\n  DATE:       {safe_text(data.get('market_date', 'UNKNOWN'))}")
    print(f"  TYPE:       {safe_text(data.get('kind', 'STOCK OR ETF'))}")
    print(f"  OPEN:       {_market_value(data.get('open'))}")
    print(f"  HIGH:       {_market_value(data.get('high'))}")
    print(f"  LOW:        {_market_value(data.get('low'))}")
    print(f"  CLOSE:      {_market_value(data.get('close'))}")
    change = float(data.get("change", 0.0))
    percent = float(data.get("percent", 0.0))
    print(f"  CHANGE:     {change:+,.2f} ({percent:+.2f}%)")
    volume = int(data.get("volume", 0) or 0)
    if volume:
        print(f"  VOLUME:     {volume:,}")
    print(f"\n  {_info_stamp(cache)}")
    print(f"  SOURCE: {safe_text(cache.get('source', 'Massive'))}")
    print("  END-OF-DAY DATA - NOT A REAL-TIME QUOTE")
    pause()


def _market_center(token: str) -> None:
    while True:
        try:
            response = _wait_for_information("markets", {}, token, attempts=75)
        except RPCError as exc:
            heading("MARS FINANCIAL MARKETS")
            print("\nMarket data is temporarily unavailable.\n")
            print(safe_text(exc))
            pause()
            return
        heading("MARS FINANCIAL MARKETS", "End-of-day market board")
        if not response.get("available"):
            print("\n" + safe_text(response.get("reason", "Market data is unavailable.")))
            pause()
            return
        cache = dict(response["cache"])
        data = dict(cache.get("data", {}))
        print(f"\n  {_info_stamp(cache)}")
        print(f"  SOURCE: {safe_text(cache.get('source', 'Massive'))}")
        print(f"  DELAY:  {safe_text(data.get('delay', 'END OF DAY'))}\n")
        if any(dict(row).get("kind") == "UNOFFICIAL"
               for row in list(data.get("benchmarks", []))):
            print("  !! UNOFFICIAL INDEX LEVELS - MAY BE DELAYED OR INACCURATE !!\n")
        print("  SYMBOL       CLOSE        CHANGE       DATE       TYPE")
        print("  " + "-" * 68)
        for row in list(data.get("benchmarks", []))[:10]:
            item = dict(row)
            symbol = safe_text(item.get("symbol", ""))[:10]
            close = _market_value(item.get("close"))
            change = float(item.get("change", 0.0))
            percent = float(item.get("percent", 0.0))
            date = safe_text(item.get("market_date", ""))[:10]
            kind = safe_text(item.get("kind", ""))[:10]
            print(f"  {symbol:<10} {close:>11}  {change:+8.2f} {percent:+6.2f}%  "
                  f"{date:<10} {kind}")
            print(f"               {safe_text(item.get('label', ''))[:58]}")
        print("\n  " + safe_text(data.get("notice", "")))
        print("  Prices are informational and are not investment advice.")
        print("\n  [L] LOOK UP A STOCK OR ETF SYMBOL    [X] RETURN")
        choice = key_input("\nMarkets> ")
        if choice in ("x", "q", "e"):
            return
        if choice != "l":
            continue
        try:
            if _active_pager is not None:
                symbol = _active_pager.read_line(
                    "Ticker symbol (Esc cancels): ", cancellable=True).strip().upper()
            else:
                symbol = input("Ticker symbol (blank cancels): ").strip().upper()
        except (EOFError, KeyboardInterrupt, InputCancelled):
            continue
        if not symbol:
            continue
        try:
            quote = _wait_for_information(
                "market_quote", {"symbol": symbol}, token, attempts=45)
            _market_detail(dict(quote["cache"]))
        except RPCError as exc:
            heading("MARKET LOOKUP")
            print("\nUnable to retrieve that symbol: " + safe_text(exc))
            pause()


def information_network(token: str) -> None:
    while True:
        heading("MARS INFORMATION NETWORK", "Live reports from the local background cache")
        print("\n  [1] WEATHER CENTER")
        print("  [2] U.S. NEWS WIRE")
        print("  [3] WORLD NEWS WIRE")
        print("  [4] TECHNOLOGY & CYBERSECURITY")
        print("  [5] SCIENCE & SPACE")
        print("  [6] SOLAR & AMATEUR RADIO")
        print("  [7] EARTHQUAKE MONITOR")
        print("  [8] FINANCIAL MARKETS")
        print("  [9] ISS & SATELLITE TRACKING")
        print("  [0] MOVIES & TELEVISION")
        print("\n  [X] RETURN TO MAIN MENU")
        choice = key_input("\nSELECT OPTION: ")
        if choice in ("x", "q", "e"):
            return
        if choice == "1": _weather_report(token)
        elif choice == "2": _news_wire(token, "us", "MARS NEWS WIRE - U.S.")
        elif choice == "3": _news_wire(token, "world", "MARS NEWS WIRE - WORLD")
        elif choice == "4":
            heading("TECHNOLOGY & CYBERSECURITY")
            print("\n  [1] Technology wire\n  [2] CISA cybersecurity advisories\n  [X] Return")
            sub = key_input("Select> ")
            if sub == "1": _news_wire(token, "technology", "MARS NEWS WIRE - TECHNOLOGY")
            elif sub == "2": _news_wire(token, "cybersecurity", "MARS WIRE - CYBERSECURITY")
        elif choice == "5":
            heading("SCIENCE & SPACE")
            print("\n  [1] Science wire\n  [2] Space and astronomy wire\n  [X] Return")
            sub = key_input("Select> ")
            if sub == "1": _news_wire(token, "science", "MARS NEWS WIRE - SCIENCE")
            elif sub == "2": _news_wire(token, "space", "MARS NEWS WIRE - SPACE")
        elif choice == "6": _solar_center(token)
        elif choice == "7": _earthquake_center(token)
        elif choice == "8": _market_center(token)
        elif choice == "9": _iss_center(token)
        elif choice == "0": _entertainment_center(token)


def dashboard(token: str) -> tuple[int, int, int | None]:
    unread_mail = 0
    unread_posts = 0
    active_users: int | None = None
    try:
        unread_mail = sum(1 for item in call("mail", "list", token=token)["messages"]
                          if not item["is_read"])
        unread_posts = sum(int(item.get("unread", 0))
                           for item in call("board", "boards", token=token)["boards"])
    except (OSError, RPCError):
        pass
    try:
        users = call("session", "online", token=token)["users"]
        if isinstance(users, list):
            active_users = len(users)
    except (OSError, RPCError):
        pass
    return unread_mail, unread_posts, active_users


_CHESS_PIECES = {
    "K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙",
    "k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟",
}


def _chess_history_lines(history: list[object], maximum: int) -> list[str]:
    clean = [safe_text(item)[:12] for item in history]
    pairs: list[str] = []
    for offset in range(0, len(clean), 2):
        number = offset // 2 + 1
        white = clean[offset]
        black = clean[offset + 1] if offset + 1 < len(clean) else ""
        pairs.append(f"{number:2}. {white:<9} {black}")
    return pairs[-maximum:]


def _chess_board_lines(view_data: dict[str, object], cursor: str,
                       selected: str | None, double_height: bool,
                       compact_cells: bool = False) -> list[str]:
    raw_pieces = view_data.get("pieces", {})
    pieces = raw_pieces if isinstance(raw_pieces, dict) else {}
    legal = {str(move)[2:4] for move in view_data.get("legal_moves", [])
             if isinstance(move, str) and selected and move.startswith(selected)}
    files = "abcdefgh"
    lines: list[str] = []
    cell = 2 if compact_cells else 4
    for rank in range(8, 0, -1):
        top = "   "
        bottom = f"{rank}  "
        for file_index, file_name in enumerate(files):
            square = f"{file_name}{rank}"
            dark = (file_index + rank) % 2 == 0
            background = "\033[48;5;22m" if dark else "\033[48;5;29m"
            if square == selected:
                background = "\033[48;5;90m"
            elif square in legal:
                background = "\033[48;5;23m" if dark else "\033[48;5;30m"
            focus = "\033[7m" if square == cursor else ""
            raw_piece = str(pieces.get(square, ""))
            glyph = _CHESS_PIECES.get(raw_piece, "•" if square in legal else " ")
            foreground = "\033[97m" if raw_piece.isupper() else "\033[30m"
            top += background + focus + (" " * cell) + RESET
            piece_cell = f"{glyph} " if compact_cells else f" {glyph}  "
            bottom += background + focus + foreground + piece_cell + RESET
        if double_height:
            lines.extend((top, bottom))
        else:
            lines.append(bottom)
    labels = "".join(f"{name} " for name in files) if compact_cells \
        else "".join(f" {name}  " for name in files)
    lines.append("   " + labels)
    return lines


def _draw_chess(view_data: dict[str, object], cursor: str,
                selected: str | None, local_status: str = "") -> None:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    rows = max(16, size.lines)
    desktop = columns >= 74
    double_height = rows >= (22 if desktop else 25)
    board = _chess_board_lines(view_data, cursor, selected, double_height,
                               compact_cells=columns < 36)
    userid = clip(view_data.get("userid", "PLAYER"), 12)
    heading_left = "MARS HOTEL CHESS" if desktop else "MARS CHESS"
    opponent = f"{userid} vs JOSHUA"
    if len(heading_left) + len(opponent) + 1 > columns:
        lines = [styled(clip(heading_left, columns), BOLD, CYAN),
                 styled(clip(opponent, columns), BOLD, CYAN)]
    else:
        header_gap = columns - len(heading_left) - len(opponent)
        lines = [styled(heading_left, BOLD, CYAN) + " " * header_gap
                 + styled(opponent, BOLD, CYAN)]
    turn = str(view_data.get("turn", "white")).upper()
    if bool(view_data.get("in_check")):
        turn += " - CHECK"
    message = local_status or str(view_data.get("message", ""))
    if desktop:
        panel_width = max(24, columns - 39)
        history = _chess_history_lines(list(view_data.get("history", [])), 7)
        panel = [
            styled("GAME", BOLD, CYAN),
            f"White: {userid}",
            "Black: JOSHUA",
            f"Level: {safe_text(view_data.get('level', 'Voyager'))}",
            f"Turn:  {turn}",
            "",
            styled("CAPTURED", BOLD, CYAN),
            "By White: " + safe_text(view_data.get("captured_by_white", "")),
            "By Black: " + safe_text(view_data.get("captured_by_black", "")),
            "",
            styled("MOVE HISTORY", BOLD, CYAN),
            *history,
        ]
        for index, board_line in enumerate(board):
            side = panel[index] if index < len(panel) else ""
            lines.append(board_line + " " + clip(side, panel_width))
        lines.append(styled(clip(turn + " — " + message, columns), YELLOW))
        lines.append(clip(
            "[ARROWS] MOVE  [ENTER] SELECT  [M] TYPE MOVE  [L] LEVEL  [H] HELP  [X] EXIT",
            columns))
    else:
        lines.append(styled(clip(turn, columns), BOLD, YELLOW))
        lines.extend(board)
        history = _chess_history_lines(list(view_data.get("history", [])), 2)
        if history:
            lines.append(styled(clip("LAST MOVES  " + "  ".join(history), columns),
                                BOLD, CYAN))
        lines.append(styled(clip(message, columns), YELLOW))
        if columns < 36:
            lines.append(clip("[ARROWS] MOVE [ENTER] GO", columns))
            lines.append(clip("[M] TYPE  [L] LEVEL", columns))
            lines.append(clip("[H] HELP  [X] EXIT", columns))
        else:
            lines.append(clip("[ARROWS] MOVE  [ENTER] SELECT", columns))
            lines.append(clip("[M] MOVE [L] LEVEL [H] HELP [X] EXIT", columns))
    visible = max(1, rows)
    # Never emit more physical lines than the negotiated screen height.  The
    # compact single-height board is used first when vertical room is scarce;
    # this final bound protects unusual clients that misreport dimensions.
    lines = lines[:visible]
    sys.stdout.write("\033[?25l\033[2J\033[H" + "\r\n".join(lines))
    sys.stdout.flush()


def _chess_help() -> None:
    _text_viewer(
        "MARS HOTEL CHESS", "Full tournament chess against Stockfish 19",
        "ARROWS move the board cursor. Enter selects a piece, then Enter on a "
        "highlighted destination moves it. M opens algebraic entry; examples "
        "are e4, Nf3, O-O, and e2e4.\n\n"
        "L cycles Joshua through Cadet, Voyager, and Commander. N starts a new "
        "game after confirmation. R resigns after confirmation. X, Escape, or "
        "Ctrl-C saves the current position and returns to the Arcade.\n\n"
        "All castling, promotion, en passant, check, checkmate, stalemate, "
        "repetition, and fifty-move rules are validated by the game authority. "
        "The engine has no network access and cannot see other BBS data.")


def _chess_client(token: str, result: dict[str, object], lease: str) -> None:
    """Responsive cursor-driven chess frontend over the authenticated game RPC."""
    cursor = "e2"
    selected: str | None = None
    local_status = "Saved game resumed." if result.get("resumed") else ""
    levels = ["CADET", "VOYAGER", "COMMANDER"]

    def submit(command_value: str) -> bool:
        nonlocal result, selected, local_status
        try:
            result = call("game", "input", {"lease": lease, "command": command_value}, token)
            selected = None
            local_status = ""
            return not bool(result.get("exit"))
        except (OSError, RPCError) as exc:
            local_status = "Unable to move: " + safe_text(exc)
            return True

    try:
        while True:
            raw_view = result.get("view")
            if not isinstance(raw_view, dict):
                raise RPCError("chess display data is unavailable")
            view_data = dict(raw_view)
            _draw_chess(view_data, cursor, selected, local_status)
            try:
                choice = key_input("")
            except (EOFError, KeyboardInterrupt):
                break
            pieces_raw = view_data.get("pieces", {})
            pieces = pieces_raw if isinstance(pieces_raw, dict) else {}
            legal_moves = [str(item) for item in view_data.get("legal_moves", [])]
            file_index = ord(cursor[0]) - ord("a")
            rank = int(cursor[1])
            if choice == "k":
                rank = min(8, rank + 1)
            elif choice == "j":
                rank = max(1, rank - 1)
            elif choice == "[":
                file_index = max(0, file_index - 1)
            elif choice == "]":
                file_index = min(7, file_index + 1)
            elif choice in ("x", "q"):
                break
            elif choice in ("h", "i", "?"):
                _chess_help()
            elif choice == "l":
                current = str(view_data.get("level", "Voyager")).upper()
                level = levels[(levels.index(current) + 1) % len(levels)] \
                    if current in levels else "VOYAGER"
                submit("LEVEL " + level)
            elif choice == "m":
                clear()
                try:
                    if _active_pager is not None:
                        value = _active_pager.read_line(
                            "Move (SAN or e2e4; Esc cancels): ", cancellable=True)
                    else:
                        value = input("Move (SAN or e2e4): ")
                except InputCancelled:
                    local_status = "Move entry cancelled."
                else:
                    if value.strip():
                        submit(value)
            elif choice == "n":
                clear()
                try:
                    answer = (_active_pager.read_line(
                        "Start a new game and replace this position? Type YES: ",
                        cancellable=True) if _active_pager is not None else input("Type YES: "))
                except InputCancelled:
                    answer = ""
                local_status = "New game cancelled."
                if answer.strip().upper() == "YES":
                    submit("NEW GAME")
                    cursor = "e2"
            elif choice == "r":
                clear()
                try:
                    answer = (_active_pager.read_line(
                        "Resign this game? Type YES: ", cancellable=True)
                        if _active_pager is not None else input("Type YES: "))
                except InputCancelled:
                    answer = ""
                local_status = "Resignation cancelled."
                if answer.strip().upper() == "YES":
                    submit("RESIGN YES")
            elif choice == "":
                square_piece = str(pieces.get(cursor, ""))
                if str(view_data.get("phase")) != "playing":
                    local_status = "Game complete. Press N for a new game or X to exit."
                elif selected is None:
                    if square_piece.isupper():
                        selected = cursor
                        local_status = f"Selected {cursor}. Choose a highlighted destination."
                    else:
                        local_status = "Select one of your white pieces."
                elif cursor == selected:
                    selected = None
                    local_status = "Selection cancelled."
                elif square_piece.isupper():
                    selected = cursor
                    local_status = f"Selected {cursor}."
                else:
                    candidates = [move for move in legal_moves
                                  if move[:2] == selected and move[2:4] == cursor]
                    if candidates:
                        move = next((item for item in candidates if item.endswith("q")),
                                    candidates[0])
                        submit(move)
                    else:
                        local_status = f"{selected}-{cursor} is not legal in this position."
            cursor = chr(ord("a") + file_index) + str(rank)
    finally:
        sys.stdout.write(RESET + "\033[?25h")
        sys.stdout.flush()
        try:
            call("game", "quit", {"lease": lease}, token)
        except (OSError, RPCError):
            pass


def _arcade_game(token: str, game: str) -> None:
    """Drive one authority-owned state machine through the authenticated RPC."""
    try:
        if game == "wopr":
            clear()
            print("CONNECTING TO WOPR COMPUTER SYSTEM...")
            time.sleep(0.12)
        result = call("game", "start", {"game": game}, token)
    except (OSError, RPCError) as exc:
        show_error(exc)
        pause()
        return
    lease = str(result["lease"])
    if game == "chess":
        _chess_client(token, result, lease)
        return
    try:
        while True:
            clear()
            print(_library_safe_text(result["screen"]))
            if result.get("resumed"):
                print(styled("\n[Saved game resumed]", DIM))
                result["resumed"] = False
            try:
                # Keep vertical spacing outside the editable prompt. Pager
                # redraws the prompt after every key; embedding a newline in
                # it made each typed character move down another row.
                print()
                prompt = str(result.get("prompt", "> "))
                if _active_pager is not None:
                    value = _active_pager.read_line(prompt, cancellable=True)
                else:
                    value = input(prompt)
            except (EOFError, KeyboardInterrupt, InputCancelled):
                try:
                    call("game", "quit", {"lease": lease}, token)
                except (OSError, RPCError):
                    pass
                return
            result = call("game", "input", {"lease": lease, "command": value}, token)
            if result.get("exit"):
                return
    except (OSError, RPCError) as exc:
        show_error(exc)
        pause()
        try:
            call("game", "quit", {"lease": lease}, token)
        except (OSError, RPCError):
            pass


def _arcade_scores(token: str) -> None:
    try:
        result = call("game", "scores", token=token)
    except (OSError, RPCError) as exc:
        show_error(exc)
        pause()
        return
    lines = ["HALL OF FAME", ""]
    scores = list(result.get("scores", []))
    if not scores:
        lines.append("No completed games have been recorded yet.")
    for index, row in enumerate(scores, 1):
        lines.append(
            f"{index:3}. {clip(row['userid'], 12):12} {clip(row['game'], 16):16} "
            f"{int(row['score']):7}  {clip(row['result'], 30)}")
    lines.extend(("", "YOUR RECORDS"))
    personal = list(result.get("personal", []))
    if not personal:
        lines.append("No completed games recorded for this account.")
    for row in personal:
        lines.append(f"{str(row['game']):18} plays {int(row['plays']):3}  "
                     f"best {int(row['best']):7}")
    _text_viewer("GALACTIC ARCADE RECORDS", "Verified completions and personal bests",
                 "\n".join(lines), preserve_columns=True)


def _arcade_door(token: str, game: str) -> None:
    """Hand the current PTY to an authenticated external game process."""
    clear()
    try:
        pager = _active_pager
        play_door(game, token, notification_socket=pager.sock if pager else None,
                  on_notification=pager.receive if pager else None,
                  paint_notification=pager.draw_notification if pager else None)
    except (OSError, EOFError, DoorError) as exc:
        clear()
        show_error(exc)
        pause()
    finally:
        # A DOS title or interpreter may leave color, cursor, or scroll state
        # behind.  The transport restores termios; redraw the BBS separately.
        sys.stdout.write(RESET + "\033[?25h")
        sys.stdout.flush()
        clear()


def arcade(token: str, wopr_direct: bool = False) -> None:
    """Authenticated Galactic Arcade menu and hidden Joshua entrance."""
    try:
        available = {str(row["id"]) for row in
                     call("game", "catalog", token=token).get("games", [])}
    except (OSError, RPCError) as exc:
        show_error(exc)
        pause()
        return
    try:
        doors = {str(row["id"]): row for row in
                 call("door", "catalog", token=token).get("games", [])}
    except (OSError, RPCError):
        doors = {}
    if wopr_direct:
        if "wopr" in available:
            _arcade_game(token, "wopr")
        else:
            print("The WOPR terminal is currently offline.")
            pause()
        return
    while True:
        heading("THE MARS HOTEL / GALACTIC ARCADE",
                "Terminal games; progress saves automatically")
        if "oregon" in available:
            print("[O]regon Trail   1970s-style overland expedition")
        if "wopr" in available:
            print("[W]OPR / Joshua  Interactive movie-inspired computer system")
        if "chess" in available:
            print("[C]hess          Full tournament chess against JOSHUA")
        if doors.get("zork", {}).get("ready"):
            print("[Z]ork I         The Great Underground Empire (release 119)")
        if doors.get("lord", {}).get("ready"):
            print("[L]egend         Legend of the Red Dragon 4.02a")
        if doors.get("tradewars", {}).get("ready"):
            print("[T]radeWars      TradeWars 2002 3.09")
        print("[S]cores         Hall of Fame and your records")
        print("[H]elp           Commands, persistence, and safe exits")
        print("[X] Exit           Return to the Lobby")
        try:
            choice = key_input("Arcade> ")
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("x", "q"):
            return
        if choice == "o" and "oregon" in available:
            _arcade_game(token, "oregon")
        elif choice == "w" and "wopr" in available:
            _arcade_game(token, "wopr")
        elif choice == "c" and "chess" in available:
            _arcade_game(token, "chess")
        elif choice == "z" and doors.get("zork", {}).get("ready"):
            _arcade_door(token, "zork")
        elif choice == "l" and doors.get("lord", {}).get("ready"):
            _arcade_door(token, "lord")
        elif choice == "t" and doors.get("tradewars", {}).get("ready"):
            _arcade_door(token, "tradewars")
        elif choice == "s":
            _arcade_scores(token)
        elif choice in ("h", "?"):
            _information_page("GALACTIC ARCADE", "Games, saves, records, and safe exits",
                              "help-games.txt")


def _marsamp_pairing_screen(pairing_url: object, selected: object = "") -> None:
    """Present the one-click browser handoff with a plain URL fallback."""
    heading("MARSAMP AUDIO PAIRING",
            "Open once in a PC or phone browser; ticket expires quickly")
    if selected:
        print("\nSelected: " + clip(safe_text(selected), width()))
    url = safe_text(pairing_url)
    inner_width = max(18, min(54, width() - 2))
    label = clip(">>> TAP: OPEN MARSAMP IN BROWSER <<<", inner_width)
    border = "+" + "-" * inner_width + "+"
    print("\n" + styled(border, BOLD, CYAN))
    print(styled("|", BOLD, CYAN) +
          styled(terminal_hyperlink(label.center(inner_width), url),
                 BOLD, REVERSE, GREEN) +
          styled("|", BOLD, CYAN))
    print(styled(border, BOLD, CYAN))
    print("\nPlain address for terminals that cannot open the button:")
    print(url + "\n")
    for line in textwrap.wrap(
            "Tap the highlighted button above. On iPhone or iPad, then tap "
            "PLAY SELECTED SONG once when the browser opens.",
            width=max(20, width())):
        print(line)
    print("The browser receives an opaque session cookie. Your BBS password,")
    print("NAS locations, filenames, and internal service addresses are not sent.")
    pause()


def _marsamp_play(token: str, track: dict[str, object],
                  tracks: list[dict[str, object]], mode: str,
                  scope: str = "") -> str:
    """Select a track and offer browser handoff when no sink is active."""
    request = {
        "id": int(track["id"]),
        "queue": [int(row["id"]) for row in tracks],
        "mode": mode,
    }
    if scope:
        request["scope"] = scope
    state = call("music", "play", request, token)
    pair_url = state.get("pair_url")
    if pair_url:
        artist = safe_text(track.get("artist", ""))
        title = safe_text(track.get("title", "Untitled"))
        _marsamp_pairing_screen(pair_url, (artist + " - " if artist else "") + title)
        return "Song selected. Complete browser pairing to hear it."
    return "Play command sent to the paired audio sink."


def _marsamp_choose_playlist(token: str) -> dict[str, object] | None:
    playlists = list(call("music", "playlists", token=token).get("playlists", []))
    if not playlists:
        return {"all": True}
    cursor = 0
    number_width = max(2, len(str(len(playlists))))
    number_buffer = ""
    while True:
        heading("MARSAMP PLAYLISTS", "Curated from highly rated songs available on the NAS")
        for position, playlist in enumerate(playlists):
            label = (f"{position + 1:0{number_width}d}. "
                     f"{safe_text(playlist.get('name', 'Playlist'))} "
                     f"({int(playlist.get('tracks', 0))} songs)")
            rendered = clip(label, width())
            print(styled(rendered, REVERSE) if position == cursor else rendered)
        selected = playlists[cursor]
        print("\n" + clip(safe_text(selected.get("description", "")), width()))
        state = call("music", "status", token=token)
        track = state.get("track")
        if isinstance(track, dict):
            print(clip("Last / current: " + str(track.get("artist", "")) + " - " +
                       str(track.get("title", "")), width()))
        number_help = (f"[01-{len(playlists):0{number_width}d}] open  "
                       "[Enter] selected  [T] all music  [P] play/pause  "
                       "[X/Esc] return")
        print(styled("\n" + number_help, DIM))
        prompt = (f"Playlists [{number_buffer}{'_' * (number_width - len(number_buffer))}]> "
                  if number_buffer else "Playlists> ")
        choice = key_input(prompt, escape_command="x")
        if choice in ("x", "q"):
            return None
        if choice.isascii() and choice.isdigit() and len(choice) <= number_width:
            number_buffer = (number_buffer + choice)[-number_width:]
            numbered = int(number_buffer)
            longer_match = any(
                f"{position:0{number_width}d}".startswith(number_buffer)
                for position in range(1, len(playlists) + 1)
            )
            if len(number_buffer) == number_width or not longer_match:
                number_buffer = ""
                if 1 <= numbered <= len(playlists):
                    cursor = numbered - 1
                    selected = playlists[cursor]
                    if int(selected.get("tracks", 0)):
                        return selected
            continue
        if choice == "" and number_buffer:
            numbered = int(number_buffer)
            number_buffer = ""
            if 1 <= numbered <= len(playlists):
                cursor = numbered - 1
                selected = playlists[cursor]
                if int(selected.get("tracks", 0)):
                    return selected
            continue
        number_buffer = ""
        if choice == "t":
            return {"all": True}
        if choice == "p":
            _marsamp_transport(token, "pause" if state.get("action") == "play" and
                               state.get("browser_active", True) else "resume")
        if choice == "j":
            cursor = min(len(playlists) - 1, cursor + 1)
        elif choice == "k":
            cursor = max(0, cursor - 1)
        elif choice == "":
            if int(selected.get("tracks", 0)):
                return selected


def _marsamp_transport(token: str, operation: str) -> dict[str, object]:
    state = call("music", operation, token=token)
    pair_url = state.get("pair_url")
    if pair_url:
        track = state.get("track")
        label = ""
        if isinstance(track, dict):
            artist = safe_text(track.get("artist", ""))
            label = (artist + " - " if artist else "") + safe_text(
                track.get("title", "Untitled"))
        _marsamp_pairing_screen(pair_url, label)
    return state


def marsamp(token: str) -> None:
    """Control the authenticated browser audio sink from an ANSI player."""
    query = safe_text(_marsamp_view.get("query", ""))[:80]
    playlist_slug = safe_text(_marsamp_view.get("playlist", ""))[:48]
    playlist_name = safe_text(_marsamp_view.get("playlist_name", ""))[:80]
    playlist_dynamic = ""
    try:
        cursor = max(0, int(_marsamp_view.get("cursor", 0)))
    except (TypeError, ValueError):
        cursor = 0
    mode = safe_text(_marsamp_view.get("mode", "sequential"))
    if mode not in ("sequential", "shuffle"):
        mode = "sequential"
    remembered_track = int(_marsamp_view.get("track_id", 0) or 0)
    entering = True
    browse = str(_marsamp_view.get("browse", "all" if query else "playlists"))
    notice = ""
    while True:
        try:
            if browse == "playlists" and not playlist_slug:
                selected = _marsamp_choose_playlist(token)
                if selected is None:
                    return
                if selected.get("all"):
                    browse = "all"
                    query = ""
                else:
                    playlist_slug = safe_text(selected.get("slug", ""))[:48]
                    playlist_name = safe_text(selected.get("name", ""))[:80]
            if playlist_slug:
                result = call("music", "playlist_tracks", {
                    "slug": playlist_slug, "offset": 0, "limit": 250,
                }, token)
                playlist = result.get("playlist")
                if isinstance(playlist, dict):
                    playlist_name = safe_text(playlist.get("name", playlist_name))[:80]
                    playlist_dynamic = safe_text(playlist.get("dynamic", ""))[:16]
            else:
                playlist_dynamic = ""
                result = call("music", "search", {
                    "query": query, "offset": 0, "limit": 40,
                }, token)
            tracks = list(result.get("tracks", []))
            state = call("music", "status", token=token)
            stats = call("music", "stats", token=token)
        except (OSError, RPCError) as exc:
            heading("MARSAMP", "Authenticated ANSI music player")
            print("MarsAmp is unavailable: " + safe_text(exc))
            print("The optional music service may not be enabled on this installation.")
            pause()
            return
        service_mode = safe_text(state.get("mode", mode))
        if service_mode in ("sequential", "shuffle"):
            mode = service_mode
        if playlist_dynamic == "all":
            mode = "shuffle"
        current = state.get("track")
        current_track = (int(current.get("id", 0))
                         if isinstance(current, dict) else 0)
        if entering and current_track and current_track != remembered_track:
            for position, row in enumerate(tracks):
                if int(row["id"]) == current_track:
                    cursor = position
                    break
        entering = False
        cursor = max(0, min(cursor, len(tracks) - 1)) if tracks else 0
        _marsamp_view.update(query=query, playlist=playlist_slug,
                             playlist_name=playlist_name, cursor=cursor, mode=mode,
                             track_id=current_track, browse=browse)
        heading("MARSAMP 0.1", right=str(state.get("action", "stop")).upper())
        if isinstance(current, dict):
            artist = safe_text(current.get("artist", ""))
            title = safe_text(current.get("title", "Untitled"))
            print(styled(clip((artist + " - " if artist else "") + title, width()),
                         BOLD, CYAN))
        else:
            print(styled("No track selected", DIM))
        active = str(state.get("action", "stop")) == "play"
        bars = "".join("▇" if active and (index + int(time.time())) % 3 else "▂"
                       for index in range(min(24, max(8, width() // 3))))
        print(styled(bars, GREEN if active else DIM))
        source_label = ("Playlist: " + playlist_name if playlist_slug
                        else "Search: " + (query or "(all)"))
        print(styled(f"{int(stats.get('tracks', 0))} tracks indexed  "
                     f"{source_label}  Mode: {mode.upper()}", DIM))
        print()
        if tracks:
            page_size = max(4, min(10, shutil.get_terminal_size((80, 24)).lines - 15))
            start = max(0, min(cursor - page_size // 2, len(tracks) - page_size))
            for position in range(start, min(len(tracks), start + page_size)):
                row = tracks[position]
                artist = safe_text(row.get("artist", "")) or "Unknown artist"
                label = f"{position + 1:2}. {artist} - {safe_text(row.get('title', 'Untitled'))}"
                rendered = clip(label, width())
                print(styled(rendered, REVERSE) if position == cursor else rendered)
        else:
            print("No matching tracks.")
        if notice:
            print(styled("\n" + clip(notice, width()), YELLOW))
        controls = ("[Enter] play  [B] back  [N] next  [P] play/pause  [V] stop  "
                    "[S] shuffle  [T] playlists/all  [A] available playlists  [F] find  [L] link audio  "
                    "[X/Esc] return")
        print()
        for line in textwrap.wrap(controls, width=max(20, width()),
                                  break_long_words=False, break_on_hyphens=False):
            print(styled(line, DIM))
        try:
            choice = key_input("MarsAmp> ", escape_command="x")
        except (EOFError, KeyboardInterrupt):
            return
        notice = ""
        try:
            if choice in ("x", "q"):
                return
            if choice == "j":
                cursor = min(len(tracks) - 1, cursor + 1) if tracks else 0
            elif choice == "k":
                cursor = max(0, cursor - 1)
            elif choice == "" and tracks:
                notice = (_marsamp_play(token, tracks[cursor], tracks, mode,
                                        playlist_slug)
                          if playlist_dynamic else
                          _marsamp_play(token, tracks[cursor], tracks, mode))
            elif choice in ("b", "z"):
                if str(state.get("action", "stop")) in ("play", "pause"):
                    moved = _marsamp_transport(token, "previous")
                    moved_id = int(moved.get("track_id", 0) or 0)
                    for position, row in enumerate(tracks):
                        if int(row["id"]) == moved_id:
                            cursor = position
                            break
                    notice = "Playing the previous song."
                elif tracks:
                    cursor = (cursor - 1) % len(tracks)
                    notice = (_marsamp_play(token, tracks[cursor], tracks, mode,
                                            playlist_slug)
                              if playlist_dynamic else
                              _marsamp_play(token, tracks[cursor], tracks, mode))
            elif choice == "n":
                if str(state.get("action", "stop")) in ("play", "pause"):
                    moved = _marsamp_transport(token, "advance")
                    moved_id = int(moved.get("track_id", 0) or 0)
                    for position, row in enumerate(tracks):
                        if int(row["id"]) == moved_id:
                            cursor = position
                            break
                    notice = ("Shuffled to another song." if mode == "shuffle"
                              else "Playing the next song.")
                elif tracks:
                    cursor = (cursor + 1) % len(tracks)
                    notice = (_marsamp_play(token, tracks[cursor], tracks, mode,
                                            playlist_slug)
                              if playlist_dynamic else
                              _marsamp_play(token, tracks[cursor], tracks, mode))
            elif choice in ("p", "c"):
                _marsamp_transport(token, "pause" if state.get("action") == "play" and
                               state.get("browser_active", True) else "resume")
            elif choice == "v":
                call("music", "stop", token=token)
            elif choice in ("s", "m"):
                mode = ("shuffle" if playlist_dynamic else
                        "shuffle" if mode == "sequential" else "sequential")
                call("music", "mode", {"mode": mode}, token)
                _marsamp_view.update(query=query, cursor=cursor, mode=mode)
                notice = ("Shuffle Everything always uses the full-library shuffle."
                          if playlist_dynamic else
                          "Play mode changed to " + mode.upper() + ".")
            elif choice == "f":
                browse = "all"
                print()
                query = input("Find artist, album, or title (blank shows all): ").strip()[:80]
                playlist_slug = ""
                playlist_name = ""
                cursor = 0
                _marsamp_view.update(query=query, playlist="", playlist_name="",
                                     cursor=cursor)
            elif choice == "t":
                browse = "all" if playlist_slug else "playlists"
                query = ""
                playlist_slug = ""
                playlist_name = ""
                cursor = 0
            elif choice in ("a", "r"):
                selected = _marsamp_choose_playlist(token)
                if selected is not None:
                    browse = "all" if selected.get("all") else "playlists"
                    query = ""
                    playlist_slug = safe_text(selected.get("slug", ""))[:48]
                    playlist_name = safe_text(selected.get("name", ""))[:80]
                    cursor = 0
                    _marsamp_view.update(playlist=playlist_slug,
                                         playlist_name=playlist_name, cursor=cursor)
            elif choice == "l":
                pairing = call("music", "pair", token=token)
                _marsamp_pairing_screen(pairing["url"])
        except (OSError, RPCError, ValueError) as exc:
            notice = "Request rejected: " + safe_text(exc)


def main_heading(identity: dict[str, object], active_users: int | None,
                 site: dict[str, object]) -> None:
    """Keep Lobby identity and presence above the compact music player."""
    clear()
    columns = min(width(), 78)
    title = safe_text(site.get("bbs_name", "The Mars Hotel")).upper() + " LOBBY"
    count = f"ACTIVE USERS: {active_users if active_users is not None else '?'}"
    header_rows = 1
    if len(title) + len(count) + 1 <= columns:
        gap = " " * (columns - len(title) - len(count))
        print(styled(title, BOLD, CYAN) + gap + styled(count, BOLD, CYAN))
    else:
        print(styled(clip(title, columns), BOLD, CYAN))
        print(styled(clip(count, columns), BOLD, CYAN))
        header_rows += 1
    name = safe_text(identity['display_name'])
    handle = safe_text(identity['userid'])
    label = f"{name} (@{handle})" if name.casefold() != handle.casefold() else f"@{handle}"
    print(styled(clip(label, columns), DIM))
    _mini_heading(lobby=True, row=header_rows + 2)
    print(styled("=" * columns, DIM))


def confirm_main_menu_exit(bbs_name: str) -> bool:
    """Require an affirmative response before Escape logs off from the Lobby."""
    try:
        choice = key_input(f"\nAre you sure you want to leave {safe_text(bbs_name)}? [Y/N] -> ")
    except EOFError:
        return True
    except KeyboardInterrupt:
        return False
    return choice == "y"


def main() -> None:
    global _active_pager, _mini_token
    descriptor = int(os.environ.get("MARS_V2_SESSION_FD", "3"))
    payload = b""
    while True:
        part = os.read(descriptor, 4096)
        if not part:
            break
        payload += part
        if len(payload) > 4096:
            raise SystemExit("invalid session payload")
    os.close(descriptor)
    session = json.loads(payload.decode("utf-8"))
    token = str(session["token"])
    identity = dict(session["identity"])
    site: dict[str, object] = {"bbs_name": "The Mars Hotel", "bbs_id": "mars-hotel"}
    pager: Pager | None = None
    try:
        if identity.get("password_change_required") and not required_password_change(identity):
            return
        pager = Pager(token)
        _active_pager = pager
        while True:
            pager.activity("Lobby")
            # Refresh authority-derived roles on every Lobby return. This makes
            # grants and revocations take effect without issuing a new login
            # token or trusting the frontend's original identity snapshot.
            try:
                refreshed = call("session", "validate", {"peer_uid": os.getuid()}, token)
                identity.update(refreshed)
            except RPCError:
                break
            unread_mail, unread_posts, active_users = dashboard(token)
            try:
                site = call("mail", "site_identity", token=token)
            except (OSError, RPCError):
                pass
            _mini_token = token if _can(identity, "music.play") else ""
            main_heading(identity, active_users, site)
            if _mini_visible and width() >= 60 and shutil.get_terminal_size((80, 24)).lines < 38:
                # Keep the entire Lobby and prompt visible on a 24-row PTY.
                entries = [
                    (f"[O] Boards ({unread_posts} new)", "[T] Talk"),
                    ("[E] New board messages", "[I] IRC / MarsHotel room"),
                    ("[C] Board counts", "[U] Member directory"),
                    (f"[M] Mail ({unread_mail} unread)", "[D] DataNet"),
                    ("[F] Files", "[G] Galactic Arcade"),
                    ("[R] MarsAmp", "[W] Welcome / history"),
                    ("[S] Profile / terminal", "[H] Help"),
                ]
                half = min(width(), 78) // 2
                for left, right in entries:
                    print("  " + left.ljust(half - 2) + right)
                if ADMIN_CAPABILITIES.intersection(identity.get("capabilities", [])):
                    print("  [A] Administration")
                print("  [X] Exit")
            elif width() < 60:
                print(styled("COMMUNITY", BOLD, YELLOW))
                print(f" [O] Boards ({unread_posts} new)")
                print(" [E] New board messages")
                print(" [C] Board counts")
                print(f" [M] Mail ({unread_mail} unread)")
                print(styled("LIVE", BOLD, YELLOW))
                print(" [T] Talk")
                print(" [I] IRC / MarsHotel room")
                print(" [U] Member directory")
                print(styled("LIBRARY & SYSTEM", BOLD, YELLOW))
                print(" [D] DataNet")
                print(" [F] Files")
                print(" [G] Galactic Arcade")
                if _can(identity, "music.play"):
                    print(" [R] MarsAmp")
                print(" [W] Welcome / history")
                print(" [S] Profile / terminal")
                if ADMIN_CAPABILITIES.intersection(identity.get("capabilities", [])):
                    print(" [A] Administration")
                print(" [H] Help")
                print(" [X] Exit")
            else:
                print(styled("COMMUNITY", BOLD, YELLOW))
                print(f"  [O] Boards       Browse and post messages       {unread_posts:3} new")
                print("  [E] New          Scan unread board messages")
                print("  [C]ount        Board activity summary")
                print(f"  [M]ail         Private post office            {unread_mail:3} unread")
                print()
                print(styled("LIVE", BOLD, YELLOW))
                print("  [T]alk         Native Chat, paging, Direct Talk")
                print("  [I]RC          Local IRC / MarsHotel public room")
                print("  [U]sers        Member directory and current activity")
                print()
                print(styled("LIBRARY & SYSTEM", BOLD, YELLOW))
                print("  [D]ataNet      Mars Information Network")
                print("  [F]iles        Curated file areas")
                print("  [G]ames        Galactic Arcade")
                if _can(identity, "music.play"):
                    print("  [R]adio        MarsAmp NAS music player")
                print("  [W]elcome      Revival information and Mars history")
                print("  [S] Profile      Account and terminal utilities")
                if ADMIN_CAPABILITIES.intersection(identity.get("capabilities", [])):
                    print("  [A]dmin        BBS administration console")
                print("  [H]elp         Command guide")
                print("  [X] Exit        Log off")
            try:
                choice = key_input("\n[Main Lobby] -> ",
                                   escape_command="escape")
            except EOFError:
                break
            except KeyboardInterrupt:
                # Ctrl-C cancels only the unfinished menu command.  Letting it
                # escape tears down the authenticated UI and closes SSH.
                continue
            if choice == "escape":
                if confirm_main_menu_exit(str(site.get("bbs_name", "the BBS"))):
                    break
                continue
            if choice == "x":
                break
            if choice in ("b", "p", "n") and _can(identity, "music.play"):
                _mini_control(choice)
            elif choice == "o":
                pager.activity("Boards")
                boards(token)
            elif choice == "e":
                pager.activity("Reading new posts")
                scan_new_posts(token)
            elif choice == "c":
                board_counts(token)
            elif choice == "m":
                pager.activity("Post Office")
                mail(token)
            elif choice == "f":
                pager.activity("File Library")
                files(token)
            elif choice == "d":
                pager.activity("Information Network")
                information_network(token)
            elif choice == "g":
                pager.activity("Galactic Arcade")
                arcade(token)
            elif choice == "r" and _can(identity, "music.play"):
                pager.activity("MarsAmp")
                marsamp(token)
            elif choice == "j":
                # Discoverable Easter egg: Help hints that an old school
                # computer still answers to Joshua. Authentication is already
                # established and the same game authority remains in force.
                arcade(token, wopr_direct=True)
            elif choice == "t":
                pager.activity("Talk Center")
                talk(token, pager)
            elif choice == "i":
                pager.activity("IRC")
                irc_center(token)
            elif choice == "u":
                try:
                    pager.activity("Member Directory")
                    member_directory(token)
                except RPCError as exc:
                    show_error(exc)
            elif choice == "w":
                welcome_screen()
            elif choice == "s":
                utilities(identity)
            elif choice == "a" and ADMIN_CAPABILITIES.intersection(
                    identity.get("capabilities", [])):
                administration(token, identity)
            elif choice == "a":
                # Compatibility alias for users accustomed to the former
                # Account menu key. Privileged users use historical [A]dmin.
                utilities(identity)
            elif choice in ("h", "?"):
                help_center()
    finally:
        _active_pager = None
        _mini_token = ""
        if pager is not None:
            pager.close()
        try:
            call("session", "end", token=token)
        except Exception:
            pass
    sys.stdout.write("\033[r")
    print("\nGoodbye from " + safe_text(site.get("bbs_name", "The Mars Hotel")) + ".")


if __name__ == "__main__":
    main()
