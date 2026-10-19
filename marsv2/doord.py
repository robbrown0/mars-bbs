"""Authenticated PTY authority for native and DOS Galactic Arcade doors."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import re
import select
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

from .db import connect
from .door_client import MAX_FRAME, MAX_HANDSHAKE, recv_frame, send_frame
from .identity import validate
from .rpc import RPCError, peer_credentials, serve


DOORS = {
    "zork": {"name": "Zork I", "description": "The Great Underground Empire"},
    "lord": {"name": "Legend of the Red Dragon",
             "description": "Authentic LORD 4.02 shareware door"},
    "tradewars": {"name": "TradeWars 2002",
                  "description": "Authentic TradeWars 2002 3.09 door"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS launch_log (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL,
  userid TEXT NOT NULL,
  game TEXT NOT NULL,
  node INTEGER NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS door_launch_created ON launch_log(created_at,id);
"""
USERID = re.compile(r"^[a-z][a-z0-9_-]{0,11}$")
ANSI_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
MAX_SESSION_SECONDS = 6 * 60 * 60
SETUP_TIMEOUT_SECONDS = 180
MAX_LORD_NODES = 16


def _exact(sock: socket.socket, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        part = sock.recv(length - len(value))
        if not part:
            raise EOFError("client disconnected")
        value.extend(part)
    return bytes(value)


def _atomic(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="ascii", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_lord_registration(path: str) -> tuple[str, str, str, str, str]:
    """Read a private five-number LORD credential without normalizing it."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeError("LORD registration must be a private regular file")
    if info.st_uid not in (0, os.geteuid()):
        raise RuntimeError("LORD registration has an unexpected owner")
    with open(path, "r", encoding="ascii") as stream:
        raw = stream.read(256)
        if stream.read(1):
            raise RuntimeError("LORD registration file is too large")
    fields = raw.split()
    if len(fields) != 5 or any(
            not re.fullmatch(r"[0-9]{1,5}", field) or int(field) > 65535
            for field in fields):
        raise RuntimeError("LORD registration must contain five decimal numbers")
    return tuple(fields)  # type: ignore[return-value]


def _cp437_graphics_to_utf8(value: bytes) -> bytes:
    """Translate DOS high-bit glyphs while preserving ASCII/ANSI bytes."""
    result = bytearray()
    for byte in value:
        if byte < 0x80:
            result.append(byte)
        else:
            result.extend(bytes((byte,)).decode("cp437").encode("utf-8"))
    return bytes(result)


def _send_terminal_output(conn: socket.socket, game: str, output: bytes) -> None:
    """Send bounded frames; DOS doors require CP437 terminal translation."""
    if game not in ("lord", "tradewars"):
        send_frame(conn, b"O", output)
        return
    # A CP437 byte expands to at most three UTF-8 bytes. Splitting the source
    # first keeps every protocol frame bounded without bisecting a code point.
    source_limit = MAX_FRAME // 3
    for offset in range(0, len(output), source_limit):
        send_frame(conn, b"O", _cp437_graphics_to_utf8(
            output[offset:offset + source_limit]))


def _pty_dimensions(game: str, columns: int, rows: int) -> tuple[int, int]:
    """Return the virtual screen expected by each game runtime."""
    if game in ("lord", "tradewars"):
        return 80, 25
    return columns, rows


def _door_sys(userid: str, display_name: str, node: int) -> str:
    # LORD and TradeWars consume the conventional 52-line DOOR.SYS format.
    # No BBS password or session capability is ever placed in a drop file.
    name = "".join(ch for ch in display_name if 32 <= ord(ch) < 127)[:25] or userid
    now = time.localtime()
    date = time.strftime("%m-%d-%y", now)
    clock = time.strftime("%H:%M", now)
    lines = [
        "COM1:", "38400", "8", str(node), "38400", "Y", "N", "Y", "Y",
        name, "Mars Hotel", "000-000-0000", "000-000-0000", "NOT-EXPORTED",
        "30", "1", date, "21600", "360", "GR", "25", "N", "", "", date,
        str(node), "Z", "0", "0", "0", "0", date,
        f"C:\\NODEINFO\\{node}", f"C:\\NODEINFO\\{node}",
        "Mars Sysop", userid, "none", "Y", "N", "Y", "7", "0", date,
        clock, clock, "32768", "0", "0", "0", "", "0", "0",
    ]
    if len(lines) != 52:
        raise RuntimeError("internal DOOR.SYS layout error")
    return "\r\n".join(lines) + "\r\n"


class DoorAuthority:
    def __init__(self, root: str, template_root: str, session_socket: str,
                 enabled: set[str], lord_registration_file: str | None = None,
                 lord_nodes: int = 4):
        self.root = Path(root)
        self.template_root = Path(template_root)
        self.session_socket = session_socket
        self.enabled = enabled
        if not 1 <= lord_nodes <= MAX_LORD_NODES:
            raise ValueError(f"LORD node count must be 1-{MAX_LORD_NODES}")
        self.lord_nodes = lord_nodes
        unknown = enabled.difference(DOORS)
        if unknown:
            raise ValueError("unknown enabled door(s): " + ", ".join(sorted(unknown)))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = str(self.root / "doors.db")
        self.db = connect(self.db_path)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.nodes: dict[str, set[int]] = {game: set() for game in DOORS}
        self.node_locks: dict[tuple[str, int], int] = {}
        self._seed("lord")
        self._seed("tradewars")
        (self.root / "zork").mkdir(mode=0o700, exist_ok=True)
        self._initialize_legacy_worlds()
        if lord_registration_file:
            self._register_lord(lord_registration_file)

    def _seed(self, game: str) -> None:
        source = self.template_root / game
        target = self.root / game
        if target.exists():
            return
        temporary = self.root / (game + ".new")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(source, temporary)
        for directory, _, files in os.walk(temporary):
            os.chmod(directory, 0o700)
            for filename in files:
                os.chmod(os.path.join(directory, filename), 0o600)
        os.replace(temporary, target)

    def _log(self, account_id: int, userid: str, game: str, node: int,
             action: str, detail: str) -> None:
        # Each play session runs in its own thread.  A fresh SQLite connection
        # avoids sharing connection state across threads while WAL serializes
        # the small append-only audit records safely.
        database = connect(self.db_path)
        try:
            with database:
                database.execute(
                    "INSERT INTO launch_log(account_id,userid,game,node,action,detail,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (account_id, userid, game, node, action, detail[:240], int(time.time())))
        finally:
            database.close()

    def _ready(self, game: str) -> tuple[bool, str]:
        if game == "zork":
            story = self.template_root / "zork" / "zork1-r119-s880429.z3"
            return story.is_file(), ""
        if game == "lord":
            executable = self.root / "lord/home/.dosemu/drive_c/doors/lord/LORD.EXE"
            marker = self.root / "lord/.initialized"
            registered = (self.root / "lord/.registered").is_file()
            limitation = (
                f"Registered historical edition; up to {self.lord_nodes} concurrent nodes."
                if registered else
                "Authentic unregistered shareware: progression stops at level 6; "
                "registered-only multi-node controls remain disabled.")
            return executable.is_file() and marker.is_file(), limitation
        executable = self.root / "tradewars/home/.dosemu/drive_c/doors/tw/TW2002.EXE"
        marker = self.root / "tradewars/.initialized"
        limitation = ("Authentic unregistered 3.09: one node; alternate ships, citadels, "
                      "and photon missiles require a registration code.")
        return executable.is_file() and marker.is_file(), limitation

    def rpc(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = req["op"]
        if op == "health":
            self.db.execute("SELECT 1").fetchone()
            return {"status": "ok"}
        ident = validate(self.session_socket, req.get("token"), peer[1])
        data = req["data"]
        if not isinstance(data, dict) or data:
            raise RPCError("unknown request field")
        if op == "catalog":
            result = []
            for game, metadata in DOORS.items():
                if game not in self.enabled:
                    continue
                ready, limitation = self._ready(game)
                result.append({"id": game, **metadata, "ready": ready,
                               "limitation": limitation})
            return {"games": result, "account_id": int(ident["account_id"])}
        raise RPCError("unknown operation")

    def _allocate(self, game: str) -> int:
        if game == "lord" and (self.root / "lord/.registered").is_file():
            maximum = self.lord_nodes
        elif game in ("lord", "tradewars"):
            maximum = 1
        else:
            maximum = 16
        with self.lock:
            for node in range(1, maximum + 1):
                if node in self.nodes[game]:
                    continue
                lock_path = self.root / game / f".mars-node-{node}.lock"
                descriptor = os.open(lock_path,
                                     os.O_RDWR | os.O_CREAT | os.O_CLOEXEC |
                                     getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    os.close(descriptor)
                    if exc.errno in (errno.EACCES, errno.EAGAIN):
                        continue
                    raise
                self.nodes[game].add(node)
                self.node_locks[(game, node)] = descriptor
                return node
        raise RPCError("all licensed game nodes are currently in use")

    def _release(self, game: str, node: int) -> None:
        with self.lock:
            self.nodes[game].discard(node)
            descriptor = self.node_locks.pop((game, node), None)
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _drop_file(self, game: str, ident: dict[str, object], node: int) -> None:
        home = self.root / game / "home"
        drive = home / ".dosemu/drive_c"
        node_dir = drive / "nodeinfo" / str(node)
        node_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic(node_dir / "DOOR.SYS", _door_sys(
            str(ident["userid"]), str(ident["display_name"]), node))
        if game == "lord":
            config = (
                f"**Mars Hotel node {node}\r\n"
                "BBSNAME The Mars Hotel\r\n"
                "BBSTYPE DOORSYS\r\n"
                "COMPORT 1\r\n"
                "FOSSIL\r\n"
                "LOCKBAUD 38400\r\n"
                "STATUS ON\r\n"
                "STATFORE 3\r\n"
                "STATBACK 7\r\n"
                f"NODE{node}\r\n"
                "NODIRECT\r\n"
                f"BBSDROP C:\\NODEINFO\\{node}\r\n"
                ";UNUSED\r\n;UNUSED\r\n;UNUSED\r\n"
            )
            _atomic(drive / "doors/lord" / f"NODE{node}.DAT", config)

    def _command(self, game: str, ident: dict[str, object], node: int,
                 temporary: Path) -> tuple[list[str], Path, list[str], list[str], dict[str, str]]:
        common_read = ["/usr", "/lib", "/lib64", "/etc", "/dev", "/proc",
                       "/sys", "/opt/mars-v2", "/opt/mars-doors"]
        sandbox = ["/usr/bin/python3", "-m", "marsv2.door_sandbox"]
        env = {"TERM": "ansi", "TMPDIR": str(temporary),
               "PYTHONPATH": "/opt/mars-v2"}
        if game == "zork":
            saves = self.root / "zork" / str(int(ident["account_id"]))
            saves.mkdir(mode=0o700, exist_ok=True)
            story = self.template_root / "zork/zork1-r119-s880429.z3"
            command = ["/usr/local/bin/dfrotz", "-p", "-R", str(saves), str(story)]
            return sandbox, saves, common_read, [str(saves), str(temporary)], env | {
                "HOME": str(saves)} | {"MARS_COMMAND": json.dumps(command)}

        home = self.root / game / "home"
        dosemu = ["/usr/bin/dosemu", "-t"]
        if game == "lord":
            dosemu += ["-I", "serial { com 1 virtual }", "-K", ":C:\\doors\\lord",
                       "-E", f"marslord.bat {node}"]
        else:
            userid = str(ident["userid"])
            if not USERID.fullmatch(userid):
                raise RPCError("userid is not compatible with TradeWars")
            dosemu += ["-K", ":C:\\doors\\tw", "-E", f"marstw.bat {userid}"]
        env.update({"HOME": str(home), "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8", "XDG_RUNTIME_DIR": str(temporary)})
        env.update({"COLUMNS": "80", "LINES": "25"})
        return sandbox, home, common_read, [str(home), str(temporary)], env | {
            "MARS_COMMAND": json.dumps(dosemu)}

    def _setup_command(self, game: str, executable: str,
                       temporary: Path) -> tuple[list[str], Path, dict[str, str]]:
        home = self.root / game / "home"
        directory = "lord" if game == "lord" else "tw"
        command = ["/usr/bin/dosemu", "-t", "-K", f":C:\\doors\\{directory}",
                   "-E", executable]
        reads = ["/usr", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys",
                 "/opt/mars-v2", "/opt/mars-doors"]
        prefix = ["/usr/bin/python3", "-m", "marsv2.door_sandbox"]
        environment = {"HOME": str(home), "TERM": "ansi", "LANG": "C.UTF-8",
                       "LC_ALL": "C.UTF-8", "TMPDIR": str(temporary),
                       "XDG_RUNTIME_DIR": str(temporary),
                       "PYTHONPATH": "/opt/mars-v2"}
        return self._wrapper(prefix, reads, [str(home), str(temporary)], command), home, environment

    def _run_setup(self, game: str, executable: str,
                   interactions: list[tuple[bytes, bytes]], initial: bytes = b"") -> bytes:
        temporary = Path("/tmp") / ("mars-setup-" + game + "-" + os.urandom(6).hex())
        temporary.mkdir(parents=True, mode=0o700)
        master = slave = -1
        process: subprocess.Popen[bytes] | None = None
        transcript = bytearray()
        search_window = bytearray()
        try:
            command, cwd, environment = self._setup_command(game, executable, temporary)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 25, 80, 0, 0))
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                       cwd=str(cwd), env=environment, close_fds=True,
                                       start_new_session=True)
            os.close(slave)
            slave = -1
            os.set_blocking(master, False)
            if initial:
                time.sleep(1.0)
                os.write(master, initial)
            pending = list(interactions)
            deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
            while process.poll() is None and time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.25)
                if not readable:
                    continue
                try:
                    chunk = os.read(master, MAX_FRAME)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                transcript.extend(chunk)
                search_window.extend(chunk)
                if len(transcript) > 256 * 1024:
                    del transcript[:-128 * 1024]
                if len(search_window) > 32 * 1024:
                    del search_window[:-16 * 1024]
                # Configuration programs paint individual words in different
                # ANSI colours.  Match their visible text, not raw CSI bytes.
                lowered = ANSI_CSI.sub(b"", bytes(search_window)).lower()
                for index, (pattern, response) in enumerate(pending):
                    if pattern.lower() in lowered:
                        pending.pop(index)
                        os.write(master, response)
                        search_window.clear()
                        break
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
                raise RuntimeError(game + " initialization timed out")
            return_code = process.wait()
            if return_code:
                # DOSEMU2 reports a non-zero status on this Unraid kernel when
                # its *own* optional Landlock teardown is unavailable, even
                # after the DOS program saved successfully.  The caller must
                # validate the authoritative output files before committing a
                # marker; an exit status alone is not the transaction gate.
                transcript.extend(f"\n[DOSEMU EXIT {return_code}]\n".encode("ascii"))
            return bytes(transcript)
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if slave >= 0:
                os.close(slave)
            if master >= 0:
                os.close(master)
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _case_file(directory: Path, filename: str) -> Path | None:
        wanted = filename.casefold()
        for item in directory.iterdir():
            if item.name.casefold() == wanted and item.is_file():
                return item
        return None

    def _initialize_legacy_worlds(self) -> None:
        """Create fresh shareware worlds without user input on first boot."""
        lord_dir = self.root / "lord/home/.dosemu/drive_c/doors/lord"
        lord_marker = self.root / "lord/.initialized"
        if not lord_marker.exists():
            transcript = self._run_setup(
                "lord", "LORDCFG.EXE",
                [(b"NEW LORD.DAT", b"a"), (b"LORD CONFIGURE", b"q")])
            lord_data = self._case_file(lord_dir, "lord.dat")
            if lord_data is None or lord_data.stat().st_size < 100:
                raise RuntimeError("LORD configuration did not create LORD.DAT")
            _atomic(lord_marker, "LORD 4.02a initialized\n")
            setup_log = self.root / "lord/setup.log"
            setup_log.write_bytes(transcript[-65536:])
            os.chmod(setup_log, 0o600)

        tw_dir = self.root / "tradewars/home/.dosemu/drive_c/doors/tw"
        tw_marker = self.root / "tradewars/.initialized"
        if not tw_marker.exists():
            transcript = self._run_setup(
                "tradewars", "BIGBANG.EXE",
                [(b"Have you REGISTERED TW2002?", b"n"),
                 (b"BigBang Setup Editor", b"z\r")])
            sector_data = self._case_file(tw_dir, "twsect.dat")
            if sector_data is None or sector_data.stat().st_size < 1000:
                raise RuntimeError("TradeWars BigBang did not create a universe")
            _atomic(tw_marker, "TradeWars 2002 3.09 universe initialized\n")
            setup_log = self.root / "tradewars/setup.log"
            setup_log.write_bytes(transcript[-65536:])
            os.chmod(setup_log, 0o600)

    def _register_lord(self, secret_path: str) -> None:
        """Apply a registration once; never persist or log the credential."""
        marker = self.root / "lord/.registered"
        if marker.is_file():
            return
        fields = _read_lord_registration(secret_path)
        interactions: list[tuple[bytes, bytes]] = [
            (b"LORD CONFIGURE", b"e"),
            (b"First Number", fields[0].encode("ascii") + b"\r"),
            (b"Second Number", fields[1].encode("ascii") + b"\r"),
            (b"Third Number", fields[2].encode("ascii") + b"\r"),
            (b"Fourth Number", fields[3].encode("ascii") + b"\r"),
            (b"Fifth Number", fields[4].encode("ascii") + b"\r"),
            (b"THE GAME IS NOW REGISTERED", b" "),
            (b"faulty", b" "),
            (b"LORD CONFIGURE", b"q"),
        ]
        transcript = self._run_setup("lord", "LORDCFG.EXE", interactions)
        visible = ANSI_CSI.sub(b"", transcript)
        success = b"THE GAME IS NOW REGISTERED" in visible.upper()
        # Terminal echo may contain the license. Never write this transcript.
        del transcript, visible, fields
        if not success:
            raise RuntimeError("LORD rejected the supplied registration")
        _atomic(marker, "Registered LORD license installed; credential withheld\n")

    @staticmethod
    def _wrapper(prefix: list[str], reads: list[str], writes: list[str],
                 command: list[str]) -> list[str]:
        result = list(prefix)
        for path in reads:
            if os.path.exists(path):
                result += ["--read", path]
        for path in writes:
            result += ["--write", path]
        return result + ["--"] + command

    def play(self, conn: socket.socket) -> None:
        peer = peer_credentials(conn)
        node = 0
        game = ""
        process: subprocess.Popen[bytes] | None = None
        master = -1
        temporary: Path | None = None
        started = time.monotonic()
        try:
            conn.settimeout(10.0)
            length = struct.unpack("!I", _exact(conn, 4))[0]
            if length < 2 or length > MAX_HANDSHAKE:
                raise RPCError("invalid game handshake")
            request = json.loads(_exact(conn, length).decode("ascii"))
            if not isinstance(request, dict) or set(request) != {
                    "v", "game", "token", "columns", "rows"} or request.get("v") != 1:
                raise RPCError("invalid game handshake")
            game = request.get("game")
            if not isinstance(game, str) or game not in self.enabled or game not in DOORS:
                raise RPCError("unknown or disabled game")
            columns = request.get("columns")
            rows = request.get("rows")
            if not isinstance(columns, int) or not 20 <= columns <= 240:
                raise RPCError("invalid terminal width")
            if not isinstance(rows, int) or not 10 <= rows <= 100:
                raise RPCError("invalid terminal height")
            ident = validate(self.session_socket, request.get("token"), peer[1])
            ready, limitation = self._ready(game)
            if not ready:
                raise RPCError(game + " has not been initialized by the Sysop")
            node = self._allocate(game)
            if game in ("lord", "tradewars"):
                self._drop_file(game, ident, node)
            temporary = Path("/tmp") / (
                f"mars-door-{int(ident['account_id'])}-{os.urandom(8).hex()}")
            temporary.mkdir(parents=True, mode=0o700)
            prefix, cwd, reads, writes, environment = self._command(
                game, ident, node, temporary)
            command = json.loads(environment.pop("MARS_COMMAND"))
            wrapped = self._wrapper(prefix, reads, writes, command)
            master, slave = pty.openpty()
            pty_columns, pty_rows = _pty_dimensions(game, columns, rows)
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", pty_rows, pty_columns, 0, 0))
            process = subprocess.Popen(wrapped, stdin=slave, stdout=slave, stderr=slave,
                                       cwd=str(cwd), env=environment, close_fds=True,
                                       start_new_session=True)
            os.close(slave)
            os.set_blocking(master, False)
            conn.settimeout(None)
            self._log(int(ident["account_id"]), str(ident["userid"]), game,
                      node, "start", limitation)
            send_frame(conn, b"A")
            selector = selectors.DefaultSelector()
            selector.register(conn, selectors.EVENT_READ)
            selector.register(master, selectors.EVENT_READ)
            tw_identity_sent = game != "tradewars"
            tw_output_tail = bytearray()
            while process.poll() is None:
                if time.monotonic() - started > MAX_SESSION_SECONDS:
                    raise RPCError("game session time limit reached")
                for key, _ in selector.select(1.0):
                    if key.fileobj == conn:
                        kind, payload = recv_frame(conn)
                        if kind == b"I":
                            if len(payload) > MAX_FRAME:
                                raise RPCError("game input is too large")
                            if tw_identity_sent:
                                os.write(master, payload)
                        elif kind == b"R" and len(payload) == 4:
                            new_rows, new_columns = struct.unpack("!HH", payload)
                            if 10 <= new_rows <= 100 and 20 <= new_columns <= 240:
                                next_columns, next_rows = _pty_dimensions(
                                    game, new_columns, new_rows)
                                if (next_columns, next_rows) != (pty_columns, pty_rows):
                                    pty_columns, pty_rows = next_columns, next_rows
                                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack(
                                        "HHHH", pty_rows, pty_columns, 0, 0))
                                    os.kill(process.pid, signal.SIGWINCH)
                        elif kind == b"C":
                            raise EOFError("client closed game")
                        else:
                            raise RPCError("invalid game input frame")
                    else:
                        try:
                            output = os.read(master, MAX_FRAME)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                output = b""
                            else:
                                raise
                        if output:
                            _send_terminal_output(conn, game, output)
                            if not tw_identity_sent:
                                tw_output_tail.extend(output)
                                if len(tw_output_tail) > 4096:
                                    del tw_output_tail[:-2048]
                                if b"what is your name?" in bytes(tw_output_tail).lower():
                                    os.write(master, str(ident["userid"]).encode("ascii") + b"\r")
                                    tw_identity_sent = True
            return_code = int(process.wait())
            send_frame(conn, b"E", struct.pack("!i", return_code))
            self._log(int(ident["account_id"]), str(ident["userid"]), game,
                      node, "exit", str(return_code))
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass
        except (RPCError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            try:
                send_frame(conn, b"X", str(exc).encode("utf-8")[:1000])
            except OSError:
                pass
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            if master >= 0:
                os.close(master)
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            if game and node:
                self._release(game, node)
            conn.close()


def _play_server(path: str, authority: DoorAuthority) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o666)
    listener.listen(32)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=authority.play, args=(conn,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--template-root", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--play-socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--enabled-doors", default=",".join(DOORS))
    parser.add_argument("--lord-registration-file")
    parser.add_argument("--lord-nodes", type=int, default=4)
    args = parser.parse_args()
    enabled = {item.strip().lower() for item in args.enabled_doors.split(",") if item.strip()}
    authority = DoorAuthority(
        args.root, args.template_root, args.session_socket, enabled,
        args.lord_registration_file, args.lord_nodes)
    threading.Thread(target=_play_server, args=(args.play_socket, authority),
                     daemon=True).start()
    serve(args.socket, authority.rpc)


if __name__ == "__main__":
    main()
