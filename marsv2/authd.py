"""Account and password authority for Mars V2."""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import stat
import time

from .authz import (ADMIN_CONSOLE_CAPABILITIES, ASSIGNABLE_ROLES,
                    ROLE_CAPABILITIES, ROLE_DESCRIPTIONS, capabilities_for)
from .db import connect
from .rpc import RPCError, serve
from .sodium import hash_password, verify_password
from .timezones import validate_timezone
from .validate import email_address, password, text, userid


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  userid TEXT NOT NULL COLLATE NOCASE UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  runtime_uid INTEGER NOT NULL UNIQUE,
  is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),
  login_enabled INTEGER NOT NULL DEFAULT 1 CHECK(login_enabled IN (0,1)),
  password_change_required INTEGER NOT NULL DEFAULT 0
    CHECK(password_change_required IN (0,1)),
  deletion_pending INTEGER NOT NULL DEFAULT 0 CHECK(deletion_pending IN (0,1)),
  cleanup_exempt INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_exempt IN (0,1)),
  last_login INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
  account_id INTEGER PRIMARY KEY,
  email TEXT NOT NULL DEFAULT '',
  profile_text TEXT NOT NULL DEFAULT '',
  signature TEXT NOT NULL DEFAULT '',
  timezone TEXT NOT NULL DEFAULT 'UTC',
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS account_roles (
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  PRIMARY KEY(account_id,role)
);
CREATE TABLE IF NOT EXISTS admin_audit (
  id INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor_id INTEGER NOT NULL,
  actor_user TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  result TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS admin_audit_created_idx ON admin_audit(created_at,id);
"""

# The mail authority has the only OS identity permitted to enumerate accounts.
# Individual lookups remain available for the existing Talk/profile workflow.
MAIL_SERVICE_UID = 21003
SESSION_SERVICE_UID = 21002
TRUSTED_AUDIT_SERVICES = {SESSION_SERVICE_UID: "session"}


class Authority:
    def __init__(self, db_path: str):
        self.db = connect(db_path)
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(accounts)")}
        if "login_enabled" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE accounts ADD COLUMN login_enabled INTEGER NOT NULL DEFAULT 1")
        if "password_change_required" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE accounts ADD COLUMN password_change_required "
                    "INTEGER NOT NULL DEFAULT 0 CHECK(password_change_required IN (0,1))")
        if "deletion_pending" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE accounts ADD COLUMN deletion_pending INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(deletion_pending IN (0,1))")
        if "cleanup_exempt" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE accounts ADD COLUMN cleanup_exempt INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(cleanup_exempt IN (0,1))")
        if "last_login" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE accounts ADD COLUMN last_login INTEGER NOT NULL DEFAULT 0")
        profile_columns = {row["name"] for row in self.db.execute(
            "PRAGMA table_info(profiles)")}
        if "email" not in profile_columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE profiles ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "timezone" not in profile_columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE profiles ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        if "signature" not in profile_columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE profiles ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO profiles(account_id,email,profile_text,timezone,signature) "
                "SELECT id,'','','UTC','' FROM accounts")
            # One-way, idempotent migration from the original Boolean V2
            # privilege flag.  The column remains during the compatibility
            # period, but all new authorization decisions use roles.
            self.db.execute(
                "INSERT OR IGNORE INTO account_roles(account_id,role) "
                "SELECT a.id,CASE WHEN a.is_admin=1 THEN 'Sysop' ELSE 'Basic-1' END "
                "FROM accounts a WHERE NOT EXISTS "
                "(SELECT 1 FROM account_roles ar WHERE ar.account_id=a.id)")
            # Remove role names that never gained an enforceable operation.
            # An account that had only a placeholder role becomes an ordinary
            # member rather than being left without a role.
            placeholders = [role for role in (
                "Basic-2", "Basic-3", "Basic-4", "WelcomeMgr", "SeeCloak",
                "Cloak", "ChatOp", "FileMgr", "InfoMgr")]
            self.db.executemany("DELETE FROM account_roles WHERE role=?",
                                [(role,) for role in placeholders])
            self.db.execute(
                "INSERT OR IGNORE INTO account_roles(account_id,role) "
                "SELECT a.id,'Basic-1' FROM accounts a WHERE NOT EXISTS "
                "(SELECT 1 FROM account_roles ar WHERE ar.account_id=a.id)")
            self.db.execute(
                "DELETE FROM account_roles WHERE role='Basic-1' AND account_id IN "
                "(SELECT account_id FROM account_roles WHERE role<>'Basic-1')")
            self.db.execute(
                "DELETE FROM account_roles WHERE role<>'Sysop' AND account_id IN "
                "(SELECT account_id FROM account_roles WHERE role='Sysop')")
        self.tickets: dict[str, tuple[float, dict[str, object]]] = {}
        self.failures: dict[str, tuple[int, float]] = {}
        self.last_registration = 0.0

    def _roles(self, account_id: int) -> list[str]:
        return [str(row["role"]) for row in self.db.execute(
            "SELECT role FROM account_roles WHERE account_id=? ORDER BY role", (account_id,))]

    def _identity(self, row: sqlite3.Row) -> dict[str, object]:
        roles = self._roles(int(row["id"]))
        return {"account_id": row["id"], "userid": row["userid"],
                "display_name": row["display_name"],
                "runtime_uid": row["runtime_uid"],
                "is_admin": "Sysop" in roles,
                "roles": roles, "capabilities": capabilities_for(roles),
                "last_login": int(row["last_login"]),
                "password_change_required": bool(row["password_change_required"])}

    def _actor(self, peer_uid: int) -> tuple[sqlite3.Row, dict[str, object]]:
        row = self.db.execute(
            "SELECT * FROM accounts WHERE runtime_uid=? AND login_enabled=1",
            (peer_uid,)).fetchone()
        if row is None:
            raise RPCError("authentication required")
        return row, self._identity(row)

    @staticmethod
    def _require(identity: dict[str, object], capability: str) -> None:
        if capability not in identity.get("capabilities", []):
            raise RPCError("permission denied")

    def _reauth(self, actor: sqlite3.Row, value: object) -> None:
        clear = password(value)
        if not verify_password(str(actor["password_hash"]), clear):
            raise RPCError("administrator reauthentication failed")

    def _audit(self, actor: dict[str, object], action: str, target: str,
               result: str = "success", detail: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO admin_audit(created_at,actor_id,actor_user,action,target,result,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (int(time.time()), actor["account_id"], actor["userid"], action,
                 text(target, "audit target", 80), result,
                 text(detail, "audit detail", 160)))

    def _admin_target(self, actor: dict[str, object], value: object) -> sqlite3.Row:
        target_name = userid(value)
        target = self.db.execute(
            "SELECT * FROM accounts WHERE userid=?", (target_name,)).fetchone()
        if target is None:
            raise RPCError("no such user")
        target_roles = self._roles(int(target["id"]))
        if "Sysop" in target_roles and "Sysop" not in actor.get("roles", []):
            raise RPCError("only a Sysop may manage a Sysop account")
        return target

    def _protect_final_sysop(self, target: sqlite3.Row) -> None:
        if "Sysop" not in self._roles(int(target["id"])):
            return
        remaining = self.db.execute(
            "SELECT count(*) FROM accounts a JOIN account_roles r ON r.account_id=a.id "
            "WHERE r.role='Sysop' AND a.login_enabled=1 AND a.id<>?",
            (target["id"],)).fetchone()[0]
        if not remaining:
            raise RPCError("cannot remove the last enabled Sysop")

    def _ticket(self, row: sqlite3.Row) -> str:
        value = secrets.token_urlsafe(32)
        self.tickets[value] = (time.monotonic() + 15.0, self._identity(row))
        return value

    def add_account(self, name: str, clear: str, display: str, admin: bool = False,
                    email: str = "", profile_text: str = "", timezone: str = "UTC",
                    password_change_required: bool = False) -> None:
        name = userid(name)
        clear = password(clear)
        display = text(display, "display name", 25, 1)
        email = email_address(email)
        profile_text = text(profile_text, "personal information", 240)
        timezone = validate_timezone(timezone)
        now = int(time.time())
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO accounts(userid,display_name,password_hash,runtime_uid,is_admin,"
                "password_change_required,created_at) VALUES(?,?,?,0,?,?,?)",
                (name, display, hash_password(clear), int(admin),
                 int(password_change_required), now))
            runtime_uid = 30000 + int(cursor.lastrowid)
            if runtime_uid > 60000:
                raise RuntimeError("runtime UID range exhausted")
            self.db.execute("UPDATE accounts SET runtime_uid=? WHERE id=?",
                            (runtime_uid, cursor.lastrowid))
            self.db.execute(
                "INSERT INTO profiles(account_id,email,profile_text,timezone) VALUES(?,?,?,?)",
                (cursor.lastrowid, email, profile_text, timezone))
            self.db.execute(
                "INSERT INTO account_roles(account_id,role) VALUES(?,?)",
                (cursor.lastrowid, "Sysop" if admin else "Basic-1"))

    def handle(self, request: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = request["op"]
        data = request["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        try:
            if op == "health":
                self.db.execute("SELECT 1").fetchone()
                return {"status": "ok"}
            if op == "login":
                name = userid(data.get("userid"))
                clear = password(data.get("password"))
                count, until = self.failures.get(name, (0, 0.0))
                now = time.monotonic()
                if now < until:
                    raise RPCError("authentication failed")
                row = self.db.execute("SELECT * FROM accounts WHERE userid=?", (name,)).fetchone()
                valid = (row is not None and bool(row["login_enabled"])
                         and verify_password(row["password_hash"], clear))
                if not valid:
                    count += 1
                    delay = min(8.0, 0.25 * (2 ** min(count, 5)))
                    self.failures[name] = (count, now + delay)
                    raise RPCError("authentication failed")
                self.failures.pop(name, None)
                ticket = self._ticket(row)
                with self.db:
                    self.db.execute("UPDATE accounts SET last_login=? WHERE id=?",
                                    (int(time.time()), row["id"]))
                return {"ticket": ticket}
            if op == "consume_ticket":
                ticket = text(data.get("ticket"), "ticket", 128, 32)
                record = self.tickets.pop(ticket, None)
                if record is None or record[0] < time.monotonic():
                    raise RPCError("invalid ticket")
                return dict(record[1])
            if op == "authorization":
                if peer[1] != SESSION_SERVICE_UID:
                    raise RPCError("authentication required")
                account_id = data.get("account_id")
                if isinstance(account_id, bool) or not isinstance(account_id, int):
                    raise RPCError("invalid account")
                row = self.db.execute(
                    "SELECT * FROM accounts WHERE id=? AND login_enabled=1",
                    (account_id,)).fetchone()
                if row is None:
                    raise RPCError("account unavailable")
                return self._identity(row)
            if op == "service_audit":
                service = TRUSTED_AUDIT_SERVICES.get(peer[1])
                if service is None:
                    raise RPCError("authentication required")
                actor_id = data.get("actor_id")
                if isinstance(actor_id, bool) or not isinstance(actor_id, int):
                    raise RPCError("invalid audit actor")
                actor = self.db.execute(
                    "SELECT id,userid FROM accounts WHERE id=?", (actor_id,)).fetchone()
                if actor is None or actor["userid"] != data.get("actor_user"):
                    raise RPCError("invalid audit actor")
                action = text(data.get("action"), "audit action", 48, 1)
                target = text(data.get("target"), "audit target", 80, 1)
                detail = text(data.get("detail", ""), "audit detail", 160)
                with self.db:
                    self.db.execute(
                        "INSERT INTO admin_audit(created_at,actor_id,actor_user,action,target,result,detail) "
                        "VALUES(?,?,?,?,?,'success',?)",
                        (int(time.time()), actor["id"], actor["userid"],
                         service + "." + action, target, detail))
                return {"recorded": True}
            if op == "register":
                now = time.monotonic()
                if now - self.last_registration < 2.0:
                    raise RPCError("registration temporarily unavailable")
                name = userid(data.get("userid"))
                clear = password(data.get("password"))
                display = text(data.get("display_name"), "display name", 25, 1)
                email = email_address(data.get("email", ""))
                profile_text = text(data.get("profile_text", ""),
                                    "personal information", 240)
                timezone = validate_timezone(data.get("timezone", "UTC"))
                try:
                    self.add_account(name, clear, display, False, email, profile_text, timezone)
                except sqlite3.IntegrityError as exc:
                    raise RPCError("userid unavailable") from exc
                self.last_registration = now
                return {"registered": True}
            if op == "lookup":
                name = userid(data.get("userid"))
                row = self.db.execute(
                    "SELECT a.id,a.userid,a.display_name,COALESCE(p.profile_text,'') profile_text "
                    "FROM accounts a LEFT JOIN profiles p ON p.account_id=a.id "
                    "WHERE a.userid=?", (name,)).fetchone()
                if row is None:
                    raise RPCError("no such user")
                return {"account_id": row["id"], "userid": row["userid"],
                        "display_name": row["display_name"],
                        "profile_text": row["profile_text"]}
            if op == "member_directory":
                self._actor(peer[1])
                query = text(data.get("query", ""), "directory filter", 12)
                offset = data.get("offset", 0)
                if isinstance(offset, bool) or not isinstance(offset, int) \
                        or offset < 0 or offset > 1_000_000:
                    raise RPCError("invalid directory offset")
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = escaped + "%"
                rows = self.db.execute(
                    "SELECT a.userid,a.display_name,a.last_login,a.created_at,"
                    "COALESCE(p.profile_text,'') profile_text FROM accounts a "
                    "LEFT JOIN profiles p ON p.account_id=a.id "
                    "WHERE a.login_enabled=1 AND a.userid LIKE ? ESCAPE '\\' "
                    "ORDER BY a.userid COLLATE NOCASE LIMIT 101 OFFSET ?",
                    (pattern, offset)).fetchall()
                more = len(rows) > 100
                return {"users": [dict(row) for row in rows[:100]],
                        "next_offset": offset + min(len(rows), 100) if more else None}
            if op == "directory":
                # This is the deliberately public portion of an account.  In
                # particular, never add account IDs, runtime UIDs, email,
                # privilege state, or password material to this response.
                if peer[1] != MAIL_SERVICE_UID:
                    raise RPCError("authentication required")
                offset = data.get("offset", 0)
                if isinstance(offset, bool) or not isinstance(offset, int) \
                        or offset < 0 or offset > 1_000_000:
                    raise RPCError("invalid directory offset")
                rows = self.db.execute(
                    "SELECT userid,display_name FROM accounts "
                    "WHERE login_enabled=1 ORDER BY userid COLLATE NOCASE LIMIT 201 OFFSET ?",
                    (offset,)).fetchall()
                more = len(rows) > 200
                visible = rows[:200]
                return {
                    "users": [{"userid": row["userid"],
                               "display_name": row["display_name"]}
                              for row in visible],
                    "next_offset": offset + len(visible) if more else None,
                }
            if op == "mail_recipient":
                if peer[1] != MAIL_SERVICE_UID:
                    raise RPCError("authentication required")
                name = userid(data.get("userid"))
                row = self.db.execute(
                    "SELECT id,userid FROM accounts WHERE userid=? AND login_enabled=1",
                    (name,)).fetchone()
                if row is None:
                    raise RPCError("no such user")
                return {"account_id": row["id"], "userid": row["userid"]}
            if op == "profile":
                row = self.db.execute(
                    "SELECT a.userid,a.display_name,a.is_admin,a.last_login,p.email,p.profile_text,"
                    "p.timezone,p.signature "
                    "FROM accounts a JOIN profiles p ON p.account_id=a.id "
                    "WHERE a.runtime_uid=? AND a.login_enabled=1", (peer[1],)).fetchone()
                if row is None:
                    raise RPCError("authentication required")
                roles = self._roles(int(self.db.execute(
                    "SELECT id FROM accounts WHERE runtime_uid=?", (peer[1],)).fetchone()[0]))
                account = self.db.execute(
                    "SELECT * FROM accounts WHERE runtime_uid=?", (peer[1],)).fetchone()
                return {"userid": row["userid"], "display_name": row["display_name"],
                        "email": row["email"], "profile_text": row["profile_text"],
                        "timezone": row["timezone"], "signature": row["signature"],
                        "last_login": int(row["last_login"]),
                        "is_admin": "Sysop" in roles, "roles": roles,
                        "capabilities": capabilities_for(roles),
                        "password_change_required": bool(
                            account["password_change_required"])}
            if op == "update_profile":
                display = text(data.get("display_name"), "display name", 25, 1)
                email = email_address(data.get("email", ""))
                row = self.db.execute(
                    "SELECT a.id,p.timezone,p.signature FROM accounts a JOIN profiles p ON p.account_id=a.id "
                    "WHERE a.runtime_uid=? AND a.login_enabled=1",
                    (peer[1],)).fetchone()
                if row is None:
                    raise RPCError("authentication required")
                profile_text = text(data.get("profile_text", ""),
                                    "personal information", 240)
                signature = text(data.get("signature", row["signature"]),
                                 "signature", 320, 0, True)
                if signature.count("\n") > 3:
                    raise RPCError("signature may contain at most four lines")
                timezone = validate_timezone(data.get("timezone", row["timezone"]))
                with self.db:
                    self.db.execute("UPDATE accounts SET display_name=? WHERE id=?",
                                    (display, row["id"]))
                    self.db.execute(
                        "UPDATE profiles SET email=?,profile_text=?,timezone=?,signature=? "
                        "WHERE account_id=?",
                        (email, profile_text, timezone, signature, row["id"]))
                return {"display_name": display, "email": email,
                        "profile_text": profile_text, "timezone": timezone,
                        "signature": signature}
            if op == "change_password":
                old = password(data.get("old_password"))
                new = password(data.get("new_password"))
                row = self.db.execute(
                    "SELECT * FROM accounts WHERE runtime_uid=? AND login_enabled=1",
                    (peer[1],)).fetchone()
                if row is None or not verify_password(row["password_hash"], old):
                    raise RPCError("authentication failed")
                with self.db:
                    self.db.execute(
                        "UPDATE accounts SET password_hash=?,password_change_required=0 "
                        "WHERE id=?", (hash_password(new), row["id"]))
                return {"changed": True}
            if op == "admin_roles":
                _row, actor = self._actor(peer[1])
                if not ADMIN_CONSOLE_CAPABILITIES.intersection(
                        actor.get("capabilities", [])):
                    raise RPCError("permission denied")
                return {"roles": [
                    {"name": role, "description": ROLE_DESCRIPTIONS[role],
                     "capabilities": sorted(ROLE_CAPABILITIES[role])}
                    for role in ASSIGNABLE_ROLES]}
            if op == "admin_accounts":
                _row, actor = self._actor(peer[1])
                self._require(actor, "account.view")
                rows = self.db.execute(
                    "SELECT a.id,a.userid,a.display_name,a.login_enabled,a.deletion_pending,"
                    "a.password_change_required,a.cleanup_exempt,a.last_login,a.created_at,"
                    "group_concat(ar.role, ',') roles FROM accounts a "
                    "LEFT JOIN account_roles ar ON ar.account_id=a.id "
                    "GROUP BY a.id ORDER BY a.userid COLLATE NOCASE LIMIT 500").fetchall()
                return {"accounts": [
                    {"id": row["id"], "userid": row["userid"],
                     "display_name": row["display_name"],
                     "login_enabled": bool(row["login_enabled"]),
                     "deletion_pending": bool(row["deletion_pending"]),
                     "password_change_required": bool(row["password_change_required"]),
                     "cleanup_exempt": bool(row["cleanup_exempt"]),
                     "last_login": int(row["last_login"]),
                     "created_at": row["created_at"],
                     "roles": sorted(str(row["roles"] or "").split(","))}
                    for row in rows]}
            if op == "admin_create_account":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.manage")
                name = userid(data.get("userid"))
                display = text(data.get("display_name"), "display name", 25, 1)
                temporary = password(data.get("new_password"))
                self._reauth(actor_row, data.get("password"))
                try:
                    self.add_account(name, temporary, display,
                                     password_change_required=True)
                except sqlite3.IntegrityError as exc:
                    raise RPCError("userid unavailable") from exc
                self._audit(actor, "account.create", name,
                            detail="temporary password; forced change=true")
                return {"userid": name, "password_change_required": True}
            if op == "admin_inactive":
                _actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.view")
                days = data.get("days", 90)
                if isinstance(days, bool) or not isinstance(days, int) or days < 1 or days > 3650:
                    raise RPCError("invalid inactivity interval")
                cutoff = int(time.time()) - days * 86400
                rows = self.db.execute(
                    "SELECT userid,display_name,last_login,created_at,cleanup_exempt,login_enabled "
                    "FROM accounts WHERE cleanup_exempt=0 AND "
                    "(CASE WHEN last_login=0 THEN created_at ELSE last_login END)<? "
                    "ORDER BY CASE WHEN last_login=0 THEN created_at ELSE last_login END LIMIT 500",
                    (cutoff,)).fetchall()
                return {"days": days, "accounts": [dict(row) for row in rows]}
            if op == "admin_set_cleanup_exempt":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.manage")
                exempt = data.get("exempt")
                if not isinstance(exempt, bool):
                    raise RPCError("invalid cleanup exemption")
                target = self._admin_target(actor, data.get("userid"))
                self._reauth(actor_row, data.get("password"))
                with self.db:
                    self.db.execute("UPDATE accounts SET cleanup_exempt=? WHERE id=?",
                                    (int(exempt), target["id"]))
                self._audit(actor, "account.cleanup_exempt", str(target["userid"]),
                            detail="exempt=" + str(exempt).lower())
                return {"userid": target["userid"], "cleanup_exempt": exempt}
            if op == "admin_set_login":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.manage")
                enabled = data.get("enabled")
                if not isinstance(enabled, bool):
                    raise RPCError("invalid account status")
                target = self._admin_target(actor, data.get("userid"))
                target_roles = self._roles(int(target["id"]))
                if enabled and bool(target["deletion_pending"]):
                    raise RPCError("account deletion is pending")
                if not enabled and int(target["id"]) == int(actor_row["id"]):
                    raise RPCError("cannot disable your current account")
                if not enabled and "Sysop" in target_roles:
                    self._protect_final_sysop(target)
                self._reauth(actor_row, data.get("password"))
                with self.db:
                    self.db.execute("UPDATE accounts SET login_enabled=? WHERE id=?",
                                    (int(enabled), target["id"]))
                self._audit(actor, "account.enable" if enabled else "account.disable",
                            str(target["userid"]))
                return {"account_id": target["id"], "userid": target["userid"],
                        "login_enabled": enabled}
            if op == "admin_set_roles":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.roles")
                roles = data.get("roles")
                if not isinstance(roles, list) or not roles or len(roles) > len(ASSIGNABLE_ROLES):
                    raise RPCError("invalid role list")
                if any(not isinstance(role, str) or role not in ROLE_CAPABILITIES
                       for role in roles) or len(set(roles)) != len(roles):
                    raise RPCError("invalid role list")
                target = self._admin_target(actor, data.get("userid"))
                if bool(target["deletion_pending"]):
                    raise RPCError("account deletion is pending")
                selected = sorted(set(roles))
                if "Sysop" in selected:
                    selected = ["Sysop"]
                elif len(selected) > 1 and "Basic-1" in selected:
                    selected.remove("Basic-1")
                old_roles = self._roles(int(target["id"]))
                if "Sysop" in old_roles and "Sysop" not in selected:
                    self._protect_final_sysop(target)
                self._reauth(actor_row, data.get("password"))
                with self.db:
                    self.db.execute("DELETE FROM account_roles WHERE account_id=?", (target["id"],))
                    self.db.executemany(
                        "INSERT INTO account_roles(account_id,role) VALUES(?,?)",
                        [(target["id"], role) for role in selected])
                    self.db.execute("UPDATE accounts SET is_admin=? WHERE id=?",
                                    (int("Sysop" in selected), target["id"]))
                self._audit(actor, "account.roles", str(target["userid"]),
                            detail="roles=" + ",".join(selected))
                return {"userid": target["userid"], "roles": selected}
            if op == "admin_reset_password":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.password_reset")
                target = self._admin_target(actor, data.get("userid"))
                if int(target["id"]) == int(actor_row["id"]):
                    raise RPCError("use Account Settings to change your own password")
                if bool(target["deletion_pending"]):
                    raise RPCError("account deletion is pending")
                temporary = password(data.get("new_password"))
                self._reauth(actor_row, data.get("password"))
                with self.db:
                    self.db.execute(
                        "UPDATE accounts SET password_hash=?,password_change_required=1 "
                        "WHERE id=?", (hash_password(temporary), target["id"]))
                # A disabled account or forgotten-password attempt may have an
                # active backoff. The audited, reauthenticated reset begins a
                # fresh temporary-password authentication window.
                self.failures.pop(str(target["userid"]), None)
                self._audit(actor, "account.password_reset", str(target["userid"]),
                            detail="forced_change=true")
                return {"account_id": target["id"], "userid": target["userid"],
                        "password_change_required": True}
            if op == "admin_prepare_delete":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.delete")
                target = self._admin_target(actor, data.get("userid"))
                if int(target["id"]) == int(actor_row["id"]):
                    raise RPCError("cannot delete your current account")
                self._protect_final_sysop(target)
                self._reauth(actor_row, data.get("password"))
                with self.db:
                    self.db.execute(
                        "UPDATE accounts SET login_enabled=0,deletion_pending=1 WHERE id=?",
                        (target["id"],))
                self._audit(actor, "account.delete.prepare", str(target["userid"]),
                            detail="account disabled; subsystem cleanup required")
                return {"account_id": target["id"], "userid": target["userid"]}
            if op == "admin_finalize_delete":
                actor_row, actor = self._actor(peer[1])
                self._require(actor, "account.delete")
                target = self._admin_target(actor, data.get("userid"))
                if int(target["id"]) == int(actor_row["id"]):
                    raise RPCError("cannot delete your current account")
                if not bool(target["deletion_pending"]):
                    raise RPCError("account deletion was not prepared")
                self._protect_final_sysop(target)
                self._reauth(actor_row, data.get("password"))
                target_name = str(target["userid"])
                target_id = int(target["id"])
                with self.db:
                    self.db.execute("DELETE FROM accounts WHERE id=?", (target_id,))
                self._audit(actor, "account.delete.finalize", target_name,
                            detail=f"account_id={target_id}; credentials and profile removed")
                return {"deleted": True, "userid": target_name}
            if op == "admin_audit":
                _row, actor = self._actor(peer[1])
                self._require(actor, "audit.read")
                rows = self.db.execute(
                    "SELECT created_at,actor_user,action,target,result,detail "
                    "FROM admin_audit ORDER BY id DESC LIMIT 200").fetchall()
                return {"events": [dict(row) for row in rows]}
        except ValueError as exc:
            raise RPCError(str(exc)) from exc
        raise RPCError("unknown operation")


def _read_secret(path: str) -> str:
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise RuntimeError("secret must be a mode-0600 regular file")
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read().rstrip("\r\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--admin-password-file")
    args = parser.parse_args()
    authority = Authority(args.db)
    if authority.db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0:
        if not args.admin_password_file:
            raise SystemExit("fresh account store requires --admin-password-file")
        authority.add_account("admin", _read_secret(args.admin_password_file),
                              "System Administrator", True)
    serve(args.socket, authority.handle)


if __name__ == "__main__":
    main()
