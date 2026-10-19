"""Unprivileged pre-authentication frontend for the forced SSH session."""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import socket
import struct
import sys

from .client import call
from .file_download import command_main
from .rpc import RPCError
from .timezones import choose_timezone
from .welcome import show_welcome

SUPERVISOR_SOCKET = "/run/mars-v2-supervisor/supervisor.sock"


def prompt_password(label: str = "Password: ") -> str:
    try:
        return getpass.getpass(label)
    except (EOFError, KeyboardInterrupt):
        return ""


def ssh_peer_address() -> str:
    """Return OpenSSH's canonical peer address, or empty when unavailable."""
    fields = os.environ.get("SSH_CONNECTION", "").split()
    if len(fields) != 4:
        return ""
    try:
        return str(ipaddress.ip_address(fields[0]))
    except ValueError:
        return ""


def handoff(ticket: str) -> int:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    conn.connect(SUPERVISOR_SOCKET)
    fds = struct.pack("3i", 0, 1, 2)
    conn.sendmsg([json.dumps({"ticket": ticket,
                              "remote_ip": ssh_peer_address()}).encode("ascii")],
                 [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
    response = json.loads(conn.recv(4096).decode("utf-8"))
    if not response.get("ok"):
        print(str(response.get("error", "Unable to start BBS session.")))
        return 1
    final = json.loads(conn.recv(4096).decode("utf-8"))
    return int(final.get("exit", 1))


def main() -> None:
    original_command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if original_command:
        raise SystemExit(command_main(original_command))
    first_prompt = show_welcome()
    if first_prompt is None:
        raise SystemExit(0)
    for _ in range(5):
        try:
            name = input(first_prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)
        if name.lower() == "new":
            try:
                new_user = input("New userid (1-12 characters): ").strip()
                display = input("Display name: ").strip()
                email = input("Email (optional, private): ").strip()
                profile = input("About you (optional, public; 240 characters): ").strip()
                timezone = choose_timezone("UTC")
                if timezone is None:
                    print("Account creation cancelled during time-zone selection.")
                    continue
                first = prompt_password("New password (12-64 characters): ")
                second = prompt_password("Retype password: ")
                if first != second:
                    print("Passwords do not match.")
                    continue
                call("auth", "register", {"userid": new_user, "display_name": display,
                                           "email": email, "profile_text": profile,
                                           "timezone": timezone,
                                           "password": first})
                print("Account created. Please log in.")
            except RPCError as exc:
                print(f"Account not created: {exc}")
            continue
        clear = prompt_password()
        try:
            result = call("auth", "login", {"userid": name, "password": clear})
            raise SystemExit(handoff(str(result["ticket"])))
        except RPCError:
            print("Invalid userid or password")
    print("Too many failed attempts.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
