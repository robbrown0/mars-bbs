#!/usr/bin/env python3
"""Deterministic security checks for the external arcade-door boundary."""

from __future__ import annotations

import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from marsv2.door_client import DoorError, MAX_FRAME, recv_frame, send_frame
from marsv2.doord import (
    DoorAuthority,
    _cp437_graphics_to_utf8,
    _door_sys,
    _pty_dimensions,
    _read_lord_registration,
    _send_terminal_output,
)


class DoorProtocolTests(unittest.TestCase):
    def test_dos_doors_use_historical_screen_dimensions(self) -> None:
        self.assertEqual((80, 25), _pty_dimensions("lord", 132, 40))
        self.assertEqual((80, 25), _pty_dimensions("lord", 40, 15))
        self.assertEqual((80, 25), _pty_dimensions("tradewars", 120, 30))
        self.assertEqual((120, 30), _pty_dimensions("zork", 120, 30))

    def test_cp437_art_becomes_utf8_without_changing_ansi(self) -> None:
        raw = b"\x1b[31m\xb0\xb1\xb2\xdb\xdc\xdd\xde\xdf\xb3\xc4\xda\xbf\xc0\xd9\x1b[0m"
        self.assertEqual(
            "\x1b[31m░▒▓█▄▌▐▀│─┌┐└┘\x1b[0m".encode("utf-8"),
            _cp437_graphics_to_utf8(raw),
        )

    def test_dos_output_expansion_remains_in_bounded_frames(self) -> None:
        left, right = socket.socketpair()
        try:
            raw = b"\xdb" * MAX_FRAME

            def writer() -> None:
                _send_terminal_output(left, "lord", raw)
                left.shutdown(socket.SHUT_WR)

            thread = threading.Thread(target=writer)
            thread.start()
            rendered = bytearray()
            while True:
                try:
                    kind, payload = recv_frame(right)
                except EOFError:
                    break
                self.assertEqual(b"O", kind)
                self.assertLessEqual(len(payload), MAX_FRAME)
                rendered.extend(payload)
            thread.join()
            self.assertEqual(("█" * MAX_FRAME).encode("utf-8"), bytes(rendered))
        finally:
            left.close()
            right.close()

    def test_lord_registration_requires_private_five_number_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "lord-registration")
            with open(path, "w", encoding="ascii") as stream:
                stream.write("1 22 333 4444 55555\n")
            os.chmod(path, 0o600)
            self.assertEqual(("1", "22", "333", "4444", "55555"),
                             _read_lord_registration(path))
            os.chmod(path, 0o644)
            with self.assertRaises(RuntimeError):
                _read_lord_registration(path)
            os.chmod(path, 0o600)
            with open(path, "w", encoding="ascii") as stream:
                stream.write("1 2 3 4\n")
            with self.assertRaises(RuntimeError):
                _read_lord_registration(path)

    def test_disabled_dos_templates_are_not_seeded_or_initialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "data")
            templates = Path(directory, "templates")
            Path(templates, "lord").mkdir(parents=True)
            Path(templates, "tradewars").mkdir()
            with mock.patch.object(DoorAuthority, "_initialize_legacy_worlds"):
                DoorAuthority(
                    str(root), str(templates), "/unused/session.sock", {"lord"})
            self.assertTrue(Path(root, "lord").is_dir())
            self.assertFalse(Path(root, "tradewars").exists())

            disabled = DoorAuthority.__new__(DoorAuthority)
            disabled.root = Path(directory, "empty")
            disabled.enabled = set()
            disabled._initialize_legacy_worlds()

    def test_door_sys_is_52_lines_and_contains_no_secret(self) -> None:
        value = _door_sys("pilot", "Test\x1b[31m\r\nPilot", 1)
        lines = value.splitlines()
        self.assertEqual(52, len(lines))
        self.assertEqual("Test[31mPilot", lines[9])
        self.assertEqual("Mars Hotel", lines[10])
        self.assertEqual("pilot", lines[35])
        self.assertEqual("C:\\NODEINFO\\1", lines[32])
        self.assertNotIn("password", value.casefold())
        self.assertNotIn("token", value.casefold())

    def test_frames_survive_fragmentation(self) -> None:
        left, right = socket.socketpair()
        try:
            wire = struct.pack("!I", 6) + b"Ohello"

            def writer() -> None:
                for byte in wire:
                    left.send(bytes([byte]))

            thread = threading.Thread(target=writer)
            thread.start()
            self.assertEqual((b"O", b"hello"), recv_frame(right))
            thread.join()
        finally:
            left.close()
            right.close()

    def test_oversized_output_frame_is_rejected(self) -> None:
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("!I", MAX_FRAME + 2))
            with self.assertRaises(DoorError):
                recv_frame(right)
        finally:
            left.close()
            right.close()

    def test_sender_rejects_oversized_input(self) -> None:
        left, right = socket.socketpair()
        try:
            with self.assertRaises(DoorError):
                send_frame(left, b"I", b"x" * (MAX_FRAME + 1))
        finally:
            left.close()
            right.close()

    def test_unregistered_dos_doors_are_single_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for game in ("zork", "lord", "tradewars"):
                os.mkdir(os.path.join(directory, game))

            def authority() -> DoorAuthority:
                value = DoorAuthority.__new__(DoorAuthority)
                value.root = Path(directory)
                value.lock = threading.Lock()
                value.nodes = {"zork": set(), "lord": set(), "tradewars": set()}
                value.node_locks = {}
                return value

            first = authority()
            second = authority()
            self.assertEqual(1, first._allocate("lord"))
            with self.assertRaises(Exception):
                first._allocate("lord")
            with self.assertRaises(Exception):
                second._allocate("lord")
            first._release("lord", 1)
            self.assertEqual(1, second._allocate("lord"))
            second._release("lord", 1)
            Path(directory, "lord", ".registered").write_text(
                "registered\n", encoding="ascii")
            first.lord_nodes = 3
            self.assertEqual([1, 2, 3], [first._allocate("lord") for _ in range(3)])
            with self.assertRaises(Exception):
                first._allocate("lord")
            for node in (1, 2, 3):
                first._release("lord", node)
            self.assertEqual(1, first._allocate("tradewars"))
            with self.assertRaises(Exception):
                second._allocate("tradewars")
            first._release("tradewars", 1)


if __name__ == "__main__":
    unittest.main()
