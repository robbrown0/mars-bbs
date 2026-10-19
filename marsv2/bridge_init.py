"""Idempotently create per-UID copies of the local bridge authentication key."""

from __future__ import annotations

import argparse
import os
import re


def existing(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as stream:
            value = stream.read().strip()
    except FileNotFoundError:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("existing bridge secret is invalid")
    return value


def write(path: str, value: str, uid: int) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chown(directory, uid, uid)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(path, uid, uid)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--chat-uid", type=int, default=21006)
    parser.add_argument("--bridge-uid", type=int, default=21008)
    args = parser.parse_args()
    chat_path = os.path.join(args.root, "chat", "secret")
    bridge_path = os.path.join(args.root, "external", "secret")
    chat_value = existing(chat_path)
    bridge_value = existing(bridge_path)
    if chat_value and bridge_value and chat_value != bridge_value:
        raise RuntimeError("bridge secret copies do not match")
    value = chat_value or bridge_value or os.urandom(32).hex()
    if chat_value is None:
        write(chat_path, value, args.chat_uid)
    if bridge_value is None:
        write(bridge_path, value, args.bridge_uid)
    print("External bridge authentication initialized")


if __name__ == "__main__":
    main()
