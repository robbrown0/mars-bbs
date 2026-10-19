"""Pre-authentication Mars Hotel terminal welcome screen."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path


ASSET = Path(__file__).resolve().parent.parent / "assets" / "mars-welcome.txt"
MARS_ADDRESS = Path("/run/sshd/mars-address")
DOCUMENTATION_ADDRESS = "192.0.2.10"
CLEAR_SCREEN = "\033[2J\033[H"


def configured_mars_ipv4() -> str | None:
    """Return the operator-configured public Mars endpoint, never a peer IP."""
    try:
        configured = MARS_ADDRESS.read_text(encoding="ascii", errors="strict").strip()
        address = ipaddress.ip_address(configured)
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    return str(address)


def configured_mars_hostname() -> str:
    try:
        host = Path("/run/sshd/mars-hostname").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "mars.ee.msstate.edu.pl"
    return host if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) else "mars.ee.msstate.edu.pl"


def artwork(mars_ip: str | None) -> str:
    text = ASSET.read_text(encoding="ascii").rstrip("\n")
    text = text.replace("mars.ee.msstate.edu.pl", configured_mars_hostname())
    if mars_ip:
        return text.replace(DOCUMENTATION_ADDRESS, mars_ip)
    return text.replace(f" [{DOCUMENTATION_ADDRESS}]", "")


def screen_text(columns: int, rows: int, mars_ip: str | None) -> str:
    """Render the exact full artwork or a bounded small-terminal fallback."""
    if columns >= 80 and rows >= 24:
        return artwork(mars_ip)

    connected = "Connected to " + configured_mars_hostname()
    addressed = f"{connected} [{mars_ip}]" if mars_ip else connected
    if rows >= 2:
        if len(addressed) <= columns:
            return addressed
        if len(connected) <= columns:
            return connected
    if columns >= len("MARS HOTEL"):
        return "MARS HOTEL"
    return ">"[:max(columns, 0)]


def show_welcome() -> str | None:
    """Draw the landing screen and return a width-safe direct login prompt."""
    try:
        size = os.get_terminal_size(0)
    except OSError:
        size = os.terminal_size((80, 24))
    text = screen_text(size.columns, size.lines, configured_mars_ipv4())
    try:
        os.write(1, (CLEAR_SCREEN + text + "\n").encode("ascii"))
    except OSError:
        return None
    if size.columns >= len("userid ('new' for new user): "):
        return "userid ('new' for new user): "
    return "userid: "
