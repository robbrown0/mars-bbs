#!/usr/bin/env python3
"""Deterministic game engines, persistence, and cross-account isolation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import marsv2.gamed as gamed_module
from marsv2.gamed import Games
from marsv2.games import GameError, apply, new_game, render, view
from marsv2.rpc import RPCError


def packet(op: str, data: dict[str, object] | None = None,
           token: str = "alice") -> dict[str, object]:
    return {"op": op, "data": data or {}, "token": token}


def denied(action, expected: str) -> None:
    try:
        action()
    except (RPCError, GameError) as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError("operation unexpectedly succeeded")


def main() -> None:
    # Complete Oregon Trail from outfitting through arrival using only normal
    # player commands and adaptive survival choices.
    oregon = new_game("oregon", 2, "alice")
    for command in ("BUY OXEN 150", "BUY FOOD 250", "BUY AMMO 50",
                    "BUY CLOTHING 50", "BUY SUPPLIES 100", "DEPART"):
        apply("oregon", oregon, command)
    completion = None
    for _ in range(60):
        if oregon["phase"] != "trail":
            break
        if int(oregon["health"]) < 35:
            result = apply("oregon", oregon, "REST")
        elif int(oregon["food"]) < 90 and int(oregon["ammo"]) >= 10:
            result = apply("oregon", oregon, "HUNT")
        else:
            result = apply("oregon", oregon, "TRAVEL")
        completion = result.get("score") or completion
    assert oregon["phase"] == "complete", render("oregon", oregon)
    assert completion and completion["result"] == "arrived"

    maze = new_game("maze", 2, "alice")
    maze_score = None
    for command in ("E", "N", "TAKE KEY", "S", "E", "E", "N",
                    "ANSWER 1983", "N"):
        result = apply("maze", maze, command)
        maze_score = result.get("score") or maze_score
    assert maze["phase"] == "complete" and maze_score

    gtw = new_game("gtw", 2, "alice")
    apply("gtw", gtw, "SCENARIO ORBITAL")
    apply("gtw", gtw, "DIFFICULTY 1")
    apply("gtw", gtw, "BEGIN")
    gtw_score = None
    for _ in range(6):
        result = apply("gtw", gtw, "DEESCALATE")
        gtw_score = result.get("score") or gtw_score
    assert gtw["phase"] == "complete" and gtw_score
    assert "ONLY WINNING MOVE" in render("gtw", gtw)

    wopr = new_game("wopr", 2, "alice")
    apply("wopr", wopr, "LIST GAMES")
    assert "TIC-TAC-TOE" in render("wopr", wopr)
    apply("wopr", wopr, "PLAY TIC-TAC-TOE")
    assert wopr["program"] == "ttt"
    for move in ("1", "2", "7", "4", "9"):
        apply("wopr", wopr, move)
        if wopr["substate"] and wopr["substate"].get("complete"):
            break
    apply("wopr", wopr, "QUIT")
    assert wopr["program"] == "shell"
    denied(lambda: apply("wopr", wopr, "BAD\x1bCOMMAND"), "control")

    chess = new_game("chess", 2, "alice")
    denied(lambda: apply("chess", chess, "e9"), "not a legal move")
    apply("chess", chess, "e4")
    chess_view = view("chess", chess)
    assert chess_view and chess_view["userid"] == "alice"
    assert chess_view["turn"] == "white" and len(chess["moves"]) == 2
    assert chess_view["engine"] == "Stockfish 19"
    assert chess_view["history"][0] == "e4"
    apply("chess", chess, "LEVEL COMMANDER")
    assert chess["level"] == "COMMANDER"
    apply("chess", chess, "NEW GAME")
    assert chess["moves"] == [] and chess["phase"] == "playing"

    identities = {
        "alice": {"account_id": 2, "userid": "alice", "display_name": "Alice",
                  "runtime_uid": 30002, "roles": ["Basic-1"], "capabilities": []},
        "bob": {"account_id": 3, "userid": "bob", "display_name": "Bob",
                "runtime_uid": 30003, "roles": ["Basic-1"], "capabilities": []},
        "sysop": {"account_id": 1, "userid": "sysop", "display_name": "Sysop",
                  "runtime_uid": 30001, "roles": ["Sysop"],
                  "capabilities": ["account.delete"]},
    }
    original_validate = gamed_module.validate
    gamed_module.validate = lambda _socket, token, _uid: identities[str(token)]
    try:
        with tempfile.TemporaryDirectory(prefix="mars-games-") as root:
            db_path = str(Path(root) / "game.db")
            games = Games(db_path, "unused")
            catalog = games.handle(packet("catalog"), (0, 30002, 0))["games"]
            assert {row["id"] for row in catalog} == {
                "oregon", "wopr", "maze", "gtw", "chess"}
            denied(lambda: games.handle(packet(
                "start", {"game": "maze", "account_id": 3}), (0, 30002, 0)),
                "unknown request field")
            started = games.handle(packet("start", {"game": "maze"}), (0, 30002, 0))
            assert games.db.execute(
                "SELECT count(*) FROM saves WHERE account_id=3").fetchone()[0] == 0
            lease = str(started["lease"])
            denied(lambda: games.handle(packet("input", {
                "lease": lease, "command": "E"}, "bob"), (0, 30003, 0)),
                   "unavailable")
            games.handle(packet("input", {"lease": lease, "command": "E"}),
                         (0, 30002, 0))
            games.handle(packet("quit", {"lease": lease}), (0, 30002, 0))
            resumed = games.handle(packet("start", {"game": "maze"}), (0, 30002, 0))
            assert resumed["resumed"] is True
            bob = games.handle(packet("start", {"game": "maze"}, "bob"), (0, 30003, 0))
            assert bob["lease"] != resumed["lease"] and bob["resumed"] is False
            # Reopening the authority preserves saves while expiring no valid
            # identity boundary.
            games.db.close()
            games = Games(db_path, "unused")
            reopened = games.handle(packet("start", {"game": "maze"}), (0, 30002, 0))
            assert reopened["resumed"] is True
            with games.db:
                games.db.execute(
                    "UPDATE saves SET state_json='[]' WHERE account_id=2 AND game='maze'")
            denied(lambda: games.handle(packet("start", {"game": "maze"}),
                                        (0, 30002, 0)), "saved game data is invalid")
            # Input is data, never a path or shell command.
            wopr_start = games.handle(packet("start", {"game": "wopr"}),
                                      (0, 30002, 0))
            response = games.handle(packet("input", {
                "lease": wopr_start["lease"],
                "command": "PLAY ../../bin/sh; id",
            }), (0, 30002, 0))
            assert "NOT AVAILABLE" in response["screen"]
            chess_start = games.handle(packet("start", {"game": "chess"}),
                                       (0, 30002, 0))
            assert chess_start["view"]["userid"] == "alice"
            chess_played = games.handle(packet("input", {
                "lease": chess_start["lease"], "command": "d4",
            }), (0, 30002, 0))
            assert len(chess_played["view"]["history"]) == 2
            games.handle(packet("quit", {"lease": chess_start["lease"]}),
                         (0, 30002, 0))
            chess_resumed = games.handle(packet("start", {"game": "chess"}),
                                         (0, 30002, 0))
            assert chess_resumed["resumed"] is True
            assert chess_resumed["view"]["history"] == chess_played["view"]["history"]
            limited = Games(str(Path(root) / "limited.db"), "unused", {"maze"})
            assert [row["id"] for row in limited.handle(
                packet("catalog"), (0, 30002, 0))["games"]] == ["maze"]
            denied(lambda: limited.handle(packet("start", {"game": "oregon"}),
                                          (0, 30002, 0)), "disabled")
            # Account cleanup is privileged and removes only the selected
            # account's game records.
            denied(lambda: games.handle(packet(
                "admin_delete_account", {"account_id": 3}), (0, 30002, 0)),
                "permission")
            games.handle(packet("admin_delete_account", {"account_id": 3}, "sysop"),
                         (0, 30001, 0))
            assert games.db.execute(
                "SELECT count(*) FROM saves WHERE account_id=3").fetchone()[0] == 0
            limited.db.close()
            games.db.close()
    finally:
        gamed_module.validate = original_validate

    print("PASS: complete games, persistence, validation, and account isolation")


if __name__ == "__main__":
    main()
