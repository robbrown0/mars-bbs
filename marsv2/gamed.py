"""Authenticated persistence and execution authority for Galactic Arcade games."""

from __future__ import annotations

import argparse
import json
import secrets
import time

from .db import connect
from .games import GAMES, GameError, apply, new_game, prompt, render, resumable, view
from .identity import validate
from .rpc import RPCError, serve
from .validate import integer, text


SCHEMA = """
CREATE TABLE IF NOT EXISTS saves (
  account_id INTEGER NOT NULL,
  game TEXT NOT NULL,
  state_json TEXT NOT NULL CHECK(length(state_json)<=24000),
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(account_id,game)
);
CREATE TABLE IF NOT EXISTS active_games (
  lease TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL,
  userid TEXT NOT NULL,
  game TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  last_activity INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_account_game
  ON active_games(account_id,game);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL,
  userid TEXT NOT NULL,
  game TEXT NOT NULL,
  score INTEGER NOT NULL CHECK(score>=0 AND score<=10000000),
  result TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scores_game_rank
  ON scores(game,score DESC,created_at,id);
CREATE TABLE IF NOT EXISTS launch_log (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL,
  userid TEXT NOT NULL,
  game TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS launch_log_created ON launch_log(created_at,id);
"""

LEASE_SECONDS = 6 * 60 * 60
AUDIT_RETENTION_SECONDS = 90 * 24 * 60 * 60
MAX_AUDIT_ROWS = 10_000
MAX_SCORES_PER_ACCOUNT = 500


