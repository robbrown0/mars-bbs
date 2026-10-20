"""Bounded terminal-key decoding and an editable multiline text buffer.

Terminal control sequences are commands, never text.  This module deliberately
recognizes a small portable xterm/VT key set and rejects every other escape or
control sequence before it can enter persistent BBS content.
"""

from __future__ import annotations

import codecs
import os
import select
from dataclasses import dataclass


KEY_ENTER = "enter"
KEY_BACKSPACE = "backspace"
KEY_DELETE = "delete"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_UP = "up"
KEY_DOWN = "down"
KEY_HOME = "home"
KEY_END = "end"
KEY_PAGE_UP = "page_up"
KEY_PAGE_DOWN = "page_down"
KEY_ESCAPE = "escape"
KEY_F2 = "f2"
KEY_F3 = "f3"
KEY_EOF = "eof"
KEY_INTERRUPT = "interrupt"
KEY_INVALID = "invalid"
KEY_CHAR = "char"
KEY_RESIZE = "resize"


ESCAPE_KEYS = {
    b"\x1b[A": KEY_UP, b"\x1bOA": KEY_UP,
    b"\x1b[B": KEY_DOWN, b"\x1bOB": KEY_DOWN,
    b"\x1b[C": KEY_RIGHT, b"\x1bOC": KEY_RIGHT,
    b"\x1b[D": KEY_LEFT, b"\x1bOD": KEY_LEFT,
    b"\x1b[H": KEY_HOME, b"\x1bOH": KEY_HOME,
    b"\x1b[1~": KEY_HOME, b"\x1b[7~": KEY_HOME,
    b"\x1b[F": KEY_END, b"\x1bOF": KEY_END,
    b"\x1b[4~": KEY_END, b"\x1b[8~": KEY_END,
    b"\x1b[3~": KEY_DELETE,
    b"\x1b[5~": KEY_PAGE_UP,
    b"\x1b[6~": KEY_PAGE_DOWN,
    b"\x1bOQ": KEY_F2, b"\x1b[12~": KEY_F2,
    b"\x1bOR": KEY_F3, b"\x1b[13~": KEY_F3,
    # Bracketed-paste markers are discarded; pasted printable characters are
    # subsequently handled through the same bounded path as typed characters.
    b"\x1b[200~": "paste_start", b"\x1b[201~": "paste_end",
}


def _escape(fd: int, timeout: float = 0.20) -> bytes:
    sequence = bytearray(b"\x1b")
    for _ in range(15):
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            break
        part = os.read(fd, 1)
        if not part:
            break
        sequence.extend(part)
        # CSI/SS3 sequences end in the 0x40-0x7e range.  An ESC followed by an
        # ordinary character is an unsupported Alt key and ends immediately.
        if len(sequence) == 2 and sequence[1] not in (ord("["), ord("O")):
            break
        if len(sequence) >= 3 and 0x40 <= sequence[-1] <= 0x7e:
            if sequence[-1] == ord("[") and len(sequence) == 2:
                continue
            # A trailing '~' or letter is final; digits/';' are parameters.
            if sequence[-1] == ord("~") or chr(sequence[-1]).isalpha():
                break
    return bytes(sequence)


