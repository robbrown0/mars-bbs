#!/usr/bin/env python3
"""Deterministic editor-model and terminal-key regressions."""

from __future__ import annotations

import contextlib
import io
import os
from unittest.mock import patch

from marsv2.terminal import (
    EditorBuffer, KeyDecoder, KEY_DELETE, KEY_DOWN, KEY_F2, KEY_HOME,
    KEY_INVALID, KEY_LEFT, KEY_PAGE_DOWN, KEY_UP,
)
from marsv2.ui import _draw_mail_editor


buffer = EditorBuffer("alpha\nbrvo\ncharlie", 128)
buffer.vertical(-1, 20)
buffer.home(20)
buffer.right()
buffer.right()
assert buffer.insert("a")
assert buffer.text == "alpha\nbravo\ncharlie"
buffer.vertical(1, 20)
buffer.home(20)
assert buffer.insert("X")
buffer.backspace()
buffer.delete()
assert buffer.insert("c")
buffer.end(20)
assert buffer.text == "alpha\nbravo\ncharlie"

wrapped = EditorBuffer("0123456789abcdefghij", 64)
rows, row, column = wrapped.visual_position(10)
assert len(rows) == 2 and row == 1 and column == 10
wrapped.home(10)
assert wrapped.cursor == 10
wrapped.vertical(-1, 10)
assert wrapped.cursor == 0
wrapped.vertical(1, 10)
assert wrapped.cursor == 10

limited = EditorBuffer("1234", 4)
assert not limited.insert("5") and limited.text == "1234"
assert not limited.insert("\x1b") and limited.text == "1234"

def decode(raw: bytes) -> tuple[str, str]:
    reader, writer = os.pipe()
    try:
        os.write(writer, raw)
        return KeyDecoder().read(reader)
    finally:
        os.close(reader)
        os.close(writer)


assert decode(b"\x1b[A")[0] == KEY_UP
assert decode(b"\x1bOB")[0] == KEY_DOWN
assert decode(b"\x1b[D")[0] == KEY_LEFT
assert decode(b"\x1b[H")[0] == KEY_HOME
assert decode(b"\x1b[3~")[0] == KEY_DELETE
assert decode(b"\x1b[6~")[0] == KEY_PAGE_DOWN
assert decode(b"\x1bOQ")[0] == KEY_F2
assert decode(b"\x13")[0] == KEY_F2
assert decode(b"\x1b[999z")[0] == KEY_INVALID
assert decode(b"\x01")[0] == KEY_INVALID

# The phone-sized view must show the send and cancel controls instead of
# clipping them beyond the right edge of the terminal.
rendered = io.StringIO()
with patch("marsv2.ui.shutil.get_terminal_size",
           return_value=os.terminal_size((58, 24))):
    with contextlib.redirect_stdout(rendered):
        _draw_mail_editor(
            "rob", "tst", EditorBuffer("", 8192), 0,
            "0 / 8192 characters", "edit", "POST OFFICE - COMPOSE",
            "To", "Send", True,
        )
output = rendered.getvalue()
assert "F2/Ctrl-S Send" in output
assert "Esc cancel" in output

print("PASS bounded editor model and terminal key decoder")
