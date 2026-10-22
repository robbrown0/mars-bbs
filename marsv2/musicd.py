"""Authenticated MarsAmp catalog, playback control, and browser audio sink.

The SSH terminal remains the control surface.  Browsers receive only opaque
track identifiers and byte streams after redeeming a short-lived pairing
ticket created by an authenticated BBS session.  Source paths and NAS
credentials never cross either public interface.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .db import connect
from .identity import validate
from .rpc import RPCError, request, serve
from .validate import integer, text

PLAY_CAPABILITY = "music.play"
MANAGE_CAPABILITY = "music.manage"
PAIR_SECONDS = 90
BROWSER_SECONDS = 8 * 60 * 60
MAX_RESULTS = 50
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

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS extra_sources (
  name TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
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


class Music:
    def __init__(self, db_path: str, session_socket: str,
                 sources: dict[str, str], public_url: str,
                 extra_root: str | None = None):
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
        self.lock = threading.RLock()
        self.pair_tickets: dict[str, dict[str, object]] = {}
        self.browser_sessions: dict[str, dict[str, object]] = {}
        self.playback: dict[int, dict[str, object]] = {}
        self.scan_running = False
        self.last_scan = 0
        self.last_scan_result: dict[str, object] = {"tracks": 0, "errors": []}

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
                self.last_scan_result = {"tracks": found_total, "errors": errors}
            return {"started": True, "tracks": found_total, "errors": errors}
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
        clauses = ["available=1"]
        parameters: list[object] = []
        if query:
            pattern = "%" + _escape_like(query) + "%"
            clauses.append("(title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' "
                           "OR album LIKE ? ESCAPE '\\')")
            parameters.extend((pattern, pattern, pattern))
        where = " WHERE " + " AND ".join(clauses)
        with self.lock:
            total = int(self.db.execute(
                "SELECT count(*) FROM tracks" + where, parameters).fetchone()[0])
            rows = self.db.execute(
                "SELECT id,title,artist,album,source,extension,size FROM tracks" + where +
                " ORDER BY artist COLLATE NOCASE,album COLLATE NOCASE,title COLLATE NOCASE "
                "LIMIT ? OFFSET ?", parameters + [limit, offset]).fetchall()
        return {"tracks": [dict(row) for row in rows], "total": total,
                "offset": offset, "next_offset": offset + len(rows)}

    def _public_track(self, track_id: int) -> dict[str, object]:
        with self.lock:
            row = self.db.execute(
                "SELECT id,title,artist,album,source,extension,size,mime_type "
                "FROM tracks WHERE id=? AND available=1", (track_id,)).fetchone()
        if row is None:
            raise RPCError("track unavailable")
        return dict(row)

    def _track_file(self, track_id: int) -> tuple[sqlite3.Row, str]:
        with self.lock:
            row = self.db.execute("SELECT * FROM tracks WHERE id=? AND available=1",
                                  (track_id,)).fetchone()
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
            browser = secrets.token_urlsafe(48)
            browser_hash = hashlib.sha256(browser.encode("ascii")).hexdigest()
            ticket = dict(ticket)
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
                state["stream"] = f"{self.base_path}/audio/{track_id}"
            except RPCError:
                state.update({"action": "stop", "track_id": 0})
        return state

    def command(self, account_id: int, action: str, track_id: int = 0) -> dict[str, object]:
        if action not in ("play", "pause", "stop"):
            raise RPCError("invalid playback command")
        if action == "play":
            self._public_track(track_id)
        with self.lock:
            previous = self.playback.get(account_id, {"serial": 0, "track_id": 0})
            selected = track_id if action == "play" else int(previous.get("track_id", 0))
            if action == "stop":
                selected = 0
            self.playback[account_id] = {
                "serial": int(previous.get("serial", 0)) + 1,
                "action": action, "track_id": selected,
            }
        return self.state(account_id)

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
            health.update({"sources": list(self._all_sources()), "last_scan": self.last_scan,
                           "last_scan_result": self.last_scan_result})
            return health
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
        if op == "search":
            query = text(data.get("query", ""), "search", 80).strip()
            offset = integer(data.get("offset", 0), "offset", 0, 10_000_000)
            limit = integer(data.get("limit", 20), "limit", 1, MAX_RESULTS)
            return self._search(query, offset, limit)
        if op == "pair":
            if data:
                raise RPCError("invalid pairing request")
            return self._new_pair(ident, str(req["token"]))
        if op == "status":
            return self.state(int(ident["account_id"]))
        if op in ("play", "pause", "stop"):
            track_id = 0
            if op == "play":
                track_id = integer(data.get("id"), "track id")
            elif data:
                raise RPCError("invalid playback request")
            account_id = int(ident["account_id"])
            result = self.command(account_id, op, track_id)
            if op == "play" and not self.browser_active(account_id):
                result["pair_url"] = self._new_pair(
                    ident, str(req["token"]))["url"]
            return result
        if op == "rescan":
            self._require(ident, MANAGE_CAPABILITY)
            if data:
                raise RPCError("invalid scan request")
            return self.start_scan()
        raise RPCError("unknown operation")


def _player_html(base_path: str) -> bytes:
    base = json.dumps(base_path)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MarsAmp Audio Sink</title>
<style>
:root{{--green:#36d07c;--blue:#2d9cff;--bg:#111326;--panel:#171a30}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--green);
font:16px ui-monospace,SFMono-Regular,Consolas,monospace;display:grid;min-height:100vh;
place-items:center}} main{{width:min(94vw,620px);border:3px double var(--blue);padding:16px;
background:var(--panel);box-shadow:0 0 32px #000}} h1{{color:var(--blue);font-size:1.3rem}}
#track{{min-height:3em}} button{{background:#202743;color:var(--green);border:1px solid var(--green);
font:inherit;padding:.65em 1em;margin:.2em}} button:active{{background:var(--green);color:#111326}}
.meter{{height:50px;display:flex;gap:4px;align-items:end;margin:14px 0}}
.meter i{{display:block;width:7%;background:var(--green);height:20%}}
small{{display:block;color:#a6afc8;margin-top:12px;line-height:1.4}}
</style></head><body><main><h1>MARSAMP // AUDIO SINK</h1>
<div id="track">Paired with The Mars Hotel. Tap Enable Audio once.</div>
<div class="meter" id="meter"></div><audio id="audio" preload="metadata"></audio>
<div><button id="enable">Play Selected Song</button><button id="pause">Pause</button>
<button id="stop">Stop</button></div>
<small>Keep this page open while controlling MarsAmp through SSH. No NAS path or BBS
credential is stored in this page.</small></main><script>
const BASE={base}; const audio=document.getElementById('audio'); let enabled=false, serial=-1;
const meter=document.getElementById('meter'); for(let i=0;i<12;i++)meter.appendChild(document.createElement('i'));
function bars(on){{[...meter.children].forEach((b,i)=>b.style.height=on?(20+((Date.now()/160+i*29)%75))+'%':'8%')}}
document.getElementById('enable').onclick=()=>{{enabled=true;if(audio.src)audio.play();}};
document.getElementById('pause').onclick=()=>audio.pause();
document.getElementById('stop').onclick=()=>{{audio.pause();audio.currentTime=0}};
async function poll(){{try{{let r=await fetch(BASE+'/api/state',{{cache:'no-store'}});
if(r.status===401){{document.getElementById('track').textContent='BBS session ended. Pair again.';return}}
let s=await r.json(); if(s.track)document.getElementById('track').textContent=
(s.track.artist?s.track.artist+' - ':'')+s.track.title;
if(s.serial!==serial){{serial=s.serial;if(s.action==='play'&&s.stream){{
audio.src=s.stream;if(enabled)audio.play();else audio.play().then(()=>enabled=true).catch(()=>{{}});}}else if(s.action==='pause')audio.pause();
else if(s.action==='stop'){{audio.pause();audio.removeAttribute('src');audio.load();}}}}
bars(!audio.paused);setTimeout(poll,750)}}catch(e){{setTimeout(poll,2000)}}}} poll();
</script></body></html>"""
    return page.encode("utf-8")


class MusicHTTP(BaseHTTPRequestHandler):
    server_version = "MarsAmp/0.1"
    music: Music

    def log_message(self, format_string: str, *args: object) -> None:
        # Do not log pairing tickets or browser cookies through request paths.
        return

    def _headers(self, status: int, content_type: str, length: int = 0,
                 extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; media-src 'self'")
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
        try:
            self._identity()
            match = re.fullmatch(r"/audio/([1-9][0-9]{0,9})", path or "")
            if match:
                self._stream(integer(int(match.group(1)), "track id"), head=True)
                return
        except RPCError:
            self._headers(401, "text/plain; charset=utf-8")
            return
        self._headers(404, "text/plain; charset=utf-8")

    def _stream(self, track_id: int, head: bool) -> None:
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
        self._headers(status, str(row["mime_type"]), length, extra)
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
    music = Music(args.db, args.session_socket, sources, args.public_url, args.extra_root)
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
