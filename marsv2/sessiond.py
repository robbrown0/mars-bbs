"""Ephemeral session and presence authority."""

from __future__ import annotations

import argparse
import ipaddress
import secrets
import time

from .rpc import RPCError, request, serve
from .validate import integer, text

MAX_SESSIONS = 48
MAX_SESSIONS_PER_ACCOUNT = 3
SESSION_LEASE_SECONDS = 90.0


class Sessions:
    def __init__(self, auth_socket: str):
        self.auth_socket = auth_socket
        self.sessions: dict[str, dict[str, object]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [token for token, session in self.sessions.items()
                   if float(session["expires"]) < now]
        for token in expired:
            del self.sessions[token]

    def _refresh(self, token: str) -> dict[str, object]:
        session = self.sessions.get(token)
        if session is None:
            raise RPCError("invalid session")
        response = request(self.auth_socket, "authorization",
                           {"account_id": session["account_id"]})
        if not response["ok"]:
            self.sessions.pop(token, None)
            raise RPCError("invalid session")
        refreshed = dict(response["data"])
        # Runtime identity is immutable for an account. Refuse an inconsistent
        # authority response instead of changing the UID attached to a token.
        if refreshed.get("runtime_uid") != session.get("runtime_uid"):
            self.sessions.pop(token, None)
            raise RPCError("session identity mismatch")
        # An administrative reset must revoke sessions which predate the reset,
        # even if the administrator's best-effort disconnect RPC was interrupted.
        # A newly authenticated temporary-password session is created with this
        # flag already set and remains valid only long enough to change it.
        if (bool(refreshed.get("password_change_required")) and
                not bool(session.get("password_change_required"))):
            self.sessions.pop(token, None)
            raise RPCError("session credentials changed")
        for key in ("userid", "display_name", "is_admin", "roles", "capabilities",
                    "password_change_required"):
            session[key] = refreshed[key]
        return session

    def handle(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = req["op"]
        data = req["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        self._prune()
        try:
            if op == "health":
                return {"status": "ok", "sessions": len(self.sessions)}
            if op == "create":
                ticket = text(data.get("ticket"), "ticket", 128, 32)
                remote_ip = data.get("remote_ip", "")
                if remote_ip:
                    if not isinstance(remote_ip, str) or len(remote_ip) > 45:
                        raise RPCError("invalid peer address")
                    try:
                        remote_ip = str(ipaddress.ip_address(remote_ip))
                    except ValueError as exc:
                        raise RPCError("invalid peer address") from exc
                response = request(self.auth_socket, "consume_ticket", {"ticket": ticket})
                if not response["ok"]:
                    raise RPCError("invalid ticket")
                ident = dict(response["data"])
                if len(self.sessions) >= MAX_SESSIONS:
                    raise RPCError("session capacity reached")
                current = sum(1 for item in self.sessions.values()
                              if item["account_id"] == ident["account_id"])
                if current >= MAX_SESSIONS_PER_ACCOUNT:
                    raise RPCError("account session limit reached")
                token = secrets.token_urlsafe(48)
                now = time.monotonic()
                ident.update({"created": now,
                              "last_activity": now,
                              "activity_mode": "Lobby",
                              "expires": now + SESSION_LEASE_SECONDS,
                              "supervisor_pid": peer[0],
                              "remote_ip": remote_ip,
                              "session_id": secrets.token_hex(8)})
                self.sessions[token] = ident
                result = {key: ident[key] for key in
                          ("account_id", "userid", "display_name", "runtime_uid", "is_admin",
                           "roles", "capabilities", "password_change_required")}
                result["last_login"] = ident.get("last_login", 0)
                result["token"] = token
                return result
            if op in ("validate", "touch", "end"):
                token = text(req.get("token"), "token", 128, 32)
                session = self._refresh(token)
                expected_uid = data.get("peer_uid")
                if expected_uid is not None:
                    expected_uid = integer(expected_uid, "peer uid", 1, 2_147_483_647)
                    if expected_uid != session["runtime_uid"]:
                        raise RPCError("session identity mismatch")
                if op == "end":
                    del self.sessions[token]
                    return {"ended": True}
                if op == "touch":
                    session["expires"] = time.monotonic() + SESSION_LEASE_SECONDS
                return {key: session[key] for key in
                        ("account_id", "userid", "display_name", "runtime_uid", "is_admin",
                         "roles", "capabilities", "password_change_required")}
            if op == "activity":
                token = text(req.get("token"), "token", 128, 32)
                session = self._refresh(token)
                if peer[1] != session["runtime_uid"]:
                    raise RPCError("session identity mismatch")
                session["last_activity"] = time.monotonic()
                if "mode" in data:
                    session["activity_mode"] = text(
                        data.get("mode"), "activity mode", 32, 1)
                return {"recorded": True, "activity_mode": session["activity_mode"]}
            if op == "online":
                token = text(req.get("token"), "token", 128, 32)
                self._refresh(token)
                users = sorted({str(item["userid"]) for item in self.sessions.values()})
                return {"users": users}
            if op == "online_details":
                token = text(req.get("token"), "token", 128, 32)
                self._refresh(token)
                now = time.monotonic()
                grouped: dict[str, dict[str, float | int | str]] = {}
                for session in self.sessions.values():
                    name = str(session["userid"])
                    created = float(session["created"])
                    last_activity = float(session.get("last_activity", created))
                    current = grouped.setdefault(name, {
                        "created": created,
                        "last_activity": last_activity,
                        "sessions": 0,
                        "activity_mode": str(session.get("activity_mode", "Online")),
                    })
                    current["created"] = min(float(current["created"]), created)
                    if last_activity >= float(current["last_activity"]):
                        current["last_activity"] = last_activity
                        current["activity_mode"] = str(
                            session.get("activity_mode", "Online"))
                    current["sessions"] = int(current["sessions"]) + 1
                return {"users": [
                    {"userid": name,
                     "sessions": int(grouped[name]["sessions"]),
                     "connected_seconds": max(
                         0, int(now - float(grouped[name]["created"]))),
                     "idle_seconds": max(
                         0, int(now - float(grouped[name]["last_activity"]))),
                     "activity": str(grouped[name]["activity_mode"]) }
                    for name in sorted(grouped)
                ]}
            if op in ("admin_sessions", "admin_end", "admin_end_account"):
                token = text(req.get("token"), "token", 128, 32)
                actor = self._refresh(token)
                capabilities = set(actor.get("capabilities", []))
                if op in ("admin_sessions", "admin_end") and \
                        "session.manage" not in capabilities:
                    raise RPCError("permission denied")
                if op == "admin_sessions":
                    now = time.monotonic()
                    return {"sessions": [
                        {"session_id": item["session_id"], "userid": item["userid"],
                         "display_name": item["display_name"],
                         "connected_seconds": max(0, int(now - float(item["created"]))),
                         "idle_seconds": max(0, int(now - float(item["last_activity"]))),
                         "activity": str(item.get("activity_mode", "Online")),
                         "current": item is actor}
                        for item in sorted(self.sessions.values(),
                                           key=lambda value: (str(value["userid"]),
                                                              str(value["session_id"])))
                    ]}
                if op == "admin_end_account":
                    if not ({"account.password_reset", "account.delete"}
                            & capabilities):
                        raise RPCError("permission denied")
                    account_id = integer(data.get("account_id"), "account id")
                    if account_id == actor["account_id"]:
                        raise RPCError("cannot terminate the current admin account")
                    targets = [value for value, item in self.sessions.items()
                               if item["account_id"] == account_id]
                    for value in targets:
                        self.sessions.pop(value, None)
                    return {"ended": len(targets)}
                target_id = text(data.get("session_id"), "session id", 16, 16)
                target_token = next((value for value, item in self.sessions.items()
                                     if item["session_id"] == target_id), None)
                if target_token is None:
                    raise RPCError("no such session")
                if target_token == token:
                    raise RPCError("cannot terminate the current admin session")
                target = self.sessions[target_token]
                audit = request(self.auth_socket, "service_audit", {
                    "actor_id": actor["account_id"], "actor_user": actor["userid"],
                    "action": "terminate", "target": str(target["userid"]),
                    "detail": "session_id=" + target_id,
                })
                if not audit["ok"]:
                    raise RPCError("audit service unavailable")
                self.sessions.pop(target_token)
                return {"ended": True, "userid": target["userid"]}
        except ValueError as exc:
            raise RPCError(str(exc)) from exc
        raise RPCError("unknown operation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--auth-socket", required=True)
    args = parser.parse_args()
    serve(args.socket, Sessions(args.auth_socket).handle)


if __name__ == "__main__":
    main()
