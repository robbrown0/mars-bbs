"""Validated IANA time zones and a reusable terminal picker."""

from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import tty
from datetime import datetime, timezone as datetime_timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from .terminal import (
    EditorBuffer, KeyDecoder, KEY_BACKSPACE, KEY_CHAR, KEY_DELETE, KEY_DOWN,
    KEY_END, KEY_ENTER, KEY_EOF, KEY_ESCAPE, KEY_HOME, KEY_INTERRUPT,
    KEY_INVALID, KEY_LEFT, KEY_PAGE_DOWN, KEY_PAGE_UP, KEY_RIGHT, KEY_UP,
)


@lru_cache(maxsize=1)
def timezone_names() -> tuple[str, ...]:
    """Return useful IANA zones without implementation-only trees."""
    hidden = {"Factory", "localtime", "posixrules"}
    names = {
        name for name in available_timezones()
        if name not in hidden
        and not name.startswith(("posix/", "right/", "SystemV/"))
        and len(name) <= 64
    }
    names.add("UTC")
    return tuple(["UTC"] + sorted(names - {"UTC"}, key=str.casefold))


def validate_timezone(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("invalid time zone")
    if value not in timezone_names():
        raise ValueError("unknown time zone")
    # Loading it here ensures a damaged/missing zoneinfo file is rejected at
    # the authority boundary, not discovered later while formatting output.
    try:
        ZoneInfo(value)
    except (KeyError, ValueError) as exc:
        raise ValueError("unknown time zone") from exc
    return value


def timezone_matches(names: tuple[str, ...], query: str) -> list[str]:
    needle = query.casefold().strip()
    if not needle:
        return list(names)
    matches = [name for name in names if needle in name.casefold()]
    return sorted(matches, key=lambda name: (
        0 if name.casefold().startswith(needle) else 1,
        name.casefold(),
    ))


def _draw_picker(names: tuple[str, ...], query: EditorBuffer, cursor: int,
                 current: str, status: str) -> tuple[list[str], int]:
    size = shutil.get_terminal_size((80, 24))
    columns = max(20, min(120, size.columns))
    rows = max(12, size.lines)
    visible_rows = max(3, rows - 8)
    matches = timezone_matches(names, query.text)
    cursor = max(0, min(cursor, len(matches) - 1)) if matches else 0
    start = max(0, min(cursor - visible_rows // 2,
                       max(0, len(matches) - visible_rows)))

    sys.stdout.write("\033[?25l\033[2J\033[H")
    sys.stdout.write("\033[1;36mTIME ZONE\033[0m\r\n")
    sys.stdout.write("Type to filter IANA regions and cities."[:columns] + "\r\n")
    sys.stdout.write(("Search: " + query.text)[:columns] + "\r\n")
    sys.stdout.write("-" * columns + "\r\n")
    if matches:
        for position in range(start, min(len(matches), start + visible_rows)):
            name = matches[position]
            suffix = "  (current)" if name == current else ""
            line = (name + suffix)[:columns]
            if position == cursor:
                line = "\033[7m" + line + "\033[0m"
            sys.stdout.write(line + "\r\n")
        for _ in range(visible_rows - min(visible_rows, len(matches) - start)):
            sys.stdout.write("\r\n")
    else:
        sys.stdout.write("No matching time zones. Backspace to change the search."[:columns]
                         + "\r\n")
        for _ in range(visible_rows - 1):
            sys.stdout.write("\r\n")
    sys.stdout.write("Arrows/PgUp/PgDn move  Enter chooses  Esc cancels"[:columns]
                     + "\r\n")
    sys.stdout.write((status or f"{len(matches)} of {len(names)} time zones shown")[:columns])
    sys.stdout.write(f"\033[3;{min(columns, 9 + query.cursor)}H\033[?25h")
    sys.stdout.flush()
    return matches, cursor


def choose_timezone(current: str = "UTC") -> str | None:
    """Choose a validated zone using immediate terminal keys and filtering."""
    current = validate_timezone(current)
    names = timezone_names()
    query = EditorBuffer("", 64)
    cursor = names.index(current)
    decoder = KeyDecoder()
    status = ""
    fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        raise RuntimeError("time-zone selection requires a terminal")
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            matches, cursor = _draw_picker(names, query, cursor, current, status)
            status = ""
            ready, _, _ = select.select([fd], [], [])
            if fd not in ready:
                continue
            key, value = decoder.read(fd)
            if key == "pending" or key in ("paste_start", "paste_end"):
                continue
            if key in (KEY_EOF, KEY_INTERRUPT, KEY_ESCAPE):
                return None
            if key == KEY_ENTER:
                if matches:
                    return validate_timezone(matches[cursor])
                status = "No time zone selected; change the search or press Esc to cancel."
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
                    status = "Search is limited to 64 characters."
            elif key == KEY_INVALID:
                status = "Unsupported terminal key ignored."
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (termios.error, OSError):
            pass
        sys.stdout.write("\033[0m\033[?25h")
        sys.stdout.flush()


def current_time_lines(zone_name: str, now: datetime | None = None) -> tuple[str, str]:
    zone_name = validate_timezone(zone_name)
    instant = now or datetime.now(datetime_timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=datetime_timezone.utc)
    instant = instant.astimezone(datetime_timezone.utc)
    pattern = "%A, %B %d, %Y  %I:%M:%S %p %Z (UTC%z)"
    utc_line = "UTC: " + instant.strftime(pattern)
    local_line = f"{zone_name}: " + instant.astimezone(ZoneInfo(zone_name)).strftime(pattern)
    return utc_line, local_line
