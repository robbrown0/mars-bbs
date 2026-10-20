"""Validation and cryptographic helpers for private BBS-to-BBS mail links."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .validate import text, userid


BBS_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
INVITATION_PREFIX = "MHX1."


def bbs_id(value: object) -> str:
    value = text(value, "BBS network ID", 32, 2).strip().lower()
    if not BBS_ID_RE.fullmatch(value):
        raise ValueError(
            "BBS network ID must use 2-32 lowercase letters, numbers, or hyphens")
    return value


def bbs_name(value: object) -> str:
    return text(value, "BBS name", 64, 2).strip()


def endpoint(value: object) -> str:
    value = text(value, "federation endpoint", 255, 1).strip()
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("federation endpoint must be an HTTPS origin without credentials or a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid federation endpoint port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid federation endpoint port")
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    return f"https://{rendered_host}{':' + str(port) if port is not None else ''}"


def fingerprint(value: object) -> str:
    value = text(value, "certificate fingerprint", 64, 64).strip().lower()
    if not FINGERPRINT_RE.fullmatch(value):
        raise ValueError("invalid certificate fingerprint")
    return value


def certificate_fingerprint(path: str) -> str:
    pem = Path(path).read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def remote_address(value: object, local_bbs_id: str) -> tuple[str, str | None, str]:
    """Return (userid, peer ID or None, canonical address)."""
    value = text(value, "recipient", 45, 1).strip()
    if value.count("@") > 1:
        raise ValueError("invalid BBS mail address")
    if "@" not in value:
        name = userid(value)
        return name, None, name
    raw_user, raw_bbs = value.rsplit("@", 1)
    name = userid(raw_user)
    peer = bbs_id(raw_bbs)
    if peer == bbs_id(local_bbs_id):
        return name, None, name
    return name, peer, f"{name}@{peer}"


def generate_secret() -> bytes:
    return secrets.token_bytes(32)


def encode_secret(value: bytes) -> str:
    if len(value) != 32:
        raise ValueError("invalid peer secret")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_secret(value: object) -> bytes:
    value = text(value, "peer secret", 64, 40).strip()
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid peer secret") from exc
    if len(decoded) != 32:
        raise ValueError("invalid peer secret")
    return decoded


def encode_invitation(identity: dict[str, str], secret: bytes) -> str:
    payload = {
        "v": 1,
        "bbs_id": bbs_id(identity["bbs_id"]),
        "bbs_name": bbs_name(identity["bbs_name"]),
        "endpoint": endpoint(identity["endpoint"]),
        "certificate_sha256": fingerprint(identity["certificate_sha256"]),
        "secret": encode_secret(secret),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii")
    return INVITATION_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_invitation(value: object) -> tuple[dict[str, str], bytes]:
    value = text(value, "link invitation", 2048, len(INVITATION_PREFIX) + 40).strip()
    if not value.startswith(INVITATION_PREFIX):
        raise ValueError("invalid link invitation")
    encoded = value[len(INVITATION_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        pairs = json.loads(raw.decode("ascii"), object_pairs_hook=lambda values: values)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid link invitation") from exc
    if not isinstance(pairs, list) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in pairs):
        raise ValueError("invalid link invitation")
    payload: dict[str, object] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in payload:
            raise ValueError("invalid link invitation")
        payload[key] = item
    if set(payload) != {
            "v", "bbs_id", "bbs_name", "endpoint", "certificate_sha256", "secret"
    } or payload.get("v") != 1:
        raise ValueError("invalid link invitation")
    identity = {
        "bbs_id": bbs_id(payload["bbs_id"]),
        "bbs_name": bbs_name(payload["bbs_name"]),
        "endpoint": endpoint(payload["endpoint"]),
        "certificate_sha256": fingerprint(payload["certificate_sha256"]),
    }
    return identity, decode_secret(payload["secret"])


def _secret_path(directory: str, peer_id: str) -> Path:
    return Path(directory) / (bbs_id(peer_id) + ".key")


def write_peer_secret(directory: str, peer_id: str, value: bytes) -> None:
    if len(value) != 32:
        raise ValueError("invalid peer secret")
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    destination = _secret_path(directory, peer_id)
    temporary = root / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_peer_secret(directory: str, peer_id: str) -> bytes:
    path = _secret_path(directory, peer_id)
    status = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
        raise ValueError("peer secret has unsafe permissions")
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError("invalid peer secret")
    return value


def delete_peer_secret(directory: str, peer_id: str) -> None:
    try:
        _secret_path(directory, peer_id).unlink()
    except FileNotFoundError:
        pass


def payload_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def signature(secret: bytes, timestamp: str, nonce: str, raw: bytes) -> str:
    material = f"{timestamp}\n{nonce}\n{payload_digest(raw)}".encode("ascii")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, timestamp: str, nonce: str,
                     raw: bytes, supplied: str) -> bool:
    if not NONCE_RE.fullmatch(nonce) or not FINGERPRINT_RE.fullmatch(supplied):
        return False
    return hmac.compare_digest(signature(secret, timestamp, nonce, raw), supplied)