class Games:
    def __init__(self, db_path: str, session_socket: str,
                 enabled: set[str] | None = None):
        self.db = connect(db_path)
        self.db.executescript(SCHEMA)
        self.session_socket = session_socket
        self.enabled = set(GAMES) if enabled is None else set(enabled)
        unknown = self.enabled.difference(GAMES)
        if unknown:
            raise ValueError("unknown enabled game(s): " + ", ".join(sorted(unknown)))
        self._prune()

    def _prune(self) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM active_games WHERE last_activity<?",
                            (now - LEASE_SECONDS,))
            self.db.execute("DELETE FROM launch_log WHERE created_at<?",
                            (now - AUDIT_RETENTION_SECONDS,))
            self.db.execute(
                "DELETE FROM launch_log WHERE id NOT IN "
                "(SELECT id FROM launch_log ORDER BY id DESC LIMIT ?)",
                (MAX_AUDIT_ROWS,))

    @staticmethod
    def _fields(data: dict[str, object], allowed: set[str]) -> None:
        unknown = set(data).difference(allowed)
        if unknown:
            raise RPCError("unknown request field")

    def _game(self, value: object) -> str:
        game = text(value, "game", 24, 1).lower()
        if game not in GAMES or game not in self.enabled:
            raise RPCError("unknown or disabled game")
        return game

    @staticmethod
    def _decode(raw: str, game: str) -> dict[str, object]:
        if len(raw) > 24_000:
            raise RPCError("saved game data is too large")
        try:
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError
            render(game, state)
            return state
        except (json.JSONDecodeError, TypeError, ValueError, GameError) as exc:
            raise RPCError("saved game data is invalid; ask a Sysop to restore it") from exc

    @staticmethod
    def _encode(state: dict[str, object]) -> str:
        raw = json.dumps(state, separators=(",", ":"), ensure_ascii=True)
        if len(raw) > 24_000:
            raise RPCError("saved game data is too large")
        return raw

    def _active(self, ident: dict[str, object], lease: object):
        value = text(lease, "game lease", 96, 32)
        row = self.db.execute("SELECT * FROM active_games WHERE lease=?", (value,)).fetchone()
        if row is None or int(row["account_id"]) != int(ident["account_id"]):
            raise RPCError("game session is unavailable")
        if int(row["last_activity"]) < int(time.time()) - LEASE_SECONDS:
            with self.db:
                self.db.execute("DELETE FROM active_games WHERE lease=?", (value,))
            raise RPCError("game session expired")
        return value, row

    def handle(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = req["op"]
        if op == "health":
            self.db.execute("SELECT 1").fetchone()
            return {"status": "ok"}
        ident = validate(self.session_socket, req.get("token"), peer[1])
        data = req["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        try:
            if op == "catalog":
                self._fields(data, set())
                return {"games": [{"id": key, **value} for key, value in GAMES.items()
                                  if key in self.enabled]}
            if op == "start":
                self._fields(data, {"game"})
                self._prune()
                game = self._game(data.get("game"))
                account_id = int(ident["account_id"])
                row = self.db.execute(
                    "SELECT state_json FROM saves WHERE account_id=? AND game=?",
                    (account_id, game)).fetchone()
                resumed = False
                if row is not None:
                    candidate = self._decode(str(row["state_json"]), game)
                    if resumable(candidate):
                        state = candidate
                        resumed = True
                    else:
                        state = new_game(game, account_id, str(ident["userid"]))
                else:
                    state = new_game(game, account_id, str(ident["userid"]))
                lease = secrets.token_urlsafe(36)
                now = int(time.time())
                with self.db:
                    self.db.execute("DELETE FROM active_games WHERE account_id=? AND game=?",
                                    (account_id, game))
                    self.db.execute(
                        "INSERT INTO active_games(lease,account_id,userid,game,started_at,last_activity) "
                        "VALUES(?,?,?,?,?,?)",
                        (lease, account_id, ident["userid"], game, now, now))
                    self.db.execute(
                        "INSERT INTO saves(account_id,game,state_json,revision,updated_at) "
                        "VALUES(?,?,?,1,?) ON CONFLICT(account_id,game) DO UPDATE SET "
                        "state_json=excluded.state_json,revision=saves.revision+1,"
                        "updated_at=excluded.updated_at",
                        (account_id, game, self._encode(state), now))
                    self.db.execute(
                        "INSERT INTO launch_log(account_id,userid,game,action,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (account_id, ident["userid"], game,
                         "resume" if resumed else "start", now))
                return {"lease": lease, "game": game, "resumed": resumed,
                        "screen": render(game, state), "prompt": prompt(game, state),
                        "view": view(game, state)}
            if op == "input":
                self._fields(data, {"lease", "command"})
                lease, active = self._active(ident, data.get("lease"))
                command = text(data.get("command", ""), "game command", 160)
                game = str(active["game"])
                row = self.db.execute(
                    "SELECT state_json FROM saves WHERE account_id=? AND game=?",
                    (ident["account_id"], game)).fetchone()
                if row is None:
                    raise RPCError("saved game is unavailable")
                state = self._decode(str(row["state_json"]), game)
                result = apply(game, state, command)
                now = int(time.time())
                score = result.pop("score", None)
                with self.db:
                    self.db.execute(
                        "UPDATE saves SET state_json=?,revision=revision+1,updated_at=? "
                        "WHERE account_id=? AND game=?",
                        (self._encode(state), now, ident["account_id"], game))
                    self.db.execute("UPDATE active_games SET last_activity=? WHERE lease=?",
                                    (now, lease))
                    if score:
                        score_game = text(score.get("game"), "score game", 24, 1).lower()
                        score_value = integer(score.get("score"), "score", 0, 10_000_000)
                        outcome = text(score.get("result"), "game result", 80, 1)
                        self.db.execute(
                            "INSERT INTO scores(account_id,userid,game,score,result,created_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (ident["account_id"], ident["userid"], score_game,
                             score_value, outcome, now))
                        self.db.execute(
                            "DELETE FROM scores WHERE account_id=? AND id NOT IN "
                            "(SELECT id FROM scores WHERE account_id=? "
                            "ORDER BY id DESC LIMIT ?)",
                            (ident["account_id"], ident["account_id"],
                             MAX_SCORES_PER_ACCOUNT))
                    if result.get("exit"):
                        self.db.execute("DELETE FROM active_games WHERE lease=?", (lease,))
                        self.db.execute(
                            "INSERT INTO launch_log(account_id,userid,game,action,created_at) "
                            "VALUES(?,?,?,?,?)",
                            (ident["account_id"], ident["userid"], game, "exit", now))
                result.pop("state", None)
                result["prompt"] = prompt(game, state)
                result["view"] = view(game, state)
                return result
            if op == "quit":
                self._fields(data, {"lease"})
                lease, active = self._active(ident, data.get("lease"))
                now = int(time.time())
                with self.db:
                    self.db.execute("DELETE FROM active_games WHERE lease=?", (lease,))
                    self.db.execute(
                        "INSERT INTO launch_log(account_id,userid,game,action,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (ident["account_id"], ident["userid"], active["game"],
                         "disconnect", now))
                return {"closed": True}
            if op == "scores":
                self._fields(data, {"game"})
                game_value = text(data.get("game", ""), "game", 24).lower()
                if game_value and game_value not in {*GAMES, "tic-tac-toe"}:
                    raise RPCError("unknown game")
                params: tuple[object, ...] = ()
                where = ""
                if game_value:
                    where = "WHERE game=?"
                    params = (game_value,)
                rows = self.db.execute(
                    "SELECT userid,game,score,result,created_at FROM scores " + where
                    + " ORDER BY score DESC,created_at,id LIMIT 100", params).fetchall()
                personal = self.db.execute(
                    "SELECT game,count(*) plays,max(score) best FROM scores "
                    "WHERE account_id=? GROUP BY game ORDER BY game",
                    (ident["account_id"],)).fetchall()
                return {"scores": [dict(row) for row in rows],
                        "personal": [dict(row) for row in personal]}
            if op == "delete_save":
                self._fields(data, {"game", "confirm"})
                game = self._game(data.get("game"))
                confirm = text(data.get("confirm"), "confirmation", 32, 1)
                if confirm != "ABANDON " + game.upper():
                    raise RPCError("confirmation does not match")
                with self.db:
                    removed = self.db.execute(
                        "DELETE FROM saves WHERE account_id=? AND game=?",
                        (ident["account_id"], game)).rowcount
                    self.db.execute(
                        "DELETE FROM active_games WHERE account_id=? AND game=?",
                        (ident["account_id"], game))
                return {"deleted": bool(removed)}
            if op == "admin_delete_account":
                self._fields(data, {"account_id"})
                if "account.delete" not in ident.get("capabilities", []):
                    raise RPCError("permission denied")
                account_id = integer(data.get("account_id"), "account id", 1, 2_147_483_647)
                with self.db:
                    self.db.execute("DELETE FROM active_games WHERE account_id=?", (account_id,))
                    self.db.execute("DELETE FROM saves WHERE account_id=?", (account_id,))
                    self.db.execute("DELETE FROM scores WHERE account_id=?", (account_id,))
                    self.db.execute("DELETE FROM launch_log WHERE account_id=?", (account_id,))
                return {"deleted": True}
            raise RPCError("unknown operation")
        except (ValueError, GameError) as exc:
            raise RPCError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--enabled-games", default=",".join(GAMES),
                        help="comma-separated adapter identifiers (default: all)")
    args = parser.parse_args()
    enabled = {value.strip().lower() for value in args.enabled_games.split(",") if value.strip()}
    authority = Games(args.db, args.session_socket, enabled)
    serve(args.socket, authority.handle)


if __name__ == "__main__":
    main()
