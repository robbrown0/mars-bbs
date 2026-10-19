"""Restricted SSH file-transfer command.

The SSH transport account has no shell, SCP, or SFTP service.  An authenticated
BBS session instead issues a short-lived bearer ticket for one library object.
This module redeems that ticket and writes only the original object bytes to
standard output; status and errors are confined to standard error.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import sys

from .client import call
from .rpc import RPCError

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
COMMAND = re.compile(r"download ([A-Za-z0-9_-]{43})\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024


def _write_all(data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(1, data[offset:])
        if written <= 0:
            raise OSError("download output closed")
        offset += written


def download(ticket: str) -> None:
    if not TOKEN.fullmatch(ticket):
        raise ValueError("invalid download ticket")
    opened = call("file", "download_open", {"ticket": ticket})
    stream = opened.get("stream")
    expected_hash = opened.get("sha256")
    expected_size = opened.get("size")
    if (not isinstance(stream, str) or not TOKEN.fullmatch(stream) or
            not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash) or
            not isinstance(expected_size, int) or
            not 0 <= expected_size <= MAX_DOWNLOAD_BYTES):
        raise ValueError("invalid response from file service")

    digest = hashlib.sha256()
    offset = 0
    while True:
        response = call("file", "download_chunk", {
            "ticket": ticket, "stream": stream, "offset": offset})
        if response.get("offset") != offset or response.get("total") != expected_size:
            raise ValueError("file changed during download")
        encoded = response.get("data")
        if not isinstance(encoded, str):
            raise ValueError("invalid response from file service")
        chunk = base64.b64decode(encoded, validate=True)
        next_offset = response.get("next_offset")
        if (not isinstance(next_offset, int) or next_offset != offset + len(chunk) or
                next_offset > expected_size):
            raise ValueError("invalid download offset")
        digest.update(chunk)
        _write_all(chunk)
        offset = next_offset
        if response.get("eof") is True:
            break
        if not chunk:
            raise ValueError("empty partial download response")

    if offset != expected_size or digest.hexdigest() != expected_hash:
        raise ValueError("download integrity check failed")


def command_main(command: str) -> int:
    match = COMMAND.fullmatch(command)
    if match is None:
        print("Only a Mars file-download ticket may be used as a remote command.",
              file=sys.stderr)
        return 64
    try:
        download(match.group(1))
        return 0
    except BrokenPipeError:
        return 1
    except (RPCError, ValueError, OSError, binascii.Error):
        # Tickets are secrets.  Keep errors deliberately generic and never
        # echo the command or credential into an SSH client transcript.
        print("Mars file download failed: invalid, expired, or interrupted ticket.",
              file=sys.stderr)
        return 1
