"""Authenticated MarsAmp catalog, playback control, and browser audio sink.

The SSH terminal remains the control surface.  Browsers receive only opaque
track identifiers and byte streams after redeeming a short-lived pairing
ticket created by an authenticated BBS session.  Source paths and NAS
credentials never cross either public interface.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from tinytag import TinyTag, TinyTagException

from .artwork import (
    ArtworkError, download_release_group_cover, local_artwork,
    search_release_groups,
)
from .db import connect
from .identity import validate
from .rpc import RPCError, request, serve
from .validate import integer, text

PLAY_CAPABILITY = "music.play"
MANAGE_CAPABILITY = "music.manage"
PAIR_SECONDS = 90
BROWSER_SECONDS = 8 * 60 * 60
MAX_RESULTS = 250
PLAY_MODES = {"sequential", "shuffle"}
DYNAMIC_ALL = "all"
ARTWORK_CACHE_BYTES = 32 * 1024 * 1024
ARTWORK_TICKET_WINDOW = 60 * 60
ARTWORK_TICKET_LIFETIME = 9 * ARTWORK_TICKET_WINDOW
STREAM_TICKET_WINDOW = 15 * 60
STREAM_TICKET_LIFETIME = 9 * STREAM_TICKET_WINDOW
DEFAULT_ARTWORK_USER_AGENT = (
    "MarsHotelBBS/1.0 (https://github.com/robbrown0/mars-bbs)"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43,86}\Z")
SUPPORTED = {
    ".aac": "audio/aac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".alac": "audio/mp4",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    # Catalog WMA so the UI can report it honestly. Browser playback support
    # varies and the web player labels it unsupported rather than transcoding.
    ".wma": "audio/x-ms-wma",
}
SKIP_DIRECTORIES = {"#recycle", "@recycle", ".trash", ".trashes"}
PLACEHOLDER_TAGS = {
    "artist", "album", "title", "track", "unknown", "unknown artist",
    "unknown album", "untitled",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_settings (
  id INTEGER PRIMARY KEY CHECK(id=1),
  compact_lines INTEGER NOT NULL CHECK(compact_lines IN (3,4))
);
INSERT OR IGNORE INTO player_settings(id,compact_lines) VALUES(1,3);
CREATE TABLE IF NOT EXISTS playback_state (
  account_id INTEGER PRIMARY KEY,
  state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  title TEXT NOT NULL,
  artist TEXT NOT NULL DEFAULT '',
  album TEXT NOT NULL DEFAULT '',
  extension TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  modified_ns INTEGER NOT NULL,
  available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)),
  UNIQUE(source,relative_path)
);
CREATE INDEX IF NOT EXISTS tracks_search
  ON tracks(available,title,artist,album);
CREATE TABLE IF NOT EXISTS track_metadata (
  track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '',
  artist TEXT NOT NULL DEFAULT '',
  album TEXT NOT NULL DEFAULT '',
  album_artist TEXT NOT NULL DEFAULT '',
  track_number TEXT NOT NULL DEFAULT '',
  disc_number TEXT NOT NULL DEFAULT '',
  year TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL CHECK(source_type IN ('embedded','reviewed')),
  confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
  file_modified_ns INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  updated_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS track_metadata_source
  ON track_metadata(source_type,confidence);
CREATE TABLE IF NOT EXISTS track_metadata_audit (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  old_metadata_json TEXT NOT NULL,
  new_metadata_json TEXT NOT NULL,
  changed_at INTEGER NOT NULL,
  changed_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS track_metadata_audit_track
  ON track_metadata_audit(track_id,changed_at);
CREATE TABLE IF NOT EXISTS extra_sources (
  name TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_slug TEXT NOT NULL REFERENCES playlists(slug) ON DELETE CASCADE,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  position INTEGER NOT NULL,
  PRIMARY KEY(playlist_slug,track_id)
);
CREATE INDEX IF NOT EXISTS playlist_tracks_order
  ON playlist_tracks(playlist_slug,position);
CREATE TABLE IF NOT EXISTS album_artwork (
  artwork_key TEXT PRIMARY KEY,
  artist TEXT NOT NULL,
  album TEXT NOT NULL,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  image BLOB NOT NULL,
  image_sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  created_by TEXT NOT NULL
);
"""


def _safe_component(value: str, maximum: int = 160) -> str:
    """Render filesystem metadata as inert, bounded terminal/browser text."""
    cleaned = "".join(ch if 32 <= ord(ch) != 127 else "?" for ch in value)
    return cleaned[:maximum]


def _metadata(relative: str) -> tuple[str, str, str]:
    parts = Path(relative).parts
    title = _safe_component(Path(parts[-1]).stem)
    album = _safe_component(parts[-2]) if len(parts) >= 2 else ""
    artist = _safe_component(parts[-3]) if len(parts) >= 3 else ""
    return title or "Untitled", artist, album


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _music_key(value: str) -> str:
    """Normalize uneven directory-derived music metadata for curated matching."""
    ascii_value = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore").decode("ascii").casefold()
    ascii_value = re.sub(
        r"^[a-z0-9]+_\(disc_\d+\)_\d+_-_", "", ascii_value)
    ascii_value = re.sub(r"^\s*\d{1,3}(?:-\d{1,3})?\s*[-_.]+\s*", "", ascii_value)
    ascii_value = re.sub(r"^\s*\d{1,3}\s+", "", ascii_value)
    return "".join(character for character in ascii_value if character.isalnum())


def _display_title(value: str) -> str:
    """Hide filename track/disc prefixes without changing catalog identity."""
    original = _safe_component(value).strip()
    cleaned = original.replace("_", " ")
    verbose = re.fullmatch(
        r".+?\s+-\s+.+?\s+-\s+\(?\d{1,3}\)?\s+-\s+(.+)", cleaned,
    )
    if verbose:
        cleaned = verbose.group(1)
    cleaned = re.sub(r"^\s*\(disc\s+\d+\)\s*", "", cleaned,
                     count=1, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^\s*\d{1,3}(?:(?:[._]\s*)|(?:-(?!\d)\s*))+", "", cleaned, count=1,
    )
    cleaned = re.sub(
        r"^\s*(?:(?:\d{1,2}-\d{1,2})(?:\s*[-_.]\s*|\s+)"
        r"|\d{1,3}(?:\s+|\s*[._]\s*|\s+-\s*|\s*-\s+))(?=\S)",
        "", cleaned, count=1,
    ).lstrip(" -.")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lstrip("-_. ")
    return cleaned or original or "Untitled"


def _display_artist(value: str) -> str:
    """Make directory-derived artist text readable without guessing identity."""
    cleaned = _safe_component(value).replace("_", " ")
    return re.sub(r"\s+", " ", cleaned).strip(" -_.")


