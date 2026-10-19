"""Apply an unprivileged Landlock policy immediately before a game exec."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import resource


PR_SET_NO_NEW_PRIVS = 38
PR_SET_DUMPABLE = 4
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

# Linux uapi/linux/landlock.h filesystem access bits through Linux 6.12.
ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14

READ = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
WRITE = (ACCESS_WRITE_FILE | ACCESS_REMOVE_DIR | ACCESS_REMOVE_FILE |
         ACCESS_MAKE_DIR | ACCESS_MAKE_REG | ACCESS_MAKE_SOCK |
         ACCESS_MAKE_FIFO | ACCESS_MAKE_SYM | ACCESS_REFER | ACCESS_TRUNCATE)
HANDLED = READ | WRITE | ACCESS_MAKE_CHAR | ACCESS_MAKE_BLOCK


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int)]


def _syscalls() -> tuple[int, int, int]:
    # Landlock syscall numbers are identical on the Linux architectures used
    # by the project (x86_64 and aarch64).
    return 444, 445, 446


def apply(read_paths: list[str], write_paths: list[str]) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    create, add, restrict = _syscalls()
    version = libc.syscall(create, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if version < 1:
        if ctypes.get_errno() in (errno.ENOSYS, errno.EOPNOTSUPP):
            return False
        raise OSError(ctypes.get_errno(), "cannot query Landlock")
    ruleset = Ruleset(HANDLED)
    ruleset_fd = libc.syscall(create, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "cannot create Landlock ruleset")
    try:
        for path, access in [(item, READ) for item in read_paths] + [
                (item, READ | WRITE) for item in write_paths]:
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, descriptor)
                if libc.syscall(add, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH,
                                ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "cannot add Landlock path")
            finally:
                os.close(descriptor)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot set no-new-privileges")
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
        if libc.syscall(restrict, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot enforce Landlock ruleset")
    finally:
        os.close(ruleset_fd)
    return True


def _parents(path: str) -> list[str]:
    result = []
    current = os.path.dirname(path.rstrip("/"))
    while current not in ("", "/"):
        result.append(current)
        current = os.path.dirname(current)
    return list(reversed(result))


def bubblewrap(read_paths: list[str], write_paths: list[str],
               command: list[str], environment: dict[str, str]) -> None:
    """Fall back to a user/mount namespace when this kernel lacks Landlock."""
    args = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid",
            "--unshare-net", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session", "--clearenv",
            # Unraid's AppArmor policy permits an unprivileged mount namespace
            # but denies mounting a new procfs.  The container already has a
            # private PID namespace; expose its proc/dev read-only while the
            # nested user namespace prevents sibling ptrace and signalling.
            "--ro-bind", "/proc", "/proc", "--dev", "/dev",
            # DOSEMU2 uses POSIX semaphores; give every game a private shm.
            "--tmpfs", "/dev/shm",
            "--tmpfs", "/tmp"]
    created = {"/tmp", "/proc", "/dev"}
    for path, writable in [(item, False) for item in read_paths] + [
            (item, True) for item in write_paths]:
        if path in ("/proc", "/dev", "/tmp"):
            continue
        for parent in _parents(path):
            if parent not in created and not any(
                    parent == root or parent.startswith(root.rstrip("/") + "/")
                    for root in created):
                args += ["--dir", parent]
                created.add(parent)
        args += ["--bind" if writable else "--ro-bind", path, path]
        created.add(path)
    for key, value in environment.items():
        if value:
            args += ["--setenv", key, value]
    cwd = os.getcwd()
    visible = read_paths + write_paths
    if os.path.isdir(cwd) and any(
            cwd == root or cwd.startswith(root.rstrip("/") + "/")
            for root in visible):
        args += ["--chdir", cwd]
    else:
        args += ["--chdir", "/"]
    os.execv(args[0], args + ["--"] + command)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read", action="append", default=[])
    parser.add_argument("--write", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        raise SystemExit("missing command")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # TradeWars' BigBang and live database open many indexed files at once.
    # The container remains bounded, but 64 descriptors corrupts initialization.
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    # DOSEMU2 uses a small worker pool for rendering, MIDI, and emulation.
    # The outer cgroup still enforces the stricter aggregate 96-PID budget.
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    # DOSEMU2 creates a sparse address-space backing file larger than the
    # eventual game data.  A 64 MiB file limit aborted before DOS booted.
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024,
                                               512 * 1024 * 1024))
    environment = {
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "TERM": os.environ.get("TERM", "ansi"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": os.environ.get("PYTHONPATH", "/opt/mars-v2"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
        "DOSEMU_LIB_DIR": os.environ.get("DOSEMU_LIB_DIR", ""),
        "DOSEMU_CONF_DIR": os.environ.get("DOSEMU_CONF_DIR", ""),
        "DOSEMU_HDIMAGE_DIR": os.environ.get("DOSEMU_HDIMAGE_DIR", ""),
        "DOSEMU_LAX_CHECKING": "1",
    }
    if apply(args.read, args.write):
        os.execve(args.command[0], args.command, environment)
    bubblewrap(args.read, args.write, args.command, environment)


if __name__ == "__main__":
    main()
