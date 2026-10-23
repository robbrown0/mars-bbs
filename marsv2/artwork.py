"""Bounded local and administrator-approved MarsAmp artwork helpers."""

from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tinytag import TinyTag, TinyTagException


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MUSICBRAINZ_HOST = "musicbrainz.org"
CAA_HOST = "coverartarchive.org"
MBID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
SIDECAR_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
    "front.jpg", "front.jpeg", "front.png", "front.webp",
    "album.jpg", "album.jpeg", "album.png", "album.webp",
)


class ArtworkError(Exception):
    """A safe artwork lookup or validation failure."""


def image_type(data: bytes) -> str | None:
    """Return a browser-safe MIME type based only on trusted magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def validate_image(data: bytes) -> tuple[str, bytes]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ArtworkError("artwork is empty or exceeds the 5 MB limit")
    mime_type = image_type(data)
    if mime_type is None:
        raise ArtworkError("artwork is not a supported JPEG, PNG, WebP, or GIF image")
    return mime_type, data


def _read_bounded(filename: str) -> tuple[str, bytes]:
    try:
        size = os.stat(filename, follow_symlinks=False).st_size
        if size <= 0 or size > MAX_IMAGE_BYTES or os.path.islink(filename):
            raise ArtworkError("artwork file is empty, too large, or unsafe")
        with open(filename, "rb") as stream:
            return validate_image(stream.read(MAX_IMAGE_BYTES + 1))
    except OSError as exc:
        raise ArtworkError("artwork file is unavailable") from exc


def local_artwork(audio_filename: str) -> tuple[str, bytes, str] | None:
    """Read embedded art first, then conventional album-folder sidecars."""
    try:
        tag = TinyTag.get(
            audio_filename, tags=True, duration=False, image=True,
            check_magic_bytes=True,
        )
        image = tag.images.front_cover or tag.images.any
        if image is not None:
            mime_type, data = validate_image(image.data)
            return mime_type, data, "embedded"
    except (ArtworkError, OSError, TinyTagException, ValueError):
        pass

    directory = os.path.realpath(os.path.dirname(audio_filename))
    try:
        names = {entry.name.casefold(): entry.name for entry in os.scandir(directory)
                 if entry.is_file(follow_symlinks=False)}
    except OSError:
        return None
    for wanted in SIDECAR_NAMES:
        actual = names.get(wanted)
        if actual is None:
            continue
        candidate = os.path.realpath(os.path.join(directory, actual))
        if os.path.commonpath((directory, candidate)) != directory:
            continue
        try:
            mime_type, data = _read_bounded(candidate)
            return mime_type, data, "folder"
        except ArtworkError:
            continue
    return None


def _allowed_art_host(hostname: str) -> bool:
    return hostname in (CAA_HOST, "archive.org") or hostname.endswith(".archive.org")


class _SafeRedirect(HTTPRedirectHandler):
    """Permit only HTTPS redirects among the fixed artwork providers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urlsplit(newurl)
        hostname = (parsed.hostname or "").casefold()
        if not _allowed_art_host(hostname):
            raise ArtworkError("artwork provider redirected to an unapproved host")
        if parsed.scheme == "http":
            parsed = parsed._replace(scheme="https")
            newurl = urlunsplit(parsed)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ArtworkError("artwork provider returned an unsafe redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request_json(url: str, user_agent: str, allowed_host: str) -> dict[str, object]:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != allowed_host
            or parsed.username or parsed.password):
        raise ArtworkError("artwork metadata endpoint is invalid")
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    try:
        with build_opener(_SafeRedirect()).open(request, timeout=4.0) as response:
            if response.headers.get_content_type() != "application/json":
                raise ArtworkError("artwork provider returned an unexpected response")
            payload = response.read(MAX_JSON_BYTES + 1)
    except ArtworkError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ArtworkError("artwork provider is temporarily unavailable") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise ArtworkError("artwork provider response is too large")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtworkError("artwork provider returned invalid metadata") from exc
    if not isinstance(result, dict):
        raise ArtworkError("artwork provider returned invalid metadata")
    return result


def _credit(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("name", ""))[:120])
        parts.append(str(item.get("joinphrase", ""))[:20])
    return "".join(parts)[:180]


def _query_term(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def search_release_groups(artist: str, album: str,
                          user_agent: str) -> list[dict[str, object]]:
    """Search canonical MusicBrainz release groups for administrator review."""
    query = f'artist:"{_query_term(artist)}" AND releasegroup:"{_query_term(album)}"'
    url = "https://musicbrainz.org/ws/2/release-group/?" + urlencode({
        "query": query, "fmt": "json", "limit": "8",
    })
    payload = _request_json(url, user_agent, MUSICBRAINZ_HOST)
    releases = payload.get("release-groups", [])
    if not isinstance(releases, list):
        raise ArtworkError("MusicBrainz returned invalid release data")
    result: list[dict[str, object]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        mbid = str(release.get("id", "")).casefold()
        if not MBID_RE.fullmatch(mbid):
            continue
        result.append({
            "id": mbid,
            "title": str(release.get("title", ""))[:160],
            "artist": _credit(release.get("artist-credit")),
            "date": str(release.get("first-release-date", ""))[:16],
            "type": str(release.get("primary-type", ""))[:32],
            "score": int(release.get("score", 0)),
        })
    return result


def download_release_group_cover(mbid: str, user_agent: str) -> tuple[str, bytes]:
    """Download the canonical 1200px front cover for one release group."""
    selected = mbid.strip().casefold()
    if not MBID_RE.fullmatch(selected):
        raise ArtworkError("invalid MusicBrainz release-group identifier")
    image_url = (
        f"https://coverartarchive.org/release-group/{selected}/front-1200"
    )
    request = Request(image_url, headers={"User-Agent": user_agent, "Accept": "image/*"})
    try:
        with build_opener(_SafeRedirect()).open(request, timeout=4.0) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ArtworkError("cover image exceeds the 5 MB limit")
            data = response.read(MAX_IMAGE_BYTES + 1)
    except ArtworkError:
        raise
    except HTTPError as exc:
        if exc.code == 404:
            raise ArtworkError("this release group has no approved front cover") from exc
        raise ArtworkError("cover image download failed") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ArtworkError("cover image download failed") from exc
    return validate_image(data)
