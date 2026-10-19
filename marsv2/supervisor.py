"""Root session supervisor.

The supervisor has no durable BBS mounts.  It consumes a one-use auth ticket,
creates an ephemeral session capability, attaches the existing PTY to a child,
then irreversibly drops that child to the account's stable numeric UID.
"""

from __future__ import annotations

import argparse
import array
import ctypes
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import traceback

from .rpc import request

PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
LIBC = ctypes.CDLL(None)
SESSION_HEARTBEAT_SECONDS = 30.0


class Supervisor:
    def __init__(self, socket_path: str, session_socket: str, session_root: str):
        self.socket_path = socket_path
        self.session_socket = session_socket
        self.session_root = session_root

    def handle(self, conn: socket.socket) -> None:
        token = None
        child = None
        session_dir = None
        heartbeat_stop = threading.Event()
        heartbeat_thread = None
        received: list[int] = []
        try:
            raw, ancillary, _, _ = conn.recvmsg(4096, socket.CMSG_SPACE(3 * 4))
            message = json.loads(raw.decode("ascii"))
            ticket = message.get("ticket")
            if not isinstance(ticket, str) or len(ticket) > 128:
                raise ValueError("invalid ticket")
            remote_ip = message.get("remote_ip", "")
            if remote_ip:
                if not isinstance(remote_ip, str) or len(remote_ip) > 45:
                    raise ValueError("invalid peer address")
                try:
                    remote_ip = str(ipaddress.ip_address(remote_ip))
                except ValueError as exc:
                    raise ValueError("invalid peer address") from exc
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(data[:3 * values.itemsize])
                    received.extend(values.tolist())
            if len(received) != 3:
                raise ValueError("PTY descriptors required")
            response = request(self.session_socket, "create",
                               {"ticket": ticket, "remote_ip": remote_ip})
            if not response["ok"]:
                reason = response.get("data", {}).get("error", "session unavailable")
                raise ValueError(str(reason))
            identity = dict(response["data"])
            token = str(identity.pop("token"))
            runtime_uid = int(identity["runtime_uid"])
            session_dir = os.path.join(self.session_root,
                                       f"{identity['account_id']}-{secrets.token_hex(12)}")
            os.mkdir(session_dir, 0o700)
            os.chown(session_dir, runtime_uid, runtime_uid)
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
            child = os.fork()
            if child == 0:
                try:
                    conn.close()
                    os.close(write_fd)
                    for target, source in enumerate(received):
                        os.dup2(source, target)
                    for source in received:
                        if source > 2:
                            os.close(source)
                    os.dup2(read_fd, 3)
                    os.set_inheritable(3, True)
                    if read_fd != 3:
                        os.close(read_fd)
                    os.setgroups([])
                    os.setgid(runtime_uid)
                    os.setuid(runtime_uid)
                    LIBC.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
                    LIBC.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
                    os.chdir(session_dir)
                    env = {"PATH": "/usr/bin", "TERM": os.environ.get("TERM", "xterm"),
                           "HOME": session_dir, "TMPDIR": session_dir,
                           "PYTHONPATH": "/opt/mars-v2", "MARS_V2_SESSION_FD": "3"}
                    for key in ("MARS_V2_DOWNLOAD_HOST", "MARS_V2_DOWNLOAD_PORT"):
                        if key in os.environ:
                            env[key] = os.environ[key]
                    os.execve("/usr/bin/python3", ["python3", "-m", "marsv2.ui"], env)
                except BaseException:
                    traceback.print_exc(file=sys.stderr)
                finally:
                    os._exit(127)
            os.close(read_fd)

            def heartbeat() -> None:
                while not heartbeat_stop.wait(SESSION_HEARTBEAT_SECONDS):
                    try:
                        response = request(self.session_socket, "touch", {}, token)
                    except Exception:
                        return
                    if not response.get("ok"):
                        return

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
            payload = json.dumps({"token": token, "identity": identity}, separators=(",", ":"))
            os.write(write_fd, payload.encode("utf-8"))
            os.close(write_fd)
            for source in received:
                os.close(source)
            received.clear()
            conn.sendall(b'{"ok":true}')
            _, status = os.waitpid(child, 0)
            exit_code = os.waitstatus_to_exitcode(status)
            conn.sendall(json.dumps({"exit": exit_code}).encode("ascii"))
        except Exception as exc:
            print(f"session supervisor error: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            try:
                conn.sendall(json.dumps({"ok": False, "error": str(exc)}).encode("ascii"))
            except OSError:
                pass
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)
            for source in received:
                try:
                    os.close(source)
                except OSError:
                    pass
            if token:
                try:
                    request(self.session_socket, "end", {}, token)
                except Exception:
                    pass
            if session_dir:
                # The supervisor intentionally lacks DAC_OVERRIDE.  It does retain
                # CAP_CHOWN solely so it can reclaim and remove an account-owned
                # ephemeral directory after an abnormal child exit.
                try:
                    os.chown(session_dir, 0, 0)
                except OSError:
                    pass
                shutil.rmtree(session_dir, ignore_errors=True)
            conn.close()

    def run(self) -> None:
        os.makedirs(os.path.dirname(self.socket_path), mode=0o750, exist_ok=True)
        os.makedirs(self.session_root, mode=0o700, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(self.socket_path)
        os.chown(self.socket_path, 0, 29999)
        os.chmod(self.socket_path, 0o660)
        listener.listen(64)
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--session-root", required=True)
    args = parser.parse_args()
    Supervisor(args.socket, args.session_socket, args.session_root).run()


if __name__ == "__main__":
    main()