def _display_album(value: str) -> str:
    """Remove filename separators and non-title explicit-content markers."""
    cleaned = _safe_component(value).replace("_", " ")
    cleaned = re.sub(r"\s*[\[(]\s*explicit\s*[\])]\s*", " ", cleaned,
                     flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(" -_.")


def _tag_text(value: object, maximum: int = 160) -> str:
    """Normalize trusted tag text while preserving spelling and punctuation."""
    if value is None:
        return ""
    cleaned = unicodedata.normalize("NFC", str(value))
    cleaned = "".join(ch if 32 <= ord(ch) != 127 else " " for ch in cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()[:maximum]


CATALOG_COLUMNS = """
t.id,
CASE WHEN m.file_modified_ns=t.modified_ns AND m.title<>'' THEN m.title ELSE t.title END AS title,
CASE WHEN m.file_modified_ns=t.modified_ns AND m.artist<>'' THEN m.artist ELSE t.artist END AS artist,
CASE WHEN m.file_modified_ns=t.modified_ns AND m.album<>'' THEN m.album ELSE t.album END AS album,
t.title AS raw_title,t.artist AS raw_artist,t.album AS raw_album,
t.source,t.relative_path,t.extension,t.mime_type,t.size,t.modified_ns,t.available,
CASE WHEN m.file_modified_ns=t.modified_ns THEN m.album_artist ELSE '' END AS album_artist,
CASE WHEN m.file_modified_ns=t.modified_ns THEN m.track_number ELSE '' END AS track_number,
CASE WHEN m.file_modified_ns=t.modified_ns THEN m.disc_number ELSE '' END AS disc_number,
CASE WHEN m.file_modified_ns=t.modified_ns THEN m.year ELSE '' END AS year,
CASE WHEN m.file_modified_ns=t.modified_ns THEN m.source_type ELSE 'path' END AS metadata_source,
CASE WHEN m.file_modified_ns=t.modified_ns THEN m.confidence ELSE 25 END AS metadata_confidence
"""
TITLE_EXPR = "CASE WHEN m.file_modified_ns=t.modified_ns AND m.title<>'' THEN m.title ELSE t.title END"
ARTIST_EXPR = "CASE WHEN m.file_modified_ns=t.modified_ns AND m.artist<>'' THEN m.artist ELSE t.artist END"
ALBUM_EXPR = "CASE WHEN m.file_modified_ns=t.modified_ns AND m.album<>'' THEN m.album ELSE t.album END"
CATALOG_JOIN = " FROM tracks t LEFT JOIN track_metadata m ON m.track_id=t.id "


def _public_row(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    result = dict(row)
    result["title"] = _display_title(str(result.get("title", "")))
    result["artist"] = _display_artist(str(result.get("artist", "")))
    result["album"] = _display_album(str(result.get("album", "")))
    result.pop("relative_path", None)
    result.pop("modified_ns", None)
    result.pop("available", None)
    result.pop("raw_title", None)
    result.pop("raw_artist", None)
    result.pop("raw_album", None)
    return result


class Music:
    def __init__(self, db_path: str, session_socket: str,
                 sources: dict[str, str], public_url: str,
                 extra_root: str | None = None,
                 playlists_file: str | None = None):
        # RPC, catalog scanning, and HTTP range requests use separate threads;
        # every database access is serialized by self.lock.
        self.db = connect(db_path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.session_socket = session_socket
        self.base_sources = {name: os.path.realpath(path) for name, path in sources.items()}
        self.extra_root = os.path.realpath(extra_root) if extra_root else None
        self.public_url = public_url.rstrip("/")
        parsed = urlparse(self.public_url)
        self.base_path = parsed.path.rstrip("/")
        self.public_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.lock = threading.RLock()
        self.pair_tickets: dict[str, dict[str, object]] = {}
        self.browser_sessions: dict[str, dict[str, object]] = {}
        self.playback: dict[int, dict[str, object]] = {}
        for row in self.db.execute("SELECT account_id,state_json FROM playback_state"):
            try:
                saved = json.loads(row["state_json"])
                if not isinstance(saved, dict):
                    continue
                # Restore the selection without unexpectedly starting audio.
                saved["action"] = "pause" if saved.get("track_id") else "stop"
                saved["resume"] = True
                self.playback[int(row["account_id"])] = saved
            except (ValueError, TypeError):
                continue
        self.scan_running = False
        self.metadata_running = False
        self.last_scan = 0
        self.last_scan_result: dict[str, object] = {"tracks": 0, "errors": []}
        self.last_metadata_result: dict[str, object] = {
            "examined": 0, "updated": 0, "skipped": 0, "errors": 0,
        }
        self.artwork_user_agent = os.environ.get(
            "MARSAMP_ARTWORK_USER_AGENT", DEFAULT_ARTWORK_USER_AGENT)[:240]
        self.artwork_cache: OrderedDict[
            tuple[int, int], tuple[str, bytes, str]
        ] = OrderedDict()
        self.artwork_cache_bytes = 0
        self.artwork_signing_key = secrets.token_bytes(32)
        self.stream_signing_key = secrets.token_bytes(32)
        self.artwork_search_at = 0.0
        self.playlist_definitions = self._load_playlists(playlists_file)
        self.dynamic_playlists = {
            str(item["slug"]): str(item.get("dynamic", ""))
            for item in self.playlist_definitions if item.get("dynamic")
        }
        self._refresh_playlists()

    def player_layout(self) -> int:
        with self.lock:
            return int(self.db.execute(
                "SELECT compact_lines FROM player_settings WHERE id=1").fetchone()[0])

    def _save_playback(self, account_id: int) -> None:
        """Persist only account-local queue state, never browser authorization."""
        self.db.execute(
            "INSERT INTO playback_state(account_id,state_json) VALUES(?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET state_json=excluded.state_json",
            (account_id, json.dumps(self.playback[account_id], separators=(",", ":"))))

    @staticmethod
    def _load_playlists(filename: str | None) -> list[dict[str, object]]:
        if filename is None:
            return []
        try:
            with open(filename, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("curated playlist file is unavailable or invalid") from exc
        if not isinstance(raw, list) or len(raw) > 50:
            raise RuntimeError("curated playlist file must contain a list")
        result: list[dict[str, object]] = []
        slugs: set[str] = set()
        for position, playlist in enumerate(raw):
            if not isinstance(playlist, dict):
                raise RuntimeError("curated playlist entry is invalid")
            slug = str(playlist.get("slug", ""))
            name = str(playlist.get("name", ""))
            description = str(playlist.get("description", ""))
            tracks = playlist.get("tracks")
            dynamic = str(playlist.get("dynamic", ""))
            if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", slug)
                    or slug in slugs or not 1 <= len(name) <= 80
                    or len(description) > 240 or dynamic not in ("", DYNAMIC_ALL)
                    or (dynamic and tracks not in (None, []))
                    or (not dynamic and (not isinstance(tracks, list)
                                         or not 1 <= len(tracks) <= MAX_RESULTS))):
                raise RuntimeError("curated playlist entry is invalid")
            clean_tracks: list[dict[str, object]] = []
            for track in tracks or []:
                if not isinstance(track, dict):
                    raise RuntimeError("curated playlist track is invalid")
                title = str(track.get("title", ""))
                artists = track.get("artists", [track.get("artist", "")])
                album = str(track.get("album", ""))
                if (not 1 <= len(title) <= 160 or not isinstance(artists, list)
                        or not 1 <= len(artists) <= 8 or len(album) > 160
                        or any(not isinstance(value, str) or not value or len(value) > 160
                               for value in artists)):
                    raise RuntimeError("curated playlist track is invalid")
                clean_tracks.append({"title": title, "artists": artists, "album": album})
            result.append({"slug": slug, "name": name, "description": description,
                           "position": position, "tracks": clean_tracks,
                           "dynamic": dynamic})
            slugs.add(slug)
        return result

    @staticmethod
    def _playlist_match(track: sqlite3.Row, wanted: dict[str, object]) -> int:
        keys = set(track.keys()) if isinstance(track, sqlite3.Row) else set(track)
        if {"raw_artist", "raw_title", "raw_album"}.issubset(keys):
            # Playlist manifests were audited against this immutable identity.
            # Canonical tags improve presentation but must never retarget a
            # curated entry to a different copy or recording.
            variants = [(str(track["raw_artist"]), str(track["raw_title"]),
                         str(track["raw_album"]))]
        else:
            variants = [(str(track["artist"]), str(track["title"]),
                         str(track["album"]))]
        wanted_artists = {_music_key(str(value)) for value in wanted["artists"]}
        wanted_title = _music_key(str(wanted["title"]))
        wanted_album = _music_key(str(wanted.get("album", "")))
        scores: list[int] = []
        for artist, title, album in variants:
            if _music_key(artist) not in wanted_artists:
                continue
            presented_title = _display_title(title)
            title_key = _music_key(presented_title)
            if title_key == wanted_title:
                score = 100
            elif wanted_title in title_key:
                score = 70
            else:
                continue
            if wanted_album:
                album_key = _music_key(album)
                if album_key == wanted_album:
                    score += 30
                elif wanted_album in album_key or album_key in wanted_album:
                    score += 15
                else:
                    score -= 10
            lowered = presented_title.casefold()
            if any(word in lowered for word in
                   ("live", "demo", "tribute", "workout", "cover")):
                score -= 20
            scores.append(score)
        return max(scores, default=-1)

    def _refresh_playlists(self) -> None:
        """Resolve portable artist/title definitions against this host's catalog."""
        with self.lock:
            available = self.db.execute(
                "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN + "WHERE t.available=1"
            ).fetchall()
            by_artist: dict[str, list[sqlite3.Row]] = {}
            for row in available:
                raw_artist_key = _music_key(str(row["raw_artist"]))
                by_artist.setdefault(raw_artist_key, []).append(row)
            resolved: list[tuple[str, int, int]] = []
            for playlist in self.playlist_definitions:
                if playlist.get("dynamic"):
                    continue
                used: set[int] = set()
                for position, wanted in enumerate(playlist["tracks"]):
                    matching_rows: list[sqlite3.Row] = []
                    for artist in wanted["artists"]:
                        matching_rows.extend(by_artist.get(_music_key(str(artist)), []))
                    candidates = [(self._playlist_match(row, wanted), int(row["id"]))
                                  for row in matching_rows]
                    candidates = [item for item in candidates
                                  if item[0] >= 0 and item[1] not in used]
                    if candidates:
                        _, track_id = max(candidates, key=lambda item: (item[0], -item[1]))
                        resolved.append((str(playlist["slug"]), track_id, position))
                        used.add(track_id)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM playlist_tracks")
                self.db.execute("DELETE FROM playlists")
                self.db.executemany(
                    "INSERT INTO playlists(slug,name,description,position) VALUES(?,?,?,?)",
                    [(item["slug"], item["name"], item["description"], item["position"])
                     for item in self.playlist_definitions])
                self.db.executemany(
                    "INSERT INTO playlist_tracks(playlist_slug,track_id,position) VALUES(?,?,?)",
                    resolved)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _extra_path(self, relative: str, *, require_directory: bool = True) -> str:
        if self.extra_root is None:
            raise RPCError("additional source root is not configured")
        candidate_path = Path(relative)
        if (not relative or candidate_path.is_absolute() or ".." in candidate_path.parts
                or relative in (".", os.curdir)):
            raise RPCError("source location must be a relative directory")
        candidate = os.path.realpath(os.path.join(self.extra_root, relative))
        if os.path.commonpath((self.extra_root, candidate)) != self.extra_root:
            raise RPCError("source location is outside the approved root")
        if require_directory and not os.path.isdir(candidate):
            raise RPCError("source location is not available")
        return candidate

    def _all_sources(self) -> dict[str, str]:
        result = dict(self.base_sources)
        with self.lock:
            rows = self.db.execute(
                "SELECT name,relative_path FROM extra_sources ORDER BY name"
            ).fetchall()
        for row in rows:
            try:
                result[str(row["name"])] = self._extra_path(
                    str(row["relative_path"]), require_directory=False)
            except RPCError:
                # Invalid legacy rows remain visible to administrators as
                # unavailable but can never escape the approved mount root.
                continue
        return result

    def admin_sources(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = [
            {"name": name, "kind": "base", "available": os.path.isdir(path)}
            for name, path in self.base_sources.items()
        ]
        with self.lock:
            extra = self.db.execute(
                "SELECT name,relative_path FROM extra_sources ORDER BY name"
            ).fetchall()
        for row in extra:
            available = False
            try:
                available = os.path.isdir(self._extra_path(str(row["relative_path"])))
            except RPCError:
                pass
            rows.append({"name": str(row["name"]), "kind": "additional",
                         "location": str(row["relative_path"]),
                         "available": available})
        return rows

    def metadata_stats(self) -> dict[str, object]:
        with self.lock:
            total = int(self.db.execute(
                "SELECT count(*) FROM tracks WHERE available=1").fetchone()[0])
            rows = self.db.execute(
                "SELECT m.source_type,count(*) AS tracks FROM track_metadata m "
                "JOIN tracks t ON t.id=m.track_id "
                "WHERE t.available=1 AND m.file_modified_ns=t.modified_ns "
                "GROUP BY m.source_type"
            ).fetchall()
        counts = {str(row["source_type"]): int(row["tracks"]) for row in rows}
        tagged = sum(counts.values())
        return {"total": total, "canonical": tagged,
                "fallback": max(0, total - tagged), "sources": counts,
                "running": self.metadata_running,
                "last_result": dict(self.last_metadata_result)}

    def admin_metadata_status(self, track_id: int) -> dict[str, object]:
        with self.lock:
            raw = self.db.execute(
                "SELECT id,title,artist,album,modified_ns FROM tracks "
                "WHERE id=? AND available=1", (track_id,)).fetchone()
            current = self.db.execute(
                "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN +
                "WHERE t.id=? AND t.available=1", (track_id,)).fetchone()
        if raw is None or current is None:
            raise RPCError("track unavailable")
        raw_result = {key: raw[key] for key in ("id", "title", "artist", "album")}
        return {"raw": raw_result, "canonical": _public_row(current)}

    def admin_metadata_update(self, identity: dict[str, object], track_id: int,
                              title: str, artist: str, album: str) -> dict[str, object]:
        with self.lock:
            track = self.db.execute(
                "SELECT id,modified_ns FROM tracks WHERE id=? AND available=1",
                (track_id,),
            ).fetchone()
            if track is None:
                raise RPCError("track unavailable")
            previous = self.db.execute(
                "SELECT title,artist,album,album_artist,track_number,disc_number,year,"
                "source_type,confidence FROM track_metadata WHERE track_id=?",
                (track_id,),
            ).fetchone()
            now = int(time.time())
            by = str(identity.get("userid", "administrator"))[:64]
            new_value = {"title": title, "artist": artist, "album": album,
                         "source_type": "reviewed", "confidence": 100}
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO track_metadata(track_id,title,artist,album,album_artist,"
                    "track_number,disc_number,year,source_type,confidence,file_modified_ns,"
                    "updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(track_id) DO UPDATE SET title=excluded.title,"
                    "artist=excluded.artist,album=excluded.album,"
                    "source_type=excluded.source_type,confidence=excluded.confidence,"
                    "file_modified_ns=excluded.file_modified_ns,"
                    "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                    (track_id, title, artist, album, "", "", "", "", "reviewed", 100,
                     int(track["modified_ns"]), now, by),
                )
                self.db.execute(
                    "INSERT INTO track_metadata_audit(track_id,action,old_metadata_json,"
                    "new_metadata_json,changed_at,changed_by) VALUES(?,?,?,?,?,?)",
                    (track_id, "review", json.dumps(dict(previous) if previous else {},
                                                     separators=(",", ":")),
                     json.dumps(new_value, separators=(",", ":")), now, by),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        self._refresh_playlists()
        return self.admin_metadata_status(track_id)

    def admin_metadata_reset(self, identity: dict[str, object],
                             track_id: int) -> dict[str, object]:
        """Remove a reviewed value, then recover current embedded tags."""
        with self.lock:
            track = self.db.execute(
                "SELECT id FROM tracks WHERE id=? AND available=1", (track_id,)
            ).fetchone()
            if track is None:
                raise RPCError("track unavailable")
            previous = self.db.execute(
                "SELECT title,artist,album,album_artist,track_number,disc_number,year,"
                "source_type,confidence FROM track_metadata WHERE track_id=?",
                (track_id,),
            ).fetchone()
            if previous is None or previous["source_type"] != "reviewed":
                raise RPCError("track does not have reviewed metadata")
            now = int(time.time())
            by = str(identity.get("userid", "administrator"))[:64]
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM track_metadata WHERE track_id=?", (track_id,))
                self.db.execute(
                    "INSERT INTO track_metadata_audit(track_id,action,old_metadata_json,"
                    "new_metadata_json,changed_at,changed_by) VALUES(?,?,?,?,?,?)",
                    (track_id, "reset", json.dumps(dict(previous), separators=(",", ":")),
                     "{}", now, by),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        self.refresh_metadata()
        self._refresh_playlists()
        return self.admin_metadata_status(track_id)

    def add_source(self, name: str, relative: str) -> dict[str, object]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise RPCError("source name must be lowercase letters, numbers, dash, or underscore")
        if name in self.base_sources:
            raise RPCError("base source names are protected")
        self._extra_path(relative)
        try:
            with self.lock, self.db:
                self.db.execute(
                    "INSERT INTO extra_sources(name,relative_path,created_at) VALUES(?,?,?)",
                    (name, relative, int(time.time())))
        except sqlite3.IntegrityError as exc:
            raise RPCError("source name or location already exists") from exc
        self.start_scan()
        return {"added": True, "name": name}

    def remove_source(self, name: str) -> dict[str, object]:
        if name in self.base_sources:
            raise RPCError("base sources cannot be removed")
        with self.lock, self.db:
            cursor = self.db.execute("DELETE FROM extra_sources WHERE name=?", (name,))
            if cursor.rowcount != 1:
                raise RPCError("additional source not found")
            self.db.execute("UPDATE tracks SET available=0 WHERE source=?", (name,))
        return {"removed": True, "name": name}

    @staticmethod
    def _require(identity: dict[str, object], capability: str) -> None:
        if capability not in identity.get("capabilities", []):
            raise RPCError("music access denied")

    def _prune_tokens(self) -> None:
        now = time.monotonic()
        self.pair_tickets = {
            key: value for key, value in self.pair_tickets.items()
            if float(value["expires"]) >= now
        }
        self.browser_sessions = {
            key: value for key, value in self.browser_sessions.items()
            if float(value["expires"]) >= now
        }

    def refresh_metadata(self) -> dict[str, object]:
        """Read embedded tags without changing audio files or raw path metadata.

        Existing entries tied to the current file modification stamp are cheap
        to skip, so the first catalog campaign is comprehensive while later
        rescans only parse new or changed files. Administrator-reviewed rows
        are never silently replaced.
        """
        with self.lock:
            if self.metadata_running:
                return dict(self.last_metadata_result)
            self.metadata_running = True
            rows = self.db.execute(
                "SELECT t.id,t.source,t.relative_path,t.title,t.artist,t.album,t.modified_ns,"
                "m.source_type,m.file_modified_ns FROM tracks t "
                "LEFT JOIN track_metadata m ON m.track_id=t.id "
                "WHERE t.available=1 ORDER BY t.id"
            ).fetchall()
        sources = self._all_sources()
        examined = updated = skipped = errors = 0
        pending: list[tuple[object, ...]] = []

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            with self.lock:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    self.db.executemany(
                        "INSERT INTO track_metadata(track_id,title,artist,album,album_artist,"
                        "track_number,disc_number,year,source_type,confidence,file_modified_ns,"
                        "updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(track_id) DO UPDATE SET title=excluded.title,"
                        "artist=excluded.artist,album=excluded.album,"
                        "album_artist=excluded.album_artist,track_number=excluded.track_number,"
                        "disc_number=excluded.disc_number,year=excluded.year,"
                        "source_type=excluded.source_type,confidence=excluded.confidence,"
                        "file_modified_ns=excluded.file_modified_ns,"
                        "updated_at=excluded.updated_at,updated_by=excluded.updated_by "
                        "WHERE track_metadata.source_type='embedded'",
                        pending,
                    )
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
            pending = []

        try:
            for row in rows:
                examined += 1
                modified_ns = int(row["modified_ns"])
                if (row["file_modified_ns"] is not None
                        and int(row["file_modified_ns"]) == modified_ns):
                    skipped += 1
                    continue
                if row["source_type"] == "reviewed":
                    # A changed file invalidates the reviewed row via its stamp,
                    # but only an administrator may replace that decision.
                    skipped += 1
                    continue
                root = sources.get(str(row["source"]))
                if root is None:
                    errors += 1
                    continue
                filename = os.path.realpath(os.path.join(root, str(row["relative_path"])))
                if (os.path.commonpath((root, filename)) != root
                        or os.path.islink(filename)):
                    errors += 1
                    continue
                try:
                    tag = TinyTag.get(filename, image=False, duration=False)
                except (OSError, TinyTagException, ValueError):
                    errors += 1
                    continue
                title = _tag_text(tag.title)
                artist = _tag_text(tag.artist)
                album = _tag_text(tag.album)
                album_artist = _tag_text(tag.albumartist)
                if not any((title, artist, album, album_artist)):
                    skipped += 1
                    continue
                if any(value.casefold() in PLACEHOLDER_TAGS
                       for value in (title, artist, album) if value):
                    skipped += 1
                    continue
                title_matches = (_music_key(_display_title(str(row["title"]))) ==
                                 _music_key(_display_title(title))) if title else False
                artist_matches = (_music_key(str(row["artist"])) ==
                                  _music_key(artist)) if artist else False
                if title and artist and album:
                    confidence = 95 if title_matches and artist_matches else \
                        90 if title_matches or artist_matches else 80
                elif title and artist:
                    confidence = 85 if title_matches or artist_matches else 75
                else:
                    confidence = 65
                pending.append((
                    int(row["id"]), title, artist, album, album_artist,
                    _tag_text(tag.track, 24), _tag_text(tag.disc, 24),
                    _tag_text(tag.year, 24), "embedded", confidence, modified_ns,
                    int(time.time()), "tag-scan",
                ))
                updated += 1
                if len(pending) >= 250:
                    flush()
            flush()
            result = {"examined": examined, "updated": updated,
                      "skipped": skipped, "errors": errors}
            with self.lock:
                self.last_metadata_result = result
            return result
        finally:
            with self.lock:
                self.metadata_running = False

    def scan(self, claimed: bool = False) -> dict[str, object]:
        if not claimed:
            with self.lock:
                if self.scan_running:
                    return {"started": False, "message": "catalog scan already running"}
                self.scan_running = True
        found_total = 0
        errors: list[str] = []
        try:
            for source, root_text in self._all_sources().items():
                root = Path(root_text)
                if not root.is_dir():
                    errors.append(f"{source}: source unavailable")
                    continue
                found: list[tuple[object, ...]] = []
                try:
                    for directory, names, files in os.walk(root, followlinks=False):
                        names[:] = [name for name in names
                                    if name.casefold() not in SKIP_DIRECTORIES
                                    and not os.path.islink(os.path.join(directory, name))]
                        for name in files:
                            extension = Path(name).suffix.casefold()
                            if extension not in SUPPORTED:
                                continue
                            absolute = os.path.join(directory, name)
                            if os.path.islink(absolute):
                                continue
                            try:
                                stat = os.stat(absolute, follow_symlinks=False)
                            except OSError:
                                continue
                            relative = os.path.relpath(absolute, root)
                            if relative.startswith(".." + os.sep) or relative == "..":
                                continue
                            title, artist, album = _metadata(relative)
                            found.append((source, relative, title, artist, album,
                                          extension, SUPPORTED[extension],
                                          int(stat.st_size), int(stat.st_mtime_ns)))
                except OSError:
                    errors.append(f"{source}: scan interrupted")
                    continue
                with self.lock:
                    # An administrator may remove an additional source while
                    # this thread is walking it. Recheck under the same lock
                    # used by removal so a completed removal cannot be made
                    # available again by a stale scan result.
                    if self._all_sources().get(source) != root_text:
                        continue
                    # db.py uses SQLite autocommit mode. Without an explicit
                    # transaction, executemany commits every track separately;
                    # a large NAS catalog can then hold this lock for minutes
                    # and make health and playback requests time out.
                    self.db.execute("BEGIN IMMEDIATE")
                    try:
                        self.db.execute(
                            "UPDATE tracks SET available=0 WHERE source=?", (source,))
                        self.db.executemany(
                            "INSERT INTO tracks(source,relative_path,title,artist,album,extension,"
                            "mime_type,size,modified_ns,available) VALUES(?,?,?,?,?,?,?,?,?,1) "
                            "ON CONFLICT(source,relative_path) DO UPDATE SET title=excluded.title,"
                            "artist=excluded.artist,album=excluded.album,extension=excluded.extension,"
                            "mime_type=excluded.mime_type,size=excluded.size,"
                            "modified_ns=excluded.modified_ns,available=1",
                            found)
                        self.db.execute("COMMIT")
                    except BaseException:
                        self.db.execute("ROLLBACK")
                        raise
                found_total += len(found)
            with self.lock:
                self.last_scan = int(time.time())
            metadata = self.refresh_metadata()
            self._refresh_playlists()
            result = {"started": True, "tracks": found_total, "errors": errors,
                      "metadata": metadata}
            with self.lock:
                self.last_scan_result = result
            return result
        finally:
            with self.lock:
                self.scan_running = False

    def start_scan(self) -> dict[str, object]:
        with self.lock:
            if self.scan_running:
                return {"started": False, "message": "catalog scan already running"}
            self.scan_running = True
        threading.Thread(target=self.scan, args=(True,),
                         name="music-catalog-scan", daemon=True).start()
        return {"started": True}

    def _search(self, query: str, offset: int, limit: int) -> dict[str, object]:
        clauses = ["t.available=1"]
        parameters: list[object] = []
        if query:
            pattern = "%" + _escape_like(query) + "%"
            clauses.append("(" + TITLE_EXPR + " LIKE ? ESCAPE '\\' OR " +
                           ARTIST_EXPR + " LIKE ? ESCAPE '\\' OR " +
                           ALBUM_EXPR + " LIKE ? ESCAPE '\\')")
            parameters.extend((pattern, pattern, pattern))
        where = " WHERE " + " AND ".join(clauses)
        with self.lock:
            total = int(self.db.execute(
                "SELECT count(*)" + CATALOG_JOIN + where, parameters).fetchone()[0])
            rows = self.db.execute(
                "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN + where +
                " ORDER BY artist COLLATE NOCASE,album COLLATE NOCASE,title COLLATE NOCASE "
                "LIMIT ? OFFSET ?", parameters + [limit, offset]).fetchall()
        return {"tracks": [_public_row(row) for row in rows], "total": total,
                "offset": offset, "next_offset": offset + len(rows)}

    def playlists(self) -> list[dict[str, object]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT p.slug,p.name,p.description,count(pt.track_id) AS tracks "
                "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_slug=p.slug "
                "GROUP BY p.slug,p.name,p.description,p.position ORDER BY p.position"
            ).fetchall()
            available = int(self.db.execute(
                "SELECT count(*) FROM tracks WHERE available=1").fetchone()[0])
        result = [dict(row) for row in rows]
        for row in result:
            dynamic = self.dynamic_playlists.get(str(row["slug"]), "")
            if dynamic == DYNAMIC_ALL:
                row.update(dynamic=dynamic, tracks=available)
        return result

    def playlist_tracks(self, slug: str, offset: int, limit: int) -> dict[str, object]:
        with self.lock:
            playlist = self.db.execute(
                "SELECT slug,name,description FROM playlists WHERE slug=?", (slug,)
            ).fetchone()
            if playlist is None:
                raise RPCError("playlist not found")
            dynamic = self.dynamic_playlists.get(slug, "")
            if dynamic == DYNAMIC_ALL:
                total = int(self.db.execute(
                    "SELECT count(*) FROM tracks WHERE available=1").fetchone()[0])
                rows = self.db.execute(
                    "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN +
                    "WHERE t.available=1 ORDER BY artist COLLATE NOCASE,album COLLATE NOCASE,"
                    "title COLLATE NOCASE LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
            else:
                total = int(self.db.execute(
                    "SELECT count(*) FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
                    "WHERE pt.playlist_slug=? AND t.available=1", (slug,)
                ).fetchone()[0])
                rows = self.db.execute(
                    "SELECT " + CATALOG_COLUMNS + " "
                    "FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
                    "LEFT JOIN track_metadata m ON m.track_id=t.id "
                    "WHERE pt.playlist_slug=? AND t.available=1 "
                    "ORDER BY pt.position LIMIT ? OFFSET ?", (slug, limit, offset)
                ).fetchall()
        public_playlist = dict(playlist)
        if dynamic:
            public_playlist["dynamic"] = dynamic
        return {"playlist": public_playlist,
                "tracks": [_public_row(row) for row in rows],
                "total": total, "offset": offset, "next_offset": offset + len(rows)}

    def _public_track(self, track_id: int) -> dict[str, object]:
        with self.lock:
            row = self.db.execute(
                "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN +
                "WHERE t.id=? AND t.available=1", (track_id,)).fetchone()
        if row is None:
            raise RPCError("track unavailable")
        return _public_row(row)

    def _track_file(self, track_id: int) -> tuple[sqlite3.Row, str]:
        with self.lock:
            row = self.db.execute(
                "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN +
                "WHERE t.id=? AND t.available=1", (track_id,)).fetchone()
        sources = self._all_sources()
        if row is None or str(row["source"]) not in sources:
            raise RPCError("track unavailable")
        root = sources[str(row["source"])]
        candidate = os.path.realpath(os.path.join(root, str(row["relative_path"])))
        if os.path.commonpath((root, candidate)) != root or os.path.islink(candidate):
            raise RPCError("track unavailable")
        try:
            stat = os.stat(candidate, follow_symlinks=False)
        except OSError as exc:
            raise RPCError("track unavailable") from exc
        if not os.path.isfile(candidate) or int(stat.st_size) != int(row["size"]):
            raise RPCError("track changed; catalog refresh required")
        return row, candidate

    @staticmethod
    def _artwork_key(row: sqlite3.Row | dict[str, object]) -> str:
        artist = _music_key(str(row["artist"]))
        album = _music_key(str(row["album"]))
        return artist + "\x1f" + album if artist and album else ""

    @classmethod
    def _artwork_keys(cls, row: sqlite3.Row | dict[str, object]) -> list[str]:
        """Prefer canonical identity while retaining pre-campaign overrides."""
        keys = [cls._artwork_key(row)]
        raw = {
            "artist": row.get("raw_artist", "") if isinstance(row, dict)
            else row["raw_artist"],
            "album": row.get("raw_album", "") if isinstance(row, dict)
            else row["raw_album"],
        }
        keys.append(cls._artwork_key(raw))
        return list(dict.fromkeys(key for key in keys if key))

    def _cache_artwork(self, key: tuple[int, int], value: tuple[str, bytes, str]) -> None:
        size = len(value[1])
        if size > ARTWORK_CACHE_BYTES:
            return
        with self.lock:
            previous = self.artwork_cache.pop(key, None)
            if previous is not None:
                self.artwork_cache_bytes -= len(previous[1])
            while self.artwork_cache and self.artwork_cache_bytes + size > ARTWORK_CACHE_BYTES:
                _, removed = self.artwork_cache.popitem(last=False)
                self.artwork_cache_bytes -= len(removed[1])
            self.artwork_cache[key] = value
            self.artwork_cache_bytes += size

    def artwork(self, track_id: int) -> tuple[str, bytes, str]:
        """Return confirmed, embedded, folder, or branded fallback artwork."""
        row, filename = self._track_file(track_id)
        artwork_keys = self._artwork_keys(row)
        if artwork_keys:
            with self.lock:
                confirmed = self.db.execute(
                    "SELECT mime_type,image FROM album_artwork WHERE artwork_key IN (" +
                    ",".join("?" for _ in artwork_keys) + ") "
                    "ORDER BY CASE artwork_key WHEN ? THEN 0 ELSE 1 END LIMIT 1",
                    artwork_keys + [artwork_keys[0]],
                ).fetchone()
            if confirmed is not None:
                return str(confirmed["mime_type"]), bytes(confirmed["image"]), "confirmed"

        cache_key = (track_id, int(row["modified_ns"]))
        with self.lock:
            cached = self.artwork_cache.pop(cache_key, None)
            if cached is not None:
                self.artwork_cache[cache_key] = cached
                return cached
        found = local_artwork(filename)
        value = found if found is not None else (
            "image/png", _artwork_png(512), "marsamp",
        )
        self._cache_artwork(cache_key, value)
        return value

    def artwork_status(self, track_id: int) -> dict[str, object]:
        track = self._public_track(track_id)
        mime_type, data, source = self.artwork(track_id)
        return {"track": track, "source": source, "mime_type": mime_type,
                "bytes": len(data), "confirmed": source == "confirmed"}

    def artwork_version(self, track_id: int) -> str:
        """Return an opaque change token without exposing file metadata."""
        with self.lock:
            row = self.db.execute(
                "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN +
                "WHERE t.id=? AND t.available=1", (track_id,),
            ).fetchone()
            if row is None:
                raise RPCError("track unavailable")
            artwork_keys = self._artwork_keys(row)
            confirmed = (self.db.execute(
                "SELECT image_sha256 FROM album_artwork WHERE artwork_key IN (" +
                ",".join("?" for _ in artwork_keys) + ") "
                "ORDER BY CASE artwork_key WHEN ? THEN 0 ELSE 1 END LIMIT 1",
                artwork_keys + [artwork_keys[0]],
            ).fetchone() if artwork_keys else None)
        if confirmed is not None:
            return str(confirmed["image_sha256"])[:16]
        seed = f"{track_id}:{int(row['modified_ns'])}".encode("ascii")
        return hashlib.sha256(seed).hexdigest()[:16]

    def artwork_capability(self, track_id: int, version: str) -> str:
        """Issue an expiring, artwork-only URL for detached media clients."""
        if not re.fullmatch(r"[0-9a-f]{16}", version):
            raise RPCError("invalid artwork version")
        now = int(time.time())
        expires = ((now // ARTWORK_TICKET_WINDOW) + 9) * ARTWORK_TICKET_WINDOW
        message = f"{track_id}:{version}:{expires}".encode("ascii")
        signature = hmac.new(
            self.artwork_signing_key, message, hashlib.sha256).hexdigest()
        return (f"{self.base_path}/artwork/{track_id}/{version}/"
                f"{expires}/{signature}")

    def validate_artwork_capability(self, track_id: int, version: str,
                                    expires: int, signature: str) -> bool:
        """Validate a capability that grants access to one track image only."""
        now = int(time.time())
        if (not re.fullmatch(r"[0-9a-f]{16}", version)
                or not re.fullmatch(r"[0-9a-f]{64}", signature)
                or expires < now or expires > now + ARTWORK_TICKET_LIFETIME):
            return False
        try:
            if not hmac.compare_digest(version, self.artwork_version(track_id)):
                return False
        except RPCError:
            return False
        message = f"{track_id}:{version}:{expires}".encode("ascii")
        expected = hmac.new(
            self.artwork_signing_key, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def stream_version(self, track_id: int) -> str:
        """Return an opaque change token for one immutable catalog file."""
        with self.lock:
            row = self.db.execute(
                "SELECT size,modified_ns FROM tracks WHERE id=? AND available=1",
                (track_id,),
            ).fetchone()
        if row is None:
            raise RPCError("track unavailable")
        seed = f"{track_id}:{int(row['size'])}:{int(row['modified_ns'])}".encode("ascii")
        return hashlib.sha256(seed).hexdigest()[:16]

    def stream_capability(self, track_id: int) -> str:
        """Issue a short-lived URL usable by a detached AirPlay/Cast receiver."""
        version = self.stream_version(track_id)
        now = int(time.time())
        expires = ((now // STREAM_TICKET_WINDOW) + 8) * STREAM_TICKET_WINDOW
        message = f"{track_id}:{version}:{expires}".encode("ascii")
        signature = hmac.new(
            self.stream_signing_key, message, hashlib.sha256).hexdigest()
        return (f"{self.base_path}/stream/{track_id}/{version}/"
                f"{expires}/{signature}")

    def validate_stream_capability(self, track_id: int, version: str,
                                   expires: int, signature: str) -> bool:
        """Validate a receiver capability scoped to one unchanged track."""
        now = int(time.time())
        if (not re.fullmatch(r"[0-9a-f]{16}", version)
                or not re.fullmatch(r"[0-9a-f]{64}", signature)
                or expires < now or expires > now + STREAM_TICKET_LIFETIME):
            return False
        try:
            if not hmac.compare_digest(version, self.stream_version(track_id)):
                return False
        except RPCError:
            return False
        message = f"{track_id}:{version}:{expires}".encode("ascii")
        expected = hmac.new(
            self.stream_signing_key, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def admin_artwork_search(self, track_id: int, artist: str,
                             album: str) -> dict[str, object]:
        track = self._public_track(track_id)
        if not self._artwork_key(track):
            raise RPCError("this track needs both artist and album information")
        # MusicBrainz asks clients sharing an IP to remain at or below one
        # request per second. This path is administrator-triggered only.
        with self.lock:
            wait_for = max(0.0, 1.05 - (time.monotonic() - self.artwork_search_at))
        if wait_for:
            time.sleep(wait_for)
        with self.lock:
            self.artwork_search_at = time.monotonic()
        try:
            candidates = search_release_groups(artist, album, self.artwork_user_agent)
        except ArtworkError as exc:
            raise RPCError(str(exc)) from exc
        return {"track": track, "artist": artist, "album": album,
                "candidates": candidates}

    def admin_artwork_import(self, identity: dict[str, object], track_id: int,
                             mbid: str) -> dict[str, object]:
        track = self._public_track(track_id)
        artwork_key = self._artwork_key(track)
        if not artwork_key:
            raise RPCError("this track needs both artist and album information")
        try:
            mime_type, data = download_release_group_cover(mbid, self.artwork_user_agent)
        except ArtworkError as exc:
            raise RPCError(str(exc)) from exc
        digest = hashlib.sha256(data).hexdigest()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO album_artwork(artwork_key,artist,album,provider,external_id,"
                "mime_type,image,image_sha256,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(artwork_key) DO UPDATE SET provider=excluded.provider,"
                "external_id=excluded.external_id,mime_type=excluded.mime_type,"
                "image=excluded.image,image_sha256=excluded.image_sha256,"
                "created_at=excluded.created_at,created_by=excluded.created_by",
                (artwork_key, str(track["artist"]), str(track["album"]),
                 "MusicBrainz release group / Cover Art Archive", mbid, mime_type, data, digest,
                 int(time.time()), str(identity["userid"])),
            )
        return {"saved": True, "track": track, "source": "confirmed",
                "mime_type": mime_type, "bytes": len(data), "sha256": digest}

    def admin_artwork_remove(self, track_id: int) -> dict[str, object]:
        row, _filename = self._track_file(track_id)
        track = _public_row(row)
        artwork_keys = self._artwork_keys(row)
        if not artwork_keys:
            raise RPCError("this track has no album artwork override")
        with self.lock, self.db:
            cursor = self.db.execute(
                "DELETE FROM album_artwork WHERE artwork_key IN (" +
                ",".join("?" for _ in artwork_keys) + ")", artwork_keys)
        if cursor.rowcount < 1:
            raise RPCError("this album has no confirmed artwork override")
        return {"removed": True, "track": track}

    def _new_pair(self, ident: dict[str, object], bbs_token: str) -> dict[str, object]:
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        with self.lock:
            self._prune_tokens()
            account_id = int(ident["account_id"])
            self.pair_tickets = {
                key: value for key, value in self.pair_tickets.items()
                if int(value["account_id"]) != account_id
            }
            self.pair_tickets[digest] = {
                "expires": time.monotonic() + PAIR_SECONDS,
                "account_id": account_id,
                "userid": str(ident["userid"]),
                "bbs_token": bbs_token,
            }
        return {"url": self.public_url + "/pair/" + quote(raw),
                "expires_seconds": PAIR_SECONDS}

    def redeem_pair(self, raw: str) -> str:
        if not TOKEN_RE.fullmatch(raw):
            raise RPCError("invalid pairing ticket")
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        with self.lock:
            self._prune_tokens()
            ticket = self.pair_tickets.pop(digest, None)
            if ticket is None:
                raise RPCError("invalid or expired pairing ticket")
        response = request(self.session_socket, "create_music_browser", {},
                           str(ticket["bbs_token"]))
        if not response["ok"]:
            raise RPCError(str(response.get("data", {}).get(
                "error", "browser authorization unavailable")))
        delegated_token = response.get("data", {}).get("token")
        if not isinstance(delegated_token, str):
            raise RPCError("browser authorization unavailable")
        with self.lock:
            browser = secrets.token_urlsafe(48)
            browser_hash = hashlib.sha256(browser.encode("ascii")).hexdigest()
            ticket = dict(ticket)
            ticket["bbs_token"] = delegated_token
            ticket["expires"] = time.monotonic() + BROWSER_SECONDS
            ticket["last_seen"] = 0.0
            self.browser_sessions[browser_hash] = ticket
            return browser

    def browser_active(self, account_id: int) -> bool:
        """Return whether a browser for this account has polled recently."""
        now = time.monotonic()
        with self.lock:
            self._prune_tokens()
            return any(
                int(browser["account_id"]) == account_id
                and now - float(browser.get("last_seen", 0.0)) <= 5.0
                for browser in self.browser_sessions.values()
            )

    def browser_identity(self, raw: str) -> dict[str, object]:
        if not TOKEN_RE.fullmatch(raw):
            raise RPCError("browser authentication required")
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        with self.lock:
            self._prune_tokens()
            browser = self.browser_sessions.get(digest)
        if browser is None:
            raise RPCError("browser authentication required")
        response = request(self.session_socket, "touch", token=str(browser["bbs_token"]))
        if not response["ok"]:
            with self.lock:
                self.browser_sessions.pop(digest, None)
            raise RPCError("BBS session ended")
        ident = dict(response["data"])
        self._require(ident, PLAY_CAPABILITY)
        if int(ident["account_id"]) != int(browser["account_id"]):
            raise RPCError("browser authentication required")
        with self.lock:
            browser["expires"] = time.monotonic() + BROWSER_SECONDS
            browser["last_seen"] = time.monotonic()
        return ident

    def state(self, account_id: int) -> dict[str, object]:
        with self.lock:
            state = dict(self.playback.get(account_id, {
                "serial": 0, "action": "stop", "track_id": 0,
            }))
        track_id = int(state.get("track_id", 0))
        if track_id:
            try:
                state["track"] = self._public_track(track_id)
                state["track"]["artwork_version"] = self.artwork_version(track_id)
                state["track"]["artwork_url"] = self.artwork_capability(
                    track_id, str(state["track"]["artwork_version"]))
                state["stream"] = f"{self.base_path}/audio/{track_id}"
                state["output_stream"] = self.stream_capability(track_id)
            except RPCError:
                state.update({"action": "stop", "track_id": 0})
        state["compact_player_lines"] = self.player_layout()
        state["browser_active"] = self.browser_active(account_id)
        state.pop("queue", None)
        state.pop("shuffle_bag", None)
        state.pop("history", None)
        return state

    def _play_queue(self, raw: object, selected: int) -> list[int]:
        if raw is None:
            return [selected]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RESULTS:
            raise RPCError("invalid playback list")
        queue: list[int] = []
        for value in raw:
            track_id = integer(value, "playback list track id")
            if track_id not in queue:
                self._public_track(track_id)
                queue.append(track_id)
        if selected not in queue:
            queue.insert(0, selected)
        return queue

    @staticmethod
    def _play_mode(value: object) -> str:
        mode = text(value, "play mode", 16).strip().casefold()
        if mode not in PLAY_MODES:
            raise RPCError("invalid play mode")
        return mode

    def command(self, account_id: int, action: str, track_id: int = 0,
                queue: object = None, mode: object = None,
                scope: object = None) -> dict[str, object]:
        if action == "resume":
            with self.lock:
                previous = self.playback.get(account_id)
                if not previous or not previous.get("track_id"):
                    raise RPCError("Select a song in MarsAmp first")
                self._public_track(int(previous["track_id"]))
                previous.update(action="play", resume=True,
                                serial=int(previous.get("serial", 0)) + 1)
                self._save_playback(account_id)
            return self.state(account_id)
        if action not in ("play", "pause", "stop"):
            raise RPCError("invalid playback command")
        if action == "play":
            self._public_track(track_id)
        with self.lock:
            previous = self.playback.get(account_id, {
                "serial": 0, "track_id": 0, "queue": [], "mode": "sequential",
            })
            selected = track_id if action == "play" else int(previous.get("track_id", 0))
            if action == "stop":
                selected = 0
            selected_queue = (self._play_queue(queue, track_id) if action == "play"
                              else list(previous.get("queue", [])))
            selected_mode = (self._play_mode(mode) if mode is not None
                             else str(previous.get("mode", "sequential")))
            selected_scope = (text(scope, "playback scope", 48).strip().casefold()
                              if action == "play" and scope else
                              str(previous.get("scope", "")) if action != "play" else "")
            if selected_scope:
                if self.dynamic_playlists.get(selected_scope) != DYNAMIC_ALL:
                    raise RPCError("invalid playback scope")
                selected_queue = [selected]
                selected_mode = "shuffle"
            shuffle_bag = (self._shuffled(selected_queue, selected)
                           if action == "play" and selected_mode == "shuffle"
                           and not selected_scope else
                           list(previous.get("shuffle_bag", [])) if action == "pause" else [])
            self.playback[account_id] = {
                "serial": int(previous.get("serial", 0)) + 1,
                "action": action, "track_id": selected,
                "queue": selected_queue, "mode": selected_mode,
                "scope": selected_scope,
                "shuffle_bag": shuffle_bag,
                "history": [] if action == "play" else list(previous.get("history", [])),
            }
            self._save_playback(account_id)
        return self.state(account_id)

    @staticmethod
    def _shuffled(queue: list[int], current: int) -> list[int]:
        """Return a cryptographically randomized no-repeat cycle."""
        bag = [value for value in queue if value != current]
        for position in range(len(bag) - 1, 0, -1):
            target = secrets.randbelow(position + 1)
            bag[position], bag[target] = bag[target], bag[position]
        return bag

    def set_mode(self, account_id: int, mode: object) -> dict[str, object]:
        selected_mode = self._play_mode(mode)
        with self.lock:
            state = self.playback.setdefault(account_id, {
                "serial": 0, "action": "stop", "track_id": 0, "queue": [],
                "mode": "sequential",
            })
            if state.get("scope") and selected_mode != "shuffle":
                selected_mode = "shuffle"
            state["mode"] = selected_mode
            state["shuffle_bag"] = (self._shuffled(
                [int(value) for value in state.get("queue", [])],
                int(state.get("track_id", 0))) if selected_mode == "shuffle" else [])
            self._save_playback(account_id)
        return self.state(account_id)

    def _available_queue(self, raw: object) -> list[int]:
        queue = [int(value) for value in raw if isinstance(value, int)] if isinstance(raw, list) else []
        if not queue:
            return []
        placeholders = ",".join("?" for _ in queue)
        available = {int(row[0]) for row in self.db.execute(
            f"SELECT id FROM tracks WHERE available=1 AND id IN ({placeholders})", queue)}
        return [value for value in queue if value in available]

    def _queue_tracks(self, track_ids: list[int]) -> list[dict[str, object]]:
        if not track_ids:
            return []
        placeholders = ",".join("?" for _ in track_ids)
        rows = self.db.execute(
            "SELECT " + CATALOG_COLUMNS + CATALOG_JOIN +
            f"WHERE t.available=1 AND t.id IN ({placeholders})", track_ids).fetchall()
        by_id = {int(row["id"]): _public_row(row) for row in rows}
        return [by_id[track_id] for track_id in track_ids if track_id in by_id]

    def queue_view(self, account_id: int) -> dict[str, object]:
        """Return a path-free browser view of the current playback order."""
        with self.lock:
            current = dict(self.playback.get(account_id, {}))
            track_id = int(current.get("track_id", 0))
            scope = str(current.get("scope", ""))
            history = [int(value) for value in current.get("history", [])
                       if isinstance(value, int)]
            current_rows = self._queue_tracks([track_id]) if track_id else []
            current_track = current_rows[0] if current_rows else None
            if self.dynamic_playlists.get(scope) == DYNAMIC_ALL:
                total = int(self.db.execute(
                    "SELECT count(*) FROM tracks WHERE available=1").fetchone()[0])
                playlist = self.db.execute(
                    "SELECT name FROM playlists WHERE slug=?", (scope,)).fetchone()
                return {
                    "dynamic": DYNAMIC_ALL,
                    "label": str(playlist["name"]) if playlist else "Shuffle Everything",
                    "mode": "shuffle", "total": total,
                    "current": current_track,
                    "recent": list(reversed(self._queue_tracks(history[-12:]))),
                    "up_next": [],
                }
            queue = self._available_queue(current.get("queue", []))
            mode = str(current.get("mode", "sequential"))
            if mode == "shuffle":
                bag = [int(value) for value in current.get("shuffle_bag", [])
                       if isinstance(value, int) and int(value) in queue
                       and int(value) != track_id]
                upcoming = list(reversed(bag))
            elif track_id in queue:
                position = queue.index(track_id)
                upcoming = queue[position + 1:] + queue[:position]
            else:
                upcoming = queue
            return {
                "dynamic": "", "label": "Current queue", "mode": mode,
                "total": len(queue),
                "current": current_track,
                "recent": list(reversed(self._queue_tracks(history[-12:]))),
                "up_next": self._queue_tracks(upcoming),
            }

    def _random_available(self, excluded: list[int]) -> int:
        unique = list(dict.fromkeys(value for value in excluded if value > 0))[-101:]
        where = "available=1"
        parameters: list[object] = []
        if unique:
            where += " AND id NOT IN (" + ",".join("?" for _ in unique) + ")"
            parameters.extend(unique)
        row = self.db.execute(
            "SELECT id FROM tracks WHERE " + where + " ORDER BY random() LIMIT 1",
            parameters).fetchone()
        if row is None:
            row = self.db.execute(
                "SELECT id FROM tracks WHERE available=1 ORDER BY random() LIMIT 1"
            ).fetchone()
        if row is None:
            raise RPCError("music catalog is empty")
        return int(row["id"])

    def _move(self, account_id: int, backwards: bool = False) -> dict[str, object]:
        with self.lock:
            current = self.playback.get(account_id)
            if not current or current.get("action") not in ("play", "pause"):
                return self.state(account_id)
            if self.dynamic_playlists.get(str(current.get("scope", ""))) == DYNAMIC_ALL:
                track_id = int(current.get("track_id", 0))
                history = [int(value) for value in current.get("history", [])
                           if isinstance(value, int)]
                if backwards and history:
                    selected = history.pop()
                else:
                    selected = self._random_available(history + [track_id])
                    if track_id:
                        history.append(track_id)
                current.update({
                    "serial": int(current.get("serial", 0)) + 1,
                    "action": "play", "resume": False, "track_id": selected,
                    "queue": [selected], "mode": "shuffle", "shuffle_bag": [],
                    "history": history[-MAX_RESULTS:],
                })
                self._save_playback(account_id)
                return self.state(account_id)
            queue = self._available_queue(current.get("queue", []))
            if not queue:
                return self.command(account_id, "stop")
            track_id = int(current.get("track_id", 0))
            history = [int(value) for value in current.get("history", [])
                       if int(value) in queue]
            mode = str(current.get("mode", "sequential"))
            if backwards and history:
                selected = history.pop()
            elif backwards:
                try:
                    selected = queue[(queue.index(track_id) - 1) % len(queue)]
                except ValueError:
                    selected = queue[-1]
            elif mode == "shuffle" and len(queue) > 1:
                bag = [int(value) for value in current.get("shuffle_bag", [])
                       if int(value) in queue and int(value) != track_id]
                if not bag:
                    bag = self._shuffled(queue, track_id)
                selected = bag.pop()
                current["shuffle_bag"] = bag
                if track_id in queue:
                    history.append(track_id)
            else:
                try:
                    selected = queue[(queue.index(track_id) + 1) % len(queue)]
                except ValueError:
                    selected = queue[0]
                if track_id in queue:
                    history.append(track_id)
            current.update({
                "serial": int(current.get("serial", 0)) + 1,
                "action": "play", "resume": False, "track_id": selected,
                "queue": queue, "history": history[-MAX_RESULTS:],
            })
            self._save_playback(account_id)
        return self.state(account_id)

    def advance(self, account_id: int) -> dict[str, object]:
        """Advance a browser sink within its terminal-selected playback list."""
        return self._move(account_id)

    def previous(self, account_id: int) -> dict[str, object]:
        return self._move(account_id, backwards=True)

    def handle(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = str(req["op"])
        if op == "health":
            with self.lock:
                tracks = int(self.db.execute(
                    "SELECT count(*) FROM tracks WHERE available=1").fetchone()[0])
            return {"status": "ok", "tracks": tracks, "scan_running": self.scan_running}
        ident = validate(self.session_socket, req.get("token"), peer[1])
        self._require(ident, PLAY_CAPABILITY)
        data = req["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        if op == "stats":
            health = self.handle({"op": "health"}, peer)
            with self.lock:
                artwork_overrides = int(self.db.execute(
                    "SELECT count(*) FROM album_artwork").fetchone()[0])
            health.update({"sources": list(self._all_sources()), "last_scan": self.last_scan,
                           "last_scan_result": self.last_scan_result,
                           "compact_player_lines": self.player_layout(),
                           "artwork_overrides": artwork_overrides,
                           "metadata": self.metadata_stats()})
            return health
        if op == "admin_player_layout":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"lines"}:
                raise RPCError("invalid player layout request")
            lines = integer(data["lines"], "player lines", 3, 4)
            with self.lock:
                self.db.execute("UPDATE player_settings SET compact_lines=? WHERE id=1", (lines,))
            return {"compact_player_lines": lines}
        if op == "admin_sources":
            self._require(ident, MANAGE_CAPABILITY)
            return {"sources": self.admin_sources()}
        if op == "admin_add_source":
            self._require(ident, MANAGE_CAPABILITY)
            name = text(data.get("name"), "source name", 32).strip().casefold()
            relative = text(data.get("location"), "source location", 240).strip()
            return self.add_source(name, relative)
        if op == "admin_remove_source":
            self._require(ident, MANAGE_CAPABILITY)
            name = text(data.get("name"), "source name", 32).strip().casefold()
            return self.remove_source(name)
        if op == "admin_artwork_status":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id"}:
                raise RPCError("invalid artwork status request")
            return self.artwork_status(integer(data["track_id"], "track id"))
        if op == "admin_artwork_search":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id", "artist", "album"}:
                raise RPCError("invalid artwork search request")
            track_id = integer(data["track_id"], "track id")
            artist = text(data["artist"], "artwork artist", 160, 1).strip()
            album = text(data["album"], "artwork album", 160, 1).strip()
            return self.admin_artwork_search(track_id, artist, album)
        if op == "admin_artwork_import":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id", "mbid", "confirm"} \
                    or data.get("confirm") != "IMPORT":
                raise RPCError("invalid artwork import confirmation")
            track_id = integer(data["track_id"], "track id")
            mbid = text(data["mbid"], "MusicBrainz release", 36, 36).casefold()
            return self.admin_artwork_import(ident, track_id, mbid)
        if op == "admin_artwork_remove":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id", "confirm"} or data.get("confirm") != "REMOVE":
                raise RPCError("invalid artwork removal confirmation")
            return self.admin_artwork_remove(integer(data["track_id"], "track id"))
        if op == "admin_metadata_status":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id"}:
                raise RPCError("invalid metadata status request")
            return self.admin_metadata_status(integer(data["track_id"], "track id"))
        if op == "admin_metadata_update":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id", "title", "artist", "album", "confirm"} \
                    or data.get("confirm") != "SAVE":
                raise RPCError("invalid metadata update confirmation")
            return self.admin_metadata_update(
                ident, integer(data["track_id"], "track id"),
                text(data["title"], "title", 160, 1).strip(),
                text(data["artist"], "artist", 160, 1).strip(),
                text(data["album"], "album", 160).strip(),
            )
        if op == "admin_metadata_reset":
            self._require(ident, MANAGE_CAPABILITY)
            if set(data) != {"track_id", "confirm"} or data.get("confirm") != "RESET":
                raise RPCError("invalid metadata reset confirmation")
            return self.admin_metadata_reset(
                ident, integer(data["track_id"], "track id"))
        if op == "search":
            query = text(data.get("query", ""), "search", 80).strip()
            offset = integer(data.get("offset", 0), "offset", 0, 10_000_000)
            limit = integer(data.get("limit", 20), "limit", 1, MAX_RESULTS)
            return self._search(query, offset, limit)
        if op == "playlists":
            if data:
                raise RPCError("invalid playlists request")
            return {"playlists": self.playlists()}
        if op == "playlist_tracks":
            if set(data) - {"slug", "offset", "limit"}:
                raise RPCError("invalid playlist request")
            slug = text(data.get("slug"), "playlist", 48).strip().casefold()
            offset = integer(data.get("offset", 0), "offset", 0, 10_000_000)
            limit = integer(data.get("limit", 20), "limit", 1, MAX_RESULTS)
            return self.playlist_tracks(slug, offset, limit)
        if op == "pair":
            if data:
                raise RPCError("invalid pairing request")
            return self._new_pair(ident, str(req["token"]))
        if op == "status":
            return self.state(int(ident["account_id"]))
        if op == "queue":
            if data:
                raise RPCError("invalid queue request")
            return self.queue_view(int(ident["account_id"]))
        if op in ("play", "pause", "stop", "resume"):
            track_id = 0
            if op == "play":
                track_id = integer(data.get("id"), "track id")
            elif data:
                raise RPCError("invalid playback request")
            account_id = int(ident["account_id"])
            result = self.command(account_id, op, track_id,
                                  data.get("queue"), data.get("mode"), data.get("scope"))
            if op in ("play", "resume") and not self.browser_active(account_id):
                result["pair_url"] = self._new_pair(
                    ident, str(req["token"]))["url"]
            return result
        if op == "mode":
            if set(data) != {"mode"}:
                raise RPCError("invalid mode request")
            return self.set_mode(int(ident["account_id"]), data["mode"])
        if op in ("advance", "previous"):
            if data:
                raise RPCError("invalid playback request")
            account_id = int(ident["account_id"])
            result = (self.advance(account_id) if op == "advance"
                      else self.previous(account_id))
            if result.get("action") == "play" and not self.browser_active(account_id):
                result["pair_url"] = self._new_pair(ident, str(req["token"]))["url"]
            return result
        if op == "rescan":
            self._require(ident, MANAGE_CAPABILITY)
            if data:
                raise RPCError("invalid scan request")
            return self.start_scan()
        raise RPCError("unknown operation")


def _artwork_svg() -> bytes:
    return b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><radialGradient id="space"><stop stop-color="#1d2442"/><stop offset="1" stop-color="#080914"/></radialGradient>
<linearGradient id="mars" x2="1" y2="1"><stop stop-color="#ffb057"/><stop offset=".52" stop-color="#dc5745"/><stop offset="1" stop-color="#702d4d"/></linearGradient>
<filter id="glow"><feGaussianBlur stdDeviation="7" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
<rect width="512" height="512" rx="72" fill="url(#space)"/><g fill="#39e69d" opacity=".25">
<circle cx="52" cy="80" r="2"/><circle cx="428" cy="66" r="3"/><circle cx="467" cy="177" r="2"/><circle cx="74" cy="384" r="3"/><circle cx="404" cy="424" r="2"/></g>
<circle cx="256" cy="216" r="126" fill="url(#mars)" filter="url(#glow)"/><path d="M153 205c64-27 140-27 206 0M158 251c58 21 136 21 196 0" fill="none" stroke="#ffca78" stroke-width="12" opacity=".45"/>
<path d="M97 350h318" stroke="#2d9cff" stroke-width="5"/><g fill="#39e69d">
<rect x="112" y="331" width="14" height="19"/><rect x="139" y="310" width="14" height="40"/><rect x="166" y="321" width="14" height="29"/><rect x="193" y="294" width="14" height="56"/><rect x="220" y="314" width="14" height="36"/><rect x="247" y="282" width="14" height="68"/><rect x="274" y="306" width="14" height="44"/><rect x="301" y="288" width="14" height="62"/><rect x="328" y="318" width="14" height="32"/><rect x="355" y="303" width="14" height="47"/><rect x="382" y="326" width="14" height="24"/></g>
<text x="256" y="406" fill="#f8fbff" font-family="monospace" font-size="49" font-weight="700" text-anchor="middle" letter-spacing="5">MARSAMP</text>
<text x="256" y="445" fill="#2d9cff" font-family="monospace" font-size="18" text-anchor="middle" letter-spacing="6">THE MARS HOTEL</text></svg>"""


def _artwork_png(size: int) -> bytes:
    if size not in (192, 512):
        raise ValueError("unsupported artwork size")
    filename = Path(__file__).resolve().parents[1] / "assets" / f"marsamp-artwork-{size}.png"
    return filename.read_bytes()


def _player_manifest(base_path: str) -> bytes:
    return json.dumps({
        "name": "MarsAmp at The Mars Hotel", "short_name": "MarsAmp",
        "description": "Authenticated music from The Mars Hotel BBS",
        "start_url": (base_path or "/") + "/" if base_path else "/",
        "scope": (base_path or "/") + "/" if base_path else "/",
        "display": "standalone", "background_color": "#080914",
        "theme_color": "#11162a",
        "icons": [
            {"src": (base_path or "") + "/artwork-192.png",
             "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": (base_path or "") + "/artwork-512.png",
             "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, separators=(",", ":")).encode("ascii")


def _player_html(base_path: str) -> bytes:
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#11162a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MarsAmp">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="artwork-192.png" type="image/png">
<link rel="apple-touch-icon" href="artwork-192.png">
<title>MarsAmp • The Mars Hotel</title>
<style>
:root{color-scheme:dark;--green:#39e69d;--blue:#47a9ff;--amber:#ffc65a;--ink:#080914;
--panel:#11162a;--panel2:#171e36;--line:#2a3659;--muted:#9aa7c2;--white:#f6f8ff}
*{box-sizing:border-box} html{background:var(--ink)} body{margin:0;min-height:100vh;min-height:100dvh;
background:radial-gradient(circle at 50% -10%,#27345e 0,#11152a 38%,#080914 80%);
color:var(--white);font:15px ui-monospace,SFMono-Regular,Consolas,monospace;padding:max(18px,env(safe-area-inset-top)) 14px max(22px,env(safe-area-inset-bottom))}
main{width:min(100%,720px);margin:auto;border:1px solid #3b4d7a;border-radius:24px;overflow:hidden;
background:linear-gradient(155deg,rgba(25,33,60,.98),rgba(10,12,25,.98));box-shadow:0 24px 80px #000b,0 0 0 1px #47a9ff20 inset}
header{display:flex;align-items:center;justify-content:space-between;padding:18px 20px;border-bottom:1px solid var(--line)}
.brand{color:var(--blue);font-weight:900;letter-spacing:.16em;font-size:1.18rem}.live{color:var(--green);font-size:.72rem;letter-spacing:.12em;border:1px solid #39e69d55;border-radius:999px;padding:6px 10px}
.stage{display:grid;grid-template-columns:190px 1fr;gap:24px;padding:24px}.art{aspect-ratio:1;border-radius:20px;background:#0c1020;box-shadow:0 14px 34px #0009,0 0 22px #2d9cff35;border:1px solid #5870a4;overflow:hidden}.art img{display:block;width:100%;height:100%;object-fit:cover}
.now{min-width:0;display:flex;flex-direction:column;justify-content:center}.eyebrow{color:var(--green);letter-spacing:.14em;font-size:.72rem;margin-bottom:10px}.title{font:800 clamp(1.35rem,4vw,2.2rem)/1.12 system-ui,sans-serif;overflow-wrap:anywhere}.artist{margin-top:9px;color:#cbd4e9;font:600 1rem system-ui,sans-serif}.album{margin-top:5px;color:var(--muted);font:.88rem system-ui,sans-serif}
.timeline{padding:0 24px 6px}.time-row{display:flex;align-items:center;gap:12px;color:var(--muted);font-size:.75rem}.time-row output{width:46px}.time-row output:last-child{text-align:right}input[type=range]{width:100%;accent-color:var(--green)}
.meter{height:36px;display:flex;gap:4px;align-items:end;margin:10px 0}.meter i{flex:1;max-width:18px;background:linear-gradient(var(--green),var(--blue));height:12%;border-radius:3px 3px 0 0;transition:height .2s}
.controls{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;padding:14px 24px 20px}button{appearance:none;border:1px solid #465b8d;border-radius:13px;background:#171f39;color:var(--white);font:700 .82rem ui-monospace,monospace;padding:13px 8px;min-height:48px;box-shadow:0 5px 14px #0005;touch-action:manipulation}button:active{transform:translateY(1px);background:#223052}button.primary{color:#07120e;background:linear-gradient(135deg,#55f0ad,#2dcf82);border-color:#8dffd0;font-size:1rem}button.danger{color:#ffc7c7}button.attention{animation:pulse 1.2s steps(2,end) infinite}button.connected{color:#07120e;background:linear-gradient(135deg,#55f0ad,#47a9ff);border-color:#b7ffe2}
@keyframes pulse{50%{box-shadow:0 0 24px #ffc65a;background:var(--amber)}}.status{margin:0 24px 12px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:#0b0f20;color:var(--amber);min-height:44px;line-height:1.35}.utility{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 24px 14px}.utility-actions{display:flex;align-items:center;gap:4px}.mode{min-width:0;color:var(--blue);font-size:.76rem;letter-spacing:.08em}.output,.shuffle{position:relative;display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:38px;padding:6px 9px;border-color:transparent;border-radius:999px;background:transparent;box-shadow:none;color:var(--muted);font-size:.7rem;letter-spacing:.09em}.output:hover,.output:focus-visible,.shuffle:hover,.shuffle:focus-visible{color:var(--white);border-color:#465b8d;background:#171f39}.output:focus-visible,.shuffle:focus-visible{outline:2px solid var(--blue);outline-offset:2px}.output:disabled,.shuffle:disabled{opacity:.42}.cast-glyph,.shuffle-glyph{width:25px;height:25px;flex:0 0 25px;display:grid;place-items:center;color:var(--blue)}.cast-glyph svg,.shuffle-glyph svg{display:block;width:25px;height:25px;overflow:visible}.shuffle.active{color:var(--green);border-color:#39e69d55;background:#39e69d0d}.shuffle.active .shuffle-glyph{color:var(--green)}.output.connected{color:var(--green);border-color:#39e69d55;background:#39e69d0d}.output.connected .cast-glyph{color:var(--green)}.output.connected::after{content:'';position:absolute;right:5px;top:5px;width:6px;height:6px;border:2px solid #11162a;border-radius:50%;background:var(--green);box-shadow:0 0 8px #39e69dcc}
details{border-top:1px solid var(--line);background:#0b0f1d}summary{cursor:pointer;list-style:none;padding:17px 22px;color:var(--green);font-weight:800;letter-spacing:.08em}summary::-webkit-details-marker{display:none}summary::after{content:'+';float:right;color:var(--blue);font-size:1.25rem}details[open] summary::after{content:'–'}.queue-meta{padding:0 22px 12px;color:var(--muted);font-size:.8rem}.queue{list-style:none;margin:0;padding:0 14px 18px;max-height:40vh;overflow:auto;overscroll-behavior:contain}.queue li{padding:11px 10px;border-top:1px solid #202945;display:grid;grid-template-columns:32px 1fr;gap:10px}.queue .number{color:var(--blue);text-align:right}.queue strong{display:block;color:var(--white);font:650 .9rem system-ui,sans-serif}.queue span{color:var(--muted);font: .78rem system-ui,sans-serif}.queue .current strong{color:var(--green)}
@media(max-width:560px){body{padding-left:8px;padding-right:8px}.stage{grid-template-columns:112px 1fr;gap:16px;padding:18px}.controls{padding-left:14px;padding-right:14px;gap:6px}.controls button{font-size:.72rem;padding:10px 4px}.timeline{padding-left:16px;padding-right:16px}.status{margin-left:16px;margin-right:16px}.utility{padding-left:16px;padding-right:16px}.brand{font-size:1rem}.output,.shuffle{padding-left:7px;padding-right:7px}.output-label,.shuffle-label{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
@media(max-width:390px){.stage{grid-template-columns:88px 1fr}.controls{grid-template-columns:repeat(3,1fr)}.controls .primary{grid-column:auto}.title{font-size:1.18rem}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body><main>
<header><div class="brand">MARSAMP</div><div class="live">● SECURE</div></header>
<section class="stage"><div class="art"><img id="cover" src="artwork-512.png" alt="Current album artwork"></div><div class="now">
<div class="eyebrow">NOW TRANSMITTING</div><div id="title" class="title">Waiting for a selection…</div>
<div id="artist" class="artist">The Mars Hotel BBS</div><div id="album" class="album">Authenticated private player</div>
<div class="meter" id="meter" aria-hidden="true"></div></div></section>
<section class="timeline"><div class="time-row"><output id="elapsed">0:00</output><input id="seek" type="range" min="0" max="1000" value="0" aria-label="Playback position"><output id="duration">0:00</output></div></section>
<section class="controls" aria-label="Playback controls">
<button id="previous" type="button" aria-label="Previous track">◀ BACK</button>
<button id="pause" type="button" aria-label="Pause">Ⅱ PAUSE</button>
<button id="enable" class="primary" type="button" aria-label="Play">▶ PLAY</button>
<button id="stop" class="danger" type="button" aria-label="Stop">■ STOP</button>
<button id="next" type="button" aria-label="Next track">NEXT ▶</button></section>
<div id="hint" class="status" role="status">Attempting to start audio automatically.</div>
<section class="utility" aria-label="Playback options"><div id="mode" class="mode">PLAY MODE: SEQUENTIAL</div>
<div class="utility-actions"><button id="shuffle" class="shuffle" type="button" title="Turn shuffle on" aria-label="Turn shuffle on" aria-pressed="false"><span class="shuffle-glyph" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M4 7h2.2c4.7 0 6.2 10 11 10H20M17 14l3 3-3 3M4 17h2.2c1.8 0 3.1-1.5 4.3-3.4M14 8.8C15 7.7 16 7 17.2 7H20M17 4l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span id="shuffle-label" class="shuffle-label">SHUFFLE</span></button>
<button id="output" class="output" type="button" title="Choose an AirPlay or Cast speaker" aria-label="Choose an AirPlay or Cast speaker"><span id="output-label" class="output-label">OUTPUT</span><span class="cast-glyph" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M4 4h15.5A1.5 1.5 0 0 1 21 5.5v11a1.5 1.5 0 0 1-1.5 1.5H14" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 15.5a4.5 4.5 0 0 1 4.5 4.5M4 11.5a8.5 8.5 0 0 1 8.5 8.5" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/><circle cx="4" cy="20" r="1.35" fill="currentColor"/></svg></span></button></div></section>
<details id="queue-panel"><summary id="queue-summary">QUEUE · loading</summary><div id="queue-meta" class="queue-meta"></div><ol id="queue" class="queue"></ol></details>
<audio id="audio" preload="metadata" autoplay playsinline x-webkit-airplay="allow"></audio>
</main><script>
const BASE=__BASE__;const ART192=new URL(BASE+'/artwork-192.png',location.origin).href;const ART512=new URL(BASE+'/artwork-512.png',location.origin).href;
const audio=document.getElementById('audio'),meter=document.getElementById('meter');
const enable=document.getElementById('enable'),hint=document.getElementById('hint');
const mode=document.getElementById('mode'),titleEl=document.getElementById('title');
const artistEl=document.getElementById('artist'),albumEl=document.getElementById('album');
const seek=document.getElementById('seek'),elapsed=document.getElementById('elapsed');
const durationEl=document.getElementById('duration'),queueEl=document.getElementById('queue');
const queueSummary=document.getElementById('queue-summary'),queueMeta=document.getElementById('queue-meta');
const output=document.getElementById('output'),outputLabel=document.getElementById('output-label');
const shuffle=document.getElementById('shuffle'),shuffleLabel=document.getElementById('shuffle-label');
const coverEl=document.getElementById('cover');coverEl.onerror=()=>{if(coverEl.src!==ART512)coverEl.src=ART512};
let enabled=false,serial=-1,currentTrack=null,serverAction='stop',currentMode='sequential',modeLocked=false,metadataKey='',mediaArtworkData='',mediaArtworkType='',localStream='',outputStream='',switchingOutput=false;
for(let i=0;i<18;i++)meter.appendChild(document.createElement('i'));
const text=value=>String(value||'').replace(/[<>]/g,'');
function clock(value){if(!Number.isFinite(value))return '0:00';let seconds=Math.max(0,Math.floor(value));return Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0')}
function bars(on){[...meter.children].forEach((bar,i)=>bar.style.height=on?(18+((Date.now()/145+i*31)%80))+'%':'10%')}
function showHint(message){hint.hidden=false;hint.textContent=message}
function hideHint(){hint.hidden=true;hint.textContent=''}
function needsTap(){enabled=false;enable.classList.add('attention');showHint('Your browser blocked automatic audio. Tap PLAY once.')}
function audioReady(){enabled=true;enable.classList.remove('attention');hideHint()}
async function beginPlayback(){if(!audio.src){showHint('Choose a song in the BBS or queue first.');return}try{await audio.play();audioReady()}catch(error){needsTap()}}
function sessionEnded(){audio.pause();audio.removeAttribute('src');audio.load();metadataKey='';mediaArtworkData='';mediaArtworkType='';localStream='';outputStream='';titleEl.textContent='Session ended';artistEl.textContent='Pair MarsAmp again from the BBS';albumEl.textContent='';showHint('Playback authorization ended.');bars(false);setMediaState('none')}
function setMediaState(value){if('mediaSession' in navigator){try{navigator.mediaSession.playbackState=value}catch(error){}}}
function remoteConnected(){return Boolean(audio.webkitCurrentPlaybackTargetIsWireless||(audio.remote&&audio.remote.state!=='disconnected'))}
function absolute(source){return source?new URL(source,location.origin).href:''}
function sameSource(source){return Boolean(source)&&audio.src===absolute(source)}
function replaceSource(source,position,wasPlaying){if(!source||sameSource(source))return;switchingOutput=true;audio.src=source;const restore=()=>{if(Number.isFinite(position)&&position>0&&Number.isFinite(audio.duration))audio.currentTime=Math.min(position,Math.max(0,audio.duration-.25));if(wasPlaying)beginPlayback();switchingOutput=false};audio.addEventListener('loadedmetadata',restore,{once:true});audio.load()}
function showOutputState(){const connected=remoteConnected();output.classList.toggle('connected',connected);outputLabel.textContent=connected?'CONNECTED':'OUTPUT';output.title=connected?'Remote speaker connected':'Choose an AirPlay or Cast speaker';output.setAttribute('aria-label',output.title)}
function showModeState(state){currentMode=String(state.mode||'sequential');modeLocked=Boolean(state.scope);const active=currentMode==='shuffle';mode.textContent='PLAY MODE: '+currentMode.toUpperCase();shuffle.classList.toggle('active',active);shuffle.setAttribute('aria-pressed',String(active));shuffleLabel.textContent=active?'SHUFFLE ON':'SHUFFLE';shuffle.title=modeLocked?'Full-library playback always shuffles':active?'Turn shuffle off':'Turn shuffle on';shuffle.setAttribute('aria-label',shuffle.title)}
function selectOutput(){if(!currentTrack||!outputStream){showHint('Choose a song before selecting a speaker.');return}const position=audio.currentTime,wasPlaying=!audio.paused;replaceSource(outputStream,position,wasPlaying);try{if(typeof audio.webkitShowPlaybackTargetPicker==='function'){audio.webkitShowPlaybackTargetPicker();showHint('Choose an AirPlay speaker. Sonos appears here when it supports AirPlay.');return}if(audio.remote&&typeof audio.remote.prompt==='function'){audio.remote.prompt().then(showOutputState).catch(error=>{showOutputState();if(error&&error.name!=='NotAllowedError')showHint('No compatible Cast or remote speaker was found.')});showHint('Choose a Cast or remote playback device.');return}}catch(error){}showHint('This browser has no webpage speaker picker. Use its system AirPlay or Cast menu.')}
function publishMetadata(track,artwork=ART512,type='image/png'){if(!track||!('mediaSession' in navigator)||!window.MediaMetadata)return;const image={src:artwork};if(type)image.type=type;if(artwork.startsWith('data:'))image.sizes='any';const artist=text(track.artist)||'The Mars Hotel',album=text(track.album)||'MarsAmp',byline=album&&album!==artist?artist+' · '+album:artist;try{navigator.mediaSession.metadata=new MediaMetadata({title:text(track.title)||'Untitled',artist:byline,album:album,artwork:[image]})}catch(error){}}
function asDataURL(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result||''));reader.onerror=()=>reject(reader.error);reader.readAsDataURL(blob)})}
function refreshMetadata(){if(currentTrack)publishMetadata(currentTrack,mediaArtworkData||ART512,mediaArtworkType||'image/png')}
function scheduleMetadataRefresh(key){for(const delay of [150,750,1750])setTimeout(()=>{if(key===metadataKey)refreshMetadata()},delay)}
async function updateMetadata(track,force=false){if(!track)return;currentTrack=track;const key=String(track.id)+':'+String(track.artwork_url||track.artwork_version||'');const legacy=BASE+'/api/artwork/'+encodeURIComponent(track.id)+'?v='+encodeURIComponent(track.artwork_version||'1');const cover=new URL(track.artwork_url||legacy,location.origin).href;const changed=key!==metadataKey;if(!changed&&!force)return;if(changed){metadataKey=key;mediaArtworkData='';mediaArtworkType='';document.title=(text(track.title)||'Untitled')+' • MarsAmp';coverEl.src=cover}refreshMetadata();scheduleMetadataRefresh(key);if(!changed)return;
try{const response=await fetch(cover,{cache:'force-cache',credentials:'omit'});if(!response.ok)throw new Error('artwork unavailable');const blob=await response.blob();const data=await asDataURL(blob);if(key!==metadataKey||!data.startsWith('data:image/'))return;mediaArtworkData=data;mediaArtworkType=blob.type||'';publishMetadata(track,mediaArtworkData,mediaArtworkType)}catch(error){if(key===metadataKey)publishMetadata(track,ART512,'image/png')}}
function updatePosition(){elapsed.value=clock(audio.currentTime);durationEl.value=clock(audio.duration);seek.value=Number.isFinite(audio.duration)&&audio.duration>0?Math.round(audio.currentTime/audio.duration*1000):0;
if('mediaSession' in navigator&&Number.isFinite(audio.duration)&&audio.duration>0){try{navigator.mediaSession.setPositionState({duration:audio.duration,playbackRate:audio.playbackRate,position:Math.min(audio.currentTime,audio.duration)})}catch(error){}}}
function queueRow(track,index,current=false){const item=document.createElement('li');if(current)item.className='current';const number=document.createElement('div');number.className='number';number.textContent=current?'▶':String(index).padStart(2,'0');const copy=document.createElement('div');const strong=document.createElement('strong');strong.textContent=text(track.title)||'Untitled';const detail=document.createElement('span');detail.textContent=[text(track.artist),text(track.album)].filter(Boolean).join(' · ');copy.append(strong,detail);item.append(number,copy);return item}
async function loadQueue(){try{const response=await fetch(BASE+'/api/queue',{cache:'no-store'});if(response.status===401){sessionEnded();return}if(!response.ok)throw new Error('queue unavailable');const data=await response.json();queueEl.replaceChildren();queueSummary.textContent=(data.label||'QUEUE')+' · '+Number(data.total||0).toLocaleString()+' tracks';
if(data.dynamic){queueMeta.textContent='Full-library shuffle selects each next track directly from the live catalog.';if(data.current)queueEl.append(queueRow(data.current,0,true));const waiting=document.createElement('li');waiting.textContent='∞ Next selection is drawn when playback advances.';queueEl.append(waiting)}else{queueMeta.textContent=(data.mode||'sequential').toUpperCase()+' · upcoming playback order';if(data.current)queueEl.append(queueRow(data.current,0,true));(data.up_next||[]).forEach((track,index)=>queueEl.append(queueRow(track,index+1)))}
}catch(error){queueSummary.textContent='QUEUE · temporarily unavailable'}}
async function applyState(state){serverAction=state.action||'stop';localStream=state.stream||'';outputStream=state.output_stream||'';const track=state.track;if(track){titleEl.textContent=text(track.title)||'Untitled';artistEl.textContent=text(track.artist)||'Unknown artist';albumEl.textContent=text(track.album)||'The Mars Hotel collection';updateMetadata(track)}else if(serverAction==='stop'){titleEl.textContent='Playback stopped';artistEl.textContent='Choose a song in the BBS';albumEl.textContent='MarsAmp is ready'}showModeState(state);
if(state.serial!==serial){serial=state.serial;if(state.action==='play'&&state.stream){const source=remoteConnected()&&outputStream?outputStream:localStream;if(!state.resume||!audio.src||!sameSource(source))audio.src=source;await beginPlayback()}else if(state.action==='pause')audio.pause();else if(state.action==='stop'){audio.pause();audio.removeAttribute('src');audio.load()}await loadQueue()}bars(!audio.paused);showOutputState()}
async function navigate(path,label){showHint(label);try{const response=await fetch(BASE+path,{method:'POST',credentials:'same-origin'});if(response.status===401){sessionEnded();return null}if(!response.ok)throw new Error('request failed');const state=await response.json();await applyState(state);return state}catch(error){showHint('Unable to change songs. Check the connection.');return null}}
async function poll(){try{const response=await fetch(BASE+'/api/state',{cache:'no-store'});if(response.status===401){sessionEnded();return}await applyState(await response.json());setTimeout(poll,750)}catch(error){setTimeout(poll,2000)}}
enable.onclick=()=>serverAction==='pause'?navigate('/api/resume','Resuming playback…'):beginPlayback();document.getElementById('pause').onclick=()=>navigate('/api/pause','Pausing playback…');document.getElementById('stop').onclick=()=>navigate('/api/stop','Stopping playback…');
document.getElementById('previous').onclick=()=>navigate('/api/previous','Loading the previous song…');document.getElementById('next').onclick=()=>navigate('/api/advance','Loading the next song…');
shuffle.onclick=async()=>{if(modeLocked){showHint('Full-library playback always shuffles.');return}const next=currentMode==='shuffle'?'sequential':'shuffle';const state=await navigate('/api/mode/'+next,next==='shuffle'?'Turning shuffle on…':'Returning to sequential play…');if(state){showHint(next==='shuffle'?'Shuffle is on. The current cycle will not repeat songs.':'Sequential play is on.');await loadQueue()}};
output.onclick=selectOutput;
seek.addEventListener('input',()=>{if(Number.isFinite(audio.duration))audio.currentTime=audio.duration*Number(seek.value)/1000});
audio.addEventListener('timeupdate',updatePosition);audio.addEventListener('durationchange',updatePosition);audio.addEventListener('loadedmetadata',refreshMetadata);audio.addEventListener('canplay',refreshMetadata);audio.addEventListener('playing',refreshMetadata);audio.addEventListener('play',()=>{refreshMetadata();enable.textContent='▶ PLAYING';bars(true);setMediaState('playing')});audio.addEventListener('pause',()=>{enable.textContent='▶ PLAY';bars(false);setMediaState('paused')});
audio.addEventListener('ended',()=>navigate('/api/advance','Loading the next song…'));document.getElementById('queue-panel').addEventListener('toggle',event=>{if(event.target.open)loadQueue()});
audio.addEventListener('webkitplaybacktargetavailabilitychanged',event=>{output.disabled=event.availability==='not-available'&&!audio.remote;output.title=event.availability==='available'?'Choose an available AirPlay speaker':'No AirPlay speaker is currently visible';output.setAttribute('aria-label',output.title)});
audio.addEventListener('webkitcurrentplaybacktargetiswirelesschanged',()=>{showOutputState();if(!remoteConnected()&&!switchingOutput&&localStream)replaceSource(localStream,audio.currentTime,!audio.paused)});
if(audio.remote&&typeof audio.remote.watchAvailability==='function'){audio.remote.onconnecting=showOutputState;audio.remote.onconnect=showOutputState;audio.remote.ondisconnect=()=>{showOutputState();if(!switchingOutput&&localStream)replaceSource(localStream,audio.currentTime,!audio.paused)};audio.remote.watchAvailability(available=>{output.disabled=!available;output.title=available?'Choose an available remote speaker':'No compatible remote speaker is currently visible';output.setAttribute('aria-label',output.title)}).catch(()=>{output.disabled=false})}
if('mediaSession' in navigator){const action=(name,handler)=>{try{navigator.mediaSession.setActionHandler(name,handler)}catch(error){}};action('play',()=>serverAction==='pause'?navigate('/api/resume','Resuming playback…'):beginPlayback());action('pause',()=>navigate('/api/pause','Pausing playback…'));action('stop',()=>navigate('/api/stop','Stopping playback…'));action('previoustrack',()=>navigate('/api/previous','Loading the previous song…'));action('nexttrack',()=>navigate('/api/advance','Loading the next song…'));action('seekbackward',details=>{audio.currentTime=Math.max(0,audio.currentTime-(details.seekOffset||10))});action('seekforward',details=>{audio.currentTime=Math.min(audio.duration||audio.currentTime,audio.currentTime+(details.seekOffset||10))});action('seekto',details=>{if(details.seekTime!=null)audio.currentTime=details.seekTime})}
poll();
</script></body></html>"""
    return page.replace("__BASE__", json.dumps(base_path)).encode("utf-8")


class MusicHTTP(BaseHTTPRequestHandler):
    server_version = "MarsAmp/0.1"
    music: Music

    def log_message(self, format_string: str, *args: object) -> None:
        # Do not log pairing tickets or browser cookies through request paths.
        return

    def _headers(self, status: int, content_type: str, length: int = 0,
                 extra: dict[str, str] | None = None,
                 cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; media-src 'self'; "
                         "img-src 'self' data:; manifest-src 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _path(self) -> str | None:
        parsed = urlparse(self.path)
        base = self.music.base_path
        if base and not (parsed.path == base or parsed.path.startswith(base + "/")):
            return None
        return parsed.path[len(base):] or "/"

    def _cookie(self) -> str:
        raw = self.headers.get("Cookie", "")
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(raw)
        except http.cookies.CookieError:
            return ""
        morsel = cookie.get("marsamp_session")
        return morsel.value if morsel is not None else ""

    def _identity(self) -> dict[str, object]:
        return self.music.browser_identity(self._cookie())

    def _json(self, obj: dict[str, object], status: int = 200) -> None:
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self._headers(status, "application/json", len(payload))
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        path = self._path()
        if path is None:
            self._headers(404, "text/plain; charset=utf-8")
            return
        if path == "/artwork.svg":
            payload = _artwork_svg()
            self._headers(200, "image/svg+xml", len(payload))
            self.wfile.write(payload)
            return
        artwork = re.fullmatch(r"/artwork-(192|512)\.png", path)
        if artwork:
            payload = _artwork_png(int(artwork.group(1)))
            self._headers(200, "image/png", len(payload))
            self.wfile.write(payload)
            return
        signed_artwork = re.fullmatch(
            r"/artwork/([1-9][0-9]{0,9})/([0-9a-f]{16})/"
            r"([0-9]{9,12})/([0-9a-f]{64})", path)
        if signed_artwork:
            track_id = integer(int(signed_artwork.group(1)), "track id")
            if not self.music.validate_artwork_capability(
                    track_id, signed_artwork.group(2), int(signed_artwork.group(3)),
                    signed_artwork.group(4)):
                self._headers(403, "text/plain; charset=utf-8")
                return
            self._track_artwork(
                track_id, head=False, cache_control="private, max-age=3600, immutable")
            return
        signed_stream = re.fullmatch(
            r"/stream/([1-9][0-9]{0,9})/([0-9a-f]{16})/"
            r"([0-9]{9,12})/([0-9a-f]{64})", path)
        if signed_stream:
            track_id = integer(int(signed_stream.group(1)), "track id")
            if not self.music.validate_stream_capability(
                    track_id, signed_stream.group(2), int(signed_stream.group(3)),
                    signed_stream.group(4)):
                self._headers(403, "text/plain; charset=utf-8")
                return
            self._stream(track_id, head=False,
                         cache_control="private, max-age=900")
            return
        if path == "/manifest.webmanifest":
            payload = _player_manifest(self.music.base_path)
            self._headers(200, "application/manifest+json", len(payload))
            self.wfile.write(payload)
            return
        if path.startswith("/pair/"):
            ticket = unquote(path.removeprefix("/pair/"))
            try:
                browser = self.music.redeem_pair(ticket)
            except RPCError:
                payload = b"Invalid or expired MarsAmp pairing ticket."
                self._headers(403, "text/plain; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            cookie_path = self.music.base_path or "/"
            self._headers(303, "text/plain; charset=utf-8", extra={
                "Location": cookie_path + "/",
                "Set-Cookie": ("marsamp_session=" + browser + "; Path=" + cookie_path +
                               "; Max-Age=" + str(BROWSER_SECONDS) +
                               "; Secure; HttpOnly; SameSite=Strict"),
            })
            return
        try:
            ident = self._identity()
            if path == "/" or path == "":
                payload = _player_html(self.music.base_path)
                self._headers(200, "text/html; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            if path == "/api/state":
                self._json(self.music.state(int(ident["account_id"])))
                return
            if path == "/api/queue":
                self._json(self.music.queue_view(int(ident["account_id"])))
                return
            artwork = re.fullmatch(r"/api/artwork/([1-9][0-9]{0,9})", path)
            if artwork:
                self._track_artwork(integer(int(artwork.group(1)), "track id"), head=False)
                return
            match = re.fullmatch(r"/audio/([1-9][0-9]{0,9})", path)
            if match:
                self._stream(integer(int(match.group(1)), "track id"), head=False)
                return
        except RPCError as exc:
            self._json({"error": str(exc)}, 401)
            return
        self._headers(404, "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        path = self._path()
        signed_artwork = re.fullmatch(
            r"/artwork/([1-9][0-9]{0,9})/([0-9a-f]{16})/"
            r"([0-9]{9,12})/([0-9a-f]{64})", path or "")
        if signed_artwork:
            track_id = integer(int(signed_artwork.group(1)), "track id")
            if not self.music.validate_artwork_capability(
                    track_id, signed_artwork.group(2), int(signed_artwork.group(3)),
                    signed_artwork.group(4)):
                self._headers(403, "text/plain; charset=utf-8")
                return
            self._track_artwork(
                track_id, head=True, cache_control="private, max-age=3600, immutable")
            return
        signed_stream = re.fullmatch(
            r"/stream/([1-9][0-9]{0,9})/([0-9a-f]{16})/"
            r"([0-9]{9,12})/([0-9a-f]{64})", path or "")
        if signed_stream:
            track_id = integer(int(signed_stream.group(1)), "track id")
            if not self.music.validate_stream_capability(
                    track_id, signed_stream.group(2), int(signed_stream.group(3)),
                    signed_stream.group(4)):
                self._headers(403, "text/plain; charset=utf-8")
                return
            self._stream(track_id, head=True,
                         cache_control="private, max-age=900")
            return
        try:
            self._identity()
            artwork = re.fullmatch(r"/api/artwork/([1-9][0-9]{0,9})", path or "")
            if artwork:
                self._track_artwork(integer(int(artwork.group(1)), "track id"), head=True)
                return
            match = re.fullmatch(r"/audio/([1-9][0-9]{0,9})", path or "")
            if match:
                self._stream(integer(int(match.group(1)), "track id"), head=True)
                return
        except RPCError:
            self._headers(401, "text/plain; charset=utf-8")
            return
        self._headers(404, "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        path = self._path()
        operations = {
            "/api/advance": "advance", "/api/previous": "previous",
            "/api/pause": "pause", "/api/resume": "resume", "/api/stop": "stop",
            "/api/mode/shuffle": "mode_shuffle",
            "/api/mode/sequential": "mode_sequential",
        }
        if path not in operations:
            self._headers(404, "text/plain; charset=utf-8")
            return
        if self.headers.get("Origin") != self.music.public_origin:
            self._headers(403, "text/plain; charset=utf-8")
            return
        try:
            ident = self._identity()
            account_id = int(ident["account_id"])
            operation = operations[path]
            if operation == "advance":
                result = self.music.advance(account_id)
            elif operation == "previous":
                result = self.music.previous(account_id)
            elif operation.startswith("mode_"):
                result = self.music.set_mode(account_id, operation.removeprefix("mode_"))
            else:
                result = self.music.command(account_id, operation)
            self._json(result)
        except RPCError as exc:
            self._json({"error": str(exc)}, 401)

    def _track_artwork(self, track_id: int, head: bool,
                       cache_control: str = "no-store") -> None:
        mime_type, payload, _source = self.music.artwork(track_id)
        self._headers(200, mime_type, len(payload), cache_control=cache_control)
        if not head:
            self.wfile.write(payload)

    def _stream(self, track_id: int, head: bool,
                cache_control: str = "no-store") -> None:
        row, filename = self.music._track_file(track_id)
        total = int(row["size"])
        start, end, status = 0, total - 1, 200
        range_value = self.headers.get("Range", "")
        if range_value:
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_value)
            if not match or (not match.group(1) and not match.group(2)):
                self._headers(416, "text/plain", extra={"Content-Range": f"bytes */{total}"})
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else total - 1
            else:
                length = int(match.group(2))
                start = max(0, total - length)
            if start >= total or end < start:
                self._headers(416, "text/plain", extra={"Content-Range": f"bytes */{total}"})
                return
            end = min(end, total - 1)
            status = 206
        length = end - start + 1
        extra = {"Accept-Ranges": "bytes"}
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{total}"
        self._headers(status, str(row["mime_type"]), length, extra,
                      cache_control=cache_control)
        if head:
            return
        with open(filename, "rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(128 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def parse_source(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("source must be NAME=PATH")
    name, path = value.split("=", 1)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or not os.path.isabs(path):
        raise argparse.ArgumentTypeError("source must use a safe name and absolute path")
    return name, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--source", action="append", type=parse_source, default=[])
    parser.add_argument("--extra-root")
    parser.add_argument("--playlists-file",
                        default="/opt/mars-v2/assets/music-playlists.json")
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--http-host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--scan-on-start", action="store_true")
    args = parser.parse_args()
    parsed = urlparse(args.public_url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise SystemExit("public URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("non-loopback MarsAmp public URL must use HTTPS")
    sources = dict(args.source)
    if not sources:
        raise SystemExit("at least one music source is required")
    music = Music(args.db, args.session_socket, sources, args.public_url, args.extra_root,
                  args.playlists_file)
    MusicHTTP.music = music
    if args.scan_on_start:
        music.start_scan()
    threading.Thread(target=serve, args=(args.socket, music.handle),
                     name="music-rpc", daemon=True).start()
    server = ThreadingHTTPServer((args.http_host, args.http_port), MusicHTTP)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