class KeyDecoder:
    """Decode one raw terminal key without returning terminal bytes as text."""

    def __init__(self) -> None:
        self.utf8 = codecs.getincrementaldecoder("utf-8")("strict")

    def read(self, fd: int) -> tuple[str, str]:
        raw = os.read(fd, 1)
        if not raw:
            return KEY_EOF, ""
        value = raw[0]
        if raw in (b"\r", b"\n"):
            return KEY_ENTER, ""
        if raw in (b"\x7f", b"\x08"):
            return KEY_BACKSPACE, ""
        if raw == b"\x1b":
            sequence = _escape(fd)
            if sequence == b"\x1b":
                return KEY_ESCAPE, ""
            return ESCAPE_KEYS.get(sequence, KEY_INVALID), sequence.hex()
        if raw == b"\x03":
            return KEY_INTERRUPT, ""
        if raw == b"\x04":
            return KEY_EOF, ""
        # Mobile SSH keyboards commonly expose Ctrl but omit function keys.
        # Treat Ctrl-S as the portable equivalent of F2 in full-screen editors.
        if raw == b"\x13":
            return KEY_F2, ""
        if value < 32:
            return KEY_INVALID, f"control-{value}"
        try:
            character = self.utf8.decode(raw, final=False)
        except UnicodeDecodeError:
            self.utf8.reset()
            return KEY_INVALID, "invalid-utf8"
        if not character:
            return "pending", ""
        if any(ord(item) < 32 or ord(item) == 127 or 0x80 <= ord(item) <= 0x9f
               for item in character):
            return KEY_INVALID, "non-printing-character"
        return KEY_CHAR, character


@dataclass(frozen=True)
class VisualRow:
    start: int
    end: int


class EditorBuffer:
    """A bounded logical buffer with cursor movement over soft-wrapped rows."""

    def __init__(self, value: str = "", maximum: int = 8192):
        if len(value) > maximum:
            raise ValueError("initial message exceeds editor limit")
        self.text = value
        self.cursor = len(value)
        self.maximum = maximum
        self.preferred_column: int | None = None

    def insert(self, value: str) -> bool:
        if not value or len(self.text) + len(value) > self.maximum:
            return False
        if any(ord(ch) < 32 or ord(ch) == 127 or 0x80 <= ord(ch) <= 0x9f
               for ch in value):
            return False
        self.text = self.text[:self.cursor] + value + self.text[self.cursor:]
        self.cursor += len(value)
        self.preferred_column = None
        return True

    def newline(self) -> bool:
        if len(self.text) >= self.maximum:
            return False
        self.text = self.text[:self.cursor] + "\n" + self.text[self.cursor:]
        self.cursor += 1
        self.preferred_column = None
        return True

    def backspace(self) -> None:
        if self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1
        self.preferred_column = None

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        self.preferred_column = None

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)
        self.preferred_column = None

    def right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)
        self.preferred_column = None

    def rows(self, columns: int) -> list[VisualRow]:
        columns = max(1, columns)
        result: list[VisualRow] = []
        start = 0
        while start <= len(self.text):
            newline = self.text.find("\n", start)
            logical_end = len(self.text) if newline < 0 else newline
            if logical_end == start:
                result.append(VisualRow(start, start))
            else:
                offset = start
                while offset < logical_end:
                    end = min(logical_end, offset + columns)
                    result.append(VisualRow(offset, end))
                    offset = end
            if newline < 0:
                break
            start = newline + 1
        return result or [VisualRow(0, 0)]

    def visual_position(self, columns: int) -> tuple[list[VisualRow], int, int]:
        rows = self.rows(columns)
        selected = len(rows) - 1
        for index, row in enumerate(rows):
            if row.start <= self.cursor <= row.end:
                # At a soft-wrap boundary, the cursor belongs to the following
                # row.  A hard newline creates a one-offset gap instead.
                if (self.cursor == row.end and index + 1 < len(rows)
                        and rows[index + 1].start == self.cursor):
                    continue
                selected = index
                break
        return rows, selected, max(0, self.cursor - rows[selected].start)

    def vertical(self, delta: int, columns: int) -> None:
        rows, row_index, column = self.visual_position(columns)
        if self.preferred_column is None:
            self.preferred_column = column
        target = max(0, min(len(rows) - 1, row_index + delta))
        row = rows[target]
        self.cursor = min(row.end, row.start + self.preferred_column)

    def home(self, columns: int) -> None:
        rows, index, _ = self.visual_position(columns)
        self.cursor = rows[index].start
        self.preferred_column = None

    def end(self, columns: int) -> None:
        rows, index, _ = self.visual_position(columns)
        self.cursor = rows[index].end
        self.preferred_column = None
