"""Central validation limits for all V2 authorities."""

from __future__ import annotations

import re

USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,11}$")
ROOM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
EMAIL_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def text(value: object, name: str, maximum: int, minimum: int = 0,
         multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}")
    if len(value) < minimum or len(value) > maximum:
        raise ValueError(f"invalid {name} length")
    for ch in value:
        code = ord(ch)
        if code == 0 or code == 27 or code == 127:
            raise ValueError(f"invalid {name} character")
        if code < 32 and not (multiline and ch in "\r\n\t"):
            raise ValueError(f"invalid {name} character")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def userid(value: object) -> str:
    value = text(value, "userid", 12, 1)
    if not USER_RE.fullmatch(value):
        raise ValueError("invalid userid")
    return value.lower()


def password(value: object) -> str:
    return text(value, "password", 64, 12)


def email_address(value: object) -> str:
    value = text(value, "email address", 254).strip()
    if not value:
        return ""
    if not value.isascii() or value.count("@") != 1 or any(ch.isspace() for ch in value):
        raise ValueError("invalid email address")
    local, domain = value.rsplit("@", 1)
    if (not local or len(local) > 64 or local.startswith(".") or local.endswith(".")
            or ".." in local or not EMAIL_LOCAL_RE.fullmatch(local)):
        raise ValueError("invalid email address")
    labels = domain.split(".")
    if len(labels) < 2 or any(not EMAIL_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("invalid email address")
    return f"{local}@{domain.lower()}"


def room(value: object) -> str:
    value = text(value, "room", 24, 1)
    if not ROOM_RE.fullmatch(value):
        raise ValueError("invalid room")
    return value.lower()


def integer(value: object, name: str, minimum: int = 1,
            maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {name}")
    if value < minimum or value > maximum:
        raise ValueError(f"invalid {name}")
    return value
