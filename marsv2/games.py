"""Deterministic, terminal-safe game adapters for the Galactic Arcade.

The adapters are pure state machines.  They never open files, spawn programs,
interpret terminal controls, or access the network.  ``gamed`` owns persistence
and supplies only validated commands and stable account identities.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import chess
import chess.engine


STATE_VERSION = 1
MAX_STATE_BYTES = 24_000
GAMES = {
    "oregon": {
        "title": "THE OREGON TRAIL",
        "description": "A 1970s-style overland journey simulation",
    },
    "wopr": {
        "title": "WOPR COMPUTER SYSTEM",
        "description": "Joshua terminal with three complete game routines",
    },
    "maze": {
        "title": "FALKEN'S MAZE",
        "description": "Explore, solve the access puzzle, and escape",
    },
    "gtw": {
        "title": "GLOBAL THERMONUCLEAR WAR",
        "description": "A fictional de-escalation strategy simulation",
    },
    "chess": {
        "title": "MARS HOTEL CHESS",
        "description": "Full tournament rules against Stockfish 19",
    },
}

STOCKFISH_PATH = os.environ.get("MARS_STOCKFISH", "/usr/local/bin/stockfish")
CHESS_LEVELS = {
    "CADET": {"skill": 2, "depth": 5},
    "VOYAGER": {"skill": 9, "depth": 9},
    "COMMANDER": {"skill": 16, "depth": 13},
}


class GameError(ValueError):
    """A safe, user-facing game validation error."""


def _base(game: str) -> dict[str, Any]:
    return {"schema": STATE_VERSION, "game": game, "updated_at": int(time.time())}


def _command(value: str) -> str:
    return " ".join(value.strip().upper().split())


def _rand(state: dict[str, Any], maximum: int) -> int:
    seed = int(state.get("seed", 1)) & 0x7fffffff
    seed = (1103515245 * seed + 12345) & 0x7fffffff
    state["seed"] = seed
    return seed % maximum


def _bounded_state(state: dict[str, Any], expected: str) -> dict[str, Any]:
    if (not isinstance(state, dict) or state.get("schema") != STATE_VERSION
            or state.get("game") != expected):
        raise GameError("saved game data is incompatible")
    try:
        encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise GameError("saved game data is invalid") from exc
    if len(encoded) > MAX_STATE_BYTES:
        raise GameError("saved game data is too large")
    return state


def new_game(game: str, account_id: int, userid: str) -> dict[str, Any]:
    seed = ((account_id * 2654435761) ^ int(time.time())) & 0x7fffffff
    if game == "oregon":
        state = _base(game)
        state.update({
            "phase": "outfitting", "cash": 700, "oxen": 0, "food": 0,
            "ammo": 0, "clothing": 0, "supplies": 0, "miles": 0,
            "health": 100, "turn": 0, "pace": "STEADY", "rations": "NORMAL",
            "seed": seed, "userid": userid,
            "message": "You have $700 to outfit your wagon. Type HELP for commands.",
        })
        return state
    if game == "maze":
        return _new_maze(account_id, userid, seed)
    if game == "gtw":
        return _new_gtw(userid, seed)
    if game == "wopr":
        state = _base(game)
        state.update({
            "program": "shell", "substate": None, "userid": userid,
            "seed": seed, "message": "GREETINGS. SHALL WE PLAY A GAME?",
        })
        return state
    if game == "chess":
        state = _base(game)
        state.update({
            "phase": "playing", "userid": userid, "moves": [],
            "player_color": "white", "level": "VOYAGER",
            "message": "WHITE TO MOVE - Select a piece or type a move.",
            "result": "", "engine": "Stockfish 19",
        })
        return state
    raise GameError("unknown game")


def apply(game: str, state: dict[str, Any], value: str) -> dict[str, Any]:
    _bounded_state(state, game)
    if len(value) > 160:
        raise GameError("command is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 or ord(ch) == 27 for ch in value):
        raise GameError("command contains unsupported control characters")
    command = _command(value)
    if game == "oregon":
        result = _oregon(state, command)
    elif game == "maze":
        result = _maze(state, command)
    elif game == "gtw":
        result = _gtw(state, command)
    elif game == "wopr":
        result = _wopr(state, command)
    elif game == "chess":
        result = _chess(state, value.strip())
    else:
        raise GameError("unknown game")
    state["updated_at"] = int(time.time())
    _bounded_state(state, game)
    result["state"] = state
    result["screen"] = render(game, state)
    result.setdefault("exit", False)
    return result


def render(game: str, state: dict[str, Any]) -> str:
    _bounded_state(state, game)
    if game == "oregon":
        return _render_oregon(state)
    if game == "maze":
        return _render_maze(state)
    if game == "gtw":
        return _render_gtw(state)
    if game == "wopr":
        return _render_wopr(state)
    if game == "chess":
        return _render_chess(state)
    raise GameError("unknown game")


def prompt(game: str, state: dict[str, Any]) -> str:
    if game == "oregon":
        return "OUTFIT> " if state["phase"] == "outfitting" else "TRAIL> "
    if game == "maze":
        return "MAZE> "
    if game == "gtw":
        return "SCENARIO> " if state["phase"] == "setup" else "COMMAND> "
    if game == "wopr":
        program = state.get("program")
        if program == "maze":
            return "MAZE> "
        if program == "gtw":
            return "COMMAND> "
        if program == "ttt":
            return "MOVE> "
        return "JOSHUA> "
    if game == "chess":
        return "CHESS> "
    return "> "


def view(game: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Return bounded structured display data for specialized terminal UIs."""
    _bounded_state(state, game)
    if game == "chess":
        return _chess_view(state)
    return None


def resumable(state: dict[str, Any]) -> bool:
    return state.get("phase") not in ("complete", "dead", "abandoned")


# ---------------------------------------------------------------------------
# Oregon Trail -- an original, compact state-machine interpretation of the
# 1970s text game. It does not embed or execute the historical BASIC program.


def _render_oregon(state: dict[str, Any]) -> str:
    line = "=" * 62
    if state["phase"] == "outfitting":
        return (
            "THE OREGON TRAIL - 1975-STYLE EXPEDITION\n" + line + "\n"
            f"TRAVELER: {state['userid']}                         CASH: ${state['cash']}\n"
            f"OXEN ${state['oxen']:3}   FOOD {state['food']:4} lb   AMMO {state['ammo']:4}\n"
            f"CLOTHING ${state['clothing']:3}   SPARE PARTS ${state['supplies']:3}\n\n"
            + str(state["message"]) + "\n\n"
            "BUY OXEN n / FOOD n / AMMO n / CLOTHING n / SUPPLIES n\n"
            "DEPART begins the journey. HELP explains the rules. QUIT saves."
        )
    status = (
        "THE OREGON TRAIL\n" + line + "\n"
        f"MILES: {state['miles']:4}/2040   TURN: {state['turn']:2}   "
        f"HEALTH: {state['health']:3}%\n"
        f"FOOD: {state['food']:4} lb   AMMO: {state['ammo']:4}   "
        f"CLOTHING: ${state['clothing']:3}   PARTS: ${state['supplies']:3}\n"
        f"PACE: {state['pace']:<8}   RATIONS: {state['rations']}\n\n"
        + str(state["message"])
    )
    if state["phase"] == "complete":
        return status + "\n\nYou reached Oregon. Your expedition is complete."
    if state["phase"] in ("dead", "abandoned"):
        return status + "\n\nThis expedition has ended."
    return status + ("\n\nTRAVEL / HUNT / REST / PACE / RATIONS / STATUS / HELP / QUIT")


def _oregon_help(outfitting: bool) -> str:
    if outfitting:
        return ("Spend dollars with BUY item amount. Required before DEPART: "
                "OXEN 100, FOOD 100, CLOTHING 20, SUPPLIES 20. Food purchases "
                "buy two pounds per dollar; ammunition buys ten shots per dollar.")
    return ("TRAVEL advances west. HUNT spends ammunition for food. REST restores health. "
            "PACE STEADY or GRUELING and RATIONS FILLING, NORMAL, or MEAGER adjust travel. "
            "QUIT saves and returns to the Arcade; ABANDON YES ends the expedition.")


def _oregon(state: dict[str, Any], command: str) -> dict[str, Any]:
    phase = str(state["phase"])
    if command in ("QUIT", "EXIT", "LOGOUT"):
        state["message"] = "Expedition saved at the last completed turn."
        return {"exit": True}
    if command in ("HELP", "?"):
        state["message"] = _oregon_help(phase == "outfitting")
        return {}
    if command in ("STATUS", "SAVE"):
        state["message"] = "Progress is saved automatically after every command."
        return {}
    if phase in ("complete", "dead", "abandoned"):
        state["message"] = "This expedition is over. Exit and begin a new journey to play again."
        return {}
    if phase == "outfitting":
        if command == "DEPART":
            missing = [name for name, minimum in
                       (("oxen", 100), ("food", 100), ("clothing", 20), ("supplies", 20))
                       if int(state[name]) < minimum]
            if missing:
                state["message"] = "You still need: " + ", ".join(missing).upper() + "."
            else:
                state["phase"] = "trail"
                state["message"] = "Your wagon leaves Independence. Oregon is 2,040 miles west."
            return {}
        match = re.fullmatch(r"BUY (OXEN|FOOD|AMMO|CLOTHING|SUPPLIES) ([0-9]{1,3})", command)
        if not match:
            state["message"] = "Use BUY item amount, DEPART, HELP, or QUIT."
            return {}
        item, raw = match.groups()
        amount = int(raw)
        if amount < 1 or amount > int(state["cash"]):
            state["message"] = "That purchase is outside your remaining budget."
            return {}
        key = item.lower()
        state["cash"] -= amount
        multiplier = 2 if item == "FOOD" else 10 if item == "AMMO" else 1
        state[key] += amount * multiplier
        state["message"] = f"Purchased {item}: ${amount}. Remaining cash: ${state['cash']}."
        return {}
    if command.startswith("PACE "):
        value = command[5:]
        if value not in ("STEADY", "GRUELING"):
            state["message"] = "PACE must be STEADY or GRUELING."
        else:
            state["pace"] = value
            state["message"] = f"Travel pace set to {value}."
        return {}
    if command.startswith("RATIONS "):
        value = command[8:]
        if value not in ("FILLING", "NORMAL", "MEAGER"):
            state["message"] = "RATIONS must be FILLING, NORMAL, or MEAGER."
        else:
            state["rations"] = value
            state["message"] = f"Rations set to {value}."
        return {}
    if command == "ABANDON YES":
        state["phase"] = "abandoned"
        state["message"] = "You turn the wagon back toward Independence."
        return {"score": {"game": "oregon", "score": 0, "result": "abandoned"}}
    if command == "HUNT":
        if int(state["ammo"]) < 10:
            state["message"] = "You do not have enough ammunition to hunt."
            return {}
        spent = 8 + _rand(state, 13)
        gained = 30 + _rand(state, 51)
        state["ammo"] = max(0, int(state["ammo"]) - spent)
        state["food"] += gained
        state["turn"] += 1
        state["message"] = f"The hunt used {spent} shots and brought back {gained} pounds of food."
        return _oregon_survival(state, 6)
    if command == "REST":
        state["turn"] += 1
        state["health"] = min(100, int(state["health"]) + 14)
        state["message"] = "The party rests and regains strength."
        return _oregon_survival(state, 8)
    if command != "TRAVEL":
        state["message"] = "Command not recognized. Type HELP for trail commands."
        return {}
    state["turn"] += 1
    distance = 70 + _rand(state, 56)
    if state["pace"] == "GRUELING":
        distance += 35
        state["health"] -= 3
    event = _rand(state, 12)
    event_text = "The trail is clear."
    if event == 0:
        distance = max(20, distance - 35)
        state["clothing"] = max(0, int(state["clothing"]) - 5)
        event_text = "A cold storm slows the wagon and wears out clothing."
    elif event == 1:
        loss = 7 if int(state["supplies"]) >= 8 else 14
        state["supplies"] = max(0, int(state["supplies"]) - 8)
        state["health"] -= loss
        event_text = "Illness strikes; medicine and rest limit the harm."
    elif event == 2:
        if int(state["supplies"]) >= 10:
            state["supplies"] -= 10
            event_text = "A wagon part breaks, but your spares repair it."
        else:
            distance = max(10, distance - 50)
            state["health"] -= 5
            event_text = "A broken wagon part causes a long delay."
    elif event == 3:
        distance += 25
        event_text = "Fine weather and a firm trail improve your progress."
    state["miles"] = min(2040, int(state["miles"]) + distance)
    state["message"] = f"You travel {distance} miles. {event_text}"
    result = _oregon_survival(state, 12)
    if state["phase"] == "trail" and int(state["miles"]) >= 2040:
        state["phase"] = "complete"
        score = max(1, 2500 + int(state["health"]) * 10 + int(state["food"])
                    + int(state["cash"]) * 2 - int(state["turn"]) * 15)
        state["message"] += " The Willamette Valley lies ahead. You made it to Oregon!"
        return {"score": {"game": "oregon", "score": score, "result": "arrived"}}
    return result


def _oregon_survival(state: dict[str, Any], base_food: int) -> dict[str, Any]:
    ration = {"FILLING": 1.35, "NORMAL": 1.0, "MEAGER": 0.65}[str(state["rations"])]
    needed = max(1, int(base_food * ration))
    eaten = min(needed, int(state["food"]))
    state["food"] -= eaten
    if eaten < needed:
        state["health"] -= 12
        state["message"] += " Food is dangerously low."
    elif state["rations"] == "FILLING":
        state["health"] = min(100, int(state["health"]) + 2)
    elif state["rations"] == "MEAGER":
        state["health"] -= 2
    if int(state["clothing"]) <= 0:
        state["health"] -= 2
    if int(state["health"]) <= 0:
        state["health"] = 0
        state["phase"] = "dead"
        state["message"] += " The expedition can go no farther."
        return {"score": {"game": "oregon", "score": int(state["miles"]),
                          "result": "expedition failed"}}
    return {}


# ---------------------------------------------------------------------------
# Falken's Maze


MAZES = (
    {
        0: ("ENTRY NODE", {"E": 1}),
        1: ("CENTRAL CORRIDOR", {"W": 0, "N": 2, "E": 3}),
        2: ("OBSERVATION ROOM", {"S": 1}),
        3: ("SECURITY DOOR", {"W": 1, "E": 4}),
        4: ("CONTROL CHAMBER", {"W": 3, "N": 5}),
        5: ("LOGIC TERMINAL", {"S": 4, "N": 6}),
        6: ("FREEDOM GATE", {"S": 5}),
    },
    {
        0: ("ENTRY NODE", {"N": 1}),
        1: ("CENTRAL CORRIDOR", {"S": 0, "W": 2, "E": 3}),
        2: ("OBSERVATION ROOM", {"E": 1}),
        3: ("SECURITY DOOR", {"W": 1, "N": 4}),
        4: ("CONTROL CHAMBER", {"S": 3, "E": 5}),
        5: ("LOGIC TERMINAL", {"W": 4, "E": 6}),
        6: ("FREEDOM GATE", {"W": 5}),
    },
)


def _new_maze(account_id: int, userid: str, seed: int) -> dict[str, Any]:
    state = _base("maze")
    state.update({
        "phase": "playing", "layout": account_id % len(MAZES), "room": 0,
        "inventory": [], "solved": False, "moves": 0, "seed": seed,
        "userid": userid, "message": "A green cursor blinks beside an unmarked passage.",
    })
    return state


def _render_maze(state: dict[str, Any]) -> str:
    graph = MAZES[int(state["layout"])]
    name, exits = graph[int(state["room"])]
    inventory = ", ".join(state["inventory"]) or "EMPTY"
    text = (
        "FALKEN'S MAZE\n" + "=" * 62 + "\n"
        f"LOCATION: {name}                    MOVES: {state['moves']}\n"
        f"EXITS: {', '.join(exits)}                    INVENTORY: {inventory}\n\n"
        + str(state["message"])
    )
    if int(state["room"]) == 2 and "BRASS KEY" not in state["inventory"]:
        text += "\nA small brass key rests beneath the terminal desk."
    if int(state["room"]) == 5 and not state["solved"]:
        text += "\nThe terminal asks: IN WHAT YEAR DID JOSHUA FIRST PLAY?"
    if state["phase"] == "complete":
        text += "\n\nThe gate opens. You have escaped Falken's Maze."
    else:
        text += "\n\nN/S/E/W, LOOK, TAKE KEY, ANSWER n, INVENTORY, HELP, QUIT"
    return text


def _maze(state: dict[str, Any], command: str) -> dict[str, Any]:
    if command in ("QUIT", "EXIT", "LOGOUT"):
        state["message"] = "Maze position saved."
        return {"exit": True}
    if state["phase"] == "complete":
        state["message"] = "The completed maze is archived in your record."
        return {}
    if command in ("HELP", "?"):
        state["message"] = ("Move with N, S, E, or W. Examine rooms with LOOK. "
                            "TAKE useful objects and ANSWER terminal questions.")
        return {}
    if command in ("LOOK", "INVENTORY", "I"):
        state["message"] = "The room scan is complete."
        return {}
    if command in ("TAKE KEY", "TAKE BRASS KEY"):
        if int(state["room"]) == 2 and "BRASS KEY" not in state["inventory"]:
            state["inventory"].append("BRASS KEY")
            state["message"] = "You take the brass key."
        else:
            state["message"] = "There is no key here to take."
        return {}
    if command.startswith("ANSWER "):
        if int(state["room"]) != 5:
            state["message"] = "No terminal here is awaiting an answer."
        elif command == "ANSWER 1983":
            state["solved"] = True
            state["message"] = "IDENTIFICATION ACCEPTED. THE FREEDOM GATE IS RELEASED."
        else:
            state["message"] = "INCORRECT. The terminal resets its question."
        return {}
    direction = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}.get(
        command, command)
    if direction.startswith("GO "):
        direction = direction[3:4]
    if direction not in ("N", "S", "E", "W"):
        state["message"] = "That instruction is not recognized. Type HELP."
        return {}
    graph = MAZES[int(state["layout"])]
    exits = graph[int(state["room"])][1]
    if direction not in exits:
        state["message"] = "A featureless wall blocks that direction."
        return {}
    target = exits[direction]
    if int(state["room"]) == 3 and target == 4 and "BRASS KEY" not in state["inventory"]:
        state["message"] = "The security door is locked. A brass keyway glints beneath it."
        return {}
    if target == 6 and not state["solved"]:
        state["message"] = "The Freedom Gate remains locked by the logic terminal."
        return {}
    state["room"] = target
    state["moves"] += 1
    state["message"] = f"You enter {graph[target][0]}."
    if target == 6:
        state["phase"] = "complete"
        score = max(100, 1200 - int(state["moves"]) * 25)
        return {"score": {"game": "maze", "score": score, "result": "escaped"}}
    return {}


# ---------------------------------------------------------------------------
# Fictional de-escalation simulation.  Names and mechanics deliberately avoid
# real targets, weapons, force structures, or operational planning data.


def _new_gtw(userid: str, seed: int) -> dict[str, Any]:
    state = _base("gtw")
    state.update({
        "phase": "setup", "scenario": "POLAR MISUNDERSTANDING", "difficulty": 1,
        "turn": 0, "tension": 55, "stability": 45, "defense": 20,
        "seed": seed, "userid": userid,
        "message": "SELECT SCENARIO, SET DIFFICULTY 1-3, THEN BEGIN.",
    })
    return state


def _render_gtw(state: dict[str, Any]) -> str:
    map_text = (
        "          .-----------------------.\n"
        "     ALPHA|      NEUTRAL ZONE     |BRAVO\n"
        "          '----------+------------'\n"
        "                   CHARLIE"
    )
    text = (
        "GLOBAL THERMONUCLEAR WAR - FICTIONAL SIMULATION\n" + "=" * 62 + "\n"
        + map_text + "\n\n"
        f"SCENARIO: {state['scenario']}   DIFFICULTY: {state['difficulty']}\n"
        f"TURN: {state['turn']}/6   TENSION: {state['tension']}   "
        f"STABILITY: {state['stability']}   DEFENSE: {state['defense']}\n\n"
        + str(state["message"])
    )
    if state["phase"] == "setup":
        return text + ("\n\nSCENARIO POLAR / ORBITAL / PACIFIC\n"
                       "DIFFICULTY 1-3 / BEGIN / HELP / QUIT")
    if state["phase"] == "complete":
        return text + "\n\nA STRANGE GAME. THE ONLY WINNING MOVE IS NOT TO PLAY."
    return text + "\n\nDEESCALATE / NEGOTIATE / DEFEND / ESCALATE / STATUS / HELP / QUIT"


def _gtw(state: dict[str, Any], command: str) -> dict[str, Any]:
    if command in ("QUIT", "EXIT", "LOGOUT"):
        state["message"] = "SIMULATION SUSPENDED. State saved."
        return {"exit": True}
    if command in ("HELP", "?"):
        state["message"] = ("This is a fictional crisis-management game. Lower tension through "
                            "negotiation and restraint. It contains no real targets or planning data.")
        return {}
    if command == "STATUS":
        state["message"] = "STRATEGIC DISPLAY UPDATED."
        return {}
    if state["phase"] == "setup":
        if command.startswith("SCENARIO "):
            name = command[9:]
            choices = {"POLAR": "POLAR MISUNDERSTANDING",
                       "ORBITAL": "ORBITAL SIGNAL ERROR",
                       "PACIFIC": "PACIFIC FALSE ALARM"}
            if name in choices:
                state["scenario"] = choices[name]
                state["message"] = f"SCENARIO LOADED: {choices[name]}."
            else:
                state["message"] = "AVAILABLE SCENARIOS: POLAR, ORBITAL, PACIFIC."
            return {}
        match = re.fullmatch(r"DIFFICULTY ([1-3])", command)
        if match:
            state["difficulty"] = int(match.group(1))
            state["message"] = f"DIFFICULTY SET TO {match.group(1)}."
            return {}
        if command == "BEGIN":
            state["phase"] = "playing"
            state["message"] = "WARNING CONDITION. COMMAND AUTHORITY TRANSFERRED TO PLAYER."
            return {}
        state["message"] = "SELECT A SCENARIO AND DIFFICULTY, THEN TYPE BEGIN."
        return {}
    if state["phase"] == "complete":
        state["message"] = "SIMULATION COMPLETE. NO WINNER RECORDED."
        return {}
    actions = {
        "DEESCALATE": (-18, 10, -2, "Both sides stand down one alert level."),
        "NEGOTIATE": (-12, 8, 0, "A neutral channel carries a verified message."),
        "DEFEND": (7, -2, 14, "Defensive preparations reassure some and alarm others."),
        "ESCALATE": (22, -12, 8, "Escalation produces immediate countermeasures."),
    }
    if command not in actions:
        state["message"] = "IMPROPER REQUEST. Type HELP for available commands."
        return {}
    tension, stability, defense, message = actions[command]
    pressure = int(state["difficulty"]) * 3 + _rand(state, 7)
    state["tension"] = max(0, min(100, int(state["tension"]) + tension + pressure))
    state["stability"] = max(0, min(100, int(state["stability"]) + stability - pressure // 2))
    state["defense"] = max(0, min(100, int(state["defense"]) + defense))
    state["turn"] += 1
    state["message"] = message + f" Automated opposition pressure: +{pressure} tension."
    if int(state["tension"]) >= 100 or int(state["turn"]) >= 6:
        state["phase"] = "complete"
        peaceful = int(state["tension"]) < 45 and int(state["stability"]) >= 45
        if peaceful:
            result = "mutual stand-down"
            state["message"] += " VERIFIED STAND-DOWN ACHIEVED."
        else:
            result = "no winner"
            state["message"] += " ESCALATION LEAVES NO WINNER."
        score = max(0, 1500 - int(state["tension"]) * 10 + int(state["stability"]) * 5)
        return {"score": {"game": "gtw", "score": score, "result": result}}
    return {}


# ---------------------------------------------------------------------------
# Mars Hotel Chess -- complete rules via python-chess and a bounded Stockfish
# opponent.  The engine executable path and all UCI options are operator-owned;
# no user input is ever interpreted as a program, path, or engine option.


def _chess_board(state: dict[str, Any]) -> chess.Board:
    moves = state.get("moves")
    if not isinstance(moves, list) or len(moves) > 1024:
        raise GameError("saved chess move history is invalid")
    board = chess.Board()
    try:
        for raw in moves:
            if not isinstance(raw, str) or len(raw) > 5:
                raise ValueError
            board.push_uci(raw)
    except (ValueError, IndexError) as exc:
        raise GameError("saved chess position is invalid") from exc
    if not board.is_valid():
        raise GameError("saved chess position is invalid")
    return board


def _chess_level(state: dict[str, Any]) -> str:
    level = str(state.get("level", "VOYAGER")).upper()
    if level not in CHESS_LEVELS:
        raise GameError("saved chess difficulty is invalid")
    return level


def _stockfish_move(board: chess.Board, level: str) -> chess.Move:
    if not os.path.isabs(STOCKFISH_PATH) or not os.access(STOCKFISH_PATH, os.X_OK):
        raise GameError("Stockfish is unavailable; the move was not recorded")
    settings = CHESS_LEVELS[level]
    engine: chess.engine.SimpleEngine | None = None
    try:
        engine = chess.engine.SimpleEngine.popen_uci(
            [STOCKFISH_PATH], timeout=4.0)
        engine.configure({
            "Threads": 1,
            "Hash": 16,
            "Skill Level": int(settings["skill"]),
        })
        played = engine.play(board, chess.engine.Limit(depth=int(settings["depth"])))
        if played.move is None or played.move not in board.legal_moves:
            raise GameError("Stockfish returned no legal move")
        return played.move
    except (chess.engine.EngineError, chess.engine.EngineTerminatedError,
            TimeoutError, OSError) as exc:
        raise GameError("Stockfish could not complete the move; try again") from exc
    finally:
        if engine is not None:
            try:
                engine.quit()
            except (chess.engine.EngineError, chess.engine.EngineTerminatedError,
                    TimeoutError, OSError):
                try:
                    engine.close()
                except (AttributeError, OSError):
                    pass


def _chess_outcome(board: chess.Board) -> tuple[str, int, str] | None:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None
    reason = outcome.termination.name.replace("_", " ").title()
    if outcome.winner is None:
        return f"Draw - {reason}", 500, "draw"
    winner = "White" if outcome.winner == chess.WHITE else "Black"
    return f"{winner} wins - {reason}", 1200 if outcome.winner == chess.WHITE else 100, \
        "player won" if outcome.winner == chess.WHITE else "Joshua won"


def _finish_chess(state: dict[str, Any], board: chess.Board) -> dict[str, Any]:
    outcome = _chess_outcome(board)
    if outcome is None:
        return {}
    description, score, result = outcome
    state["phase"] = "complete"
    state["result"] = description
    state["message"] = description + ". Type NEW GAME to play again or X to exit."
    return {"score": {"game": "chess", "score": score, "result": result}}


def _parse_chess_move(board: chess.Board, value: str) -> chess.Move:
    raw = value.strip()
    if raw.upper().startswith("MOVE "):
        raw = raw[5:].strip()
    if not raw or len(raw) > 16:
        raise GameError("Enter a move such as e4, Nf3, or e2e4")
    try:
        return board.parse_san(raw)
    except ValueError:
        try:
            return board.parse_uci(raw.lower())
        except ValueError as exc:
            raise GameError(f"{raw!r} is not a legal move in this position") from exc


def _reset_chess(state: dict[str, Any]) -> None:
    userid = str(state.get("userid", "player"))
    level = _chess_level(state)
    state.clear()
    state.update(_base("chess"))
    state.update({
        "phase": "playing", "userid": userid, "moves": [],
        "player_color": "white", "level": level,
        "message": "WHITE TO MOVE - Select a piece or type a move.",
        "result": "", "engine": "Stockfish 19",
    })


def _chess(state: dict[str, Any], value: str) -> dict[str, Any]:
    command = _command(value)
    board = _chess_board(state)
    level = _chess_level(state)
    if command in ("QUIT", "EXIT", "LOGOUT"):
        state["message"] = "Game saved at the current position."
        return {"exit": True}
    if command in ("HELP", "?"):
        state["message"] = (
            "Use arrows and Enter, or type SAN/UCI moves such as Nf3 or g1f3. "
            "LEVEL CADET, LEVEL VOYAGER, or LEVEL COMMANDER changes Joshua. "
            "RESIGN YES ends the game; NEW GAME starts over; X saves and exits.")
        return {}
    if command == "NEW GAME":
        _reset_chess(state)
        return {}
    if command.startswith("LEVEL "):
        selected = command[6:].strip()
        if selected not in CHESS_LEVELS:
            state["message"] = "Choose CADET, VOYAGER, or COMMANDER."
        else:
            state["level"] = selected
            state["message"] = f"JOSHUA difficulty set to {selected.title()}."
        return {}
    if command == "RESIGN":
        state["message"] = "Type RESIGN YES to confirm, or continue playing."
        return {}
    if command == "RESIGN YES":
        if state.get("phase") == "complete":
            state["message"] = "This game is already complete. Type NEW GAME to play again."
            return {}
        state["phase"] = "complete"
        state["result"] = "Black wins - White resigned"
        state["message"] = "You resigned. Type NEW GAME to play again or X to exit."
        return {"score": {"game": "chess", "score": 0, "result": "resigned"}}
    if state.get("phase") == "complete":
        state["message"] = "This game is complete. Type NEW GAME or X to exit."
        return {}

    move = _parse_chess_move(board, value)
    player_san = board.san(move)
    board.push(move)
    additions = [move.uci()]
    outcome = _chess_outcome(board)
    if outcome is None:
        engine_move = _stockfish_move(board, level)
        engine_san = board.san(engine_move)
        board.push(engine_move)
        additions.append(engine_move.uci())
        state["message"] = f"You played {player_san}. JOSHUA replied {engine_san}."
    else:
        state["message"] = f"You played {player_san}."
    state["moves"].extend(additions)
    finished = _finish_chess(state, board)
    return finished


def _chess_captured(board: chess.Board) -> tuple[str, str]:
    initial = {"P": 8, "N": 2, "B": 2, "R": 2, "Q": 1}
    white_now = {piece: 0 for piece in initial}
    black_now = {piece: 0 for piece in initial}
    for item in board.piece_map().values():
        symbol = item.symbol().upper()
        if symbol not in initial:
            continue
        (white_now if item.color == chess.WHITE else black_now)[symbol] += 1
    captured_by_black = "".join(
        piece * max(0, initial[piece] - white_now[piece]) for piece in "QRBNP")
    captured_by_white = "".join(
        piece.lower() * max(0, initial[piece] - black_now[piece]) for piece in "QRBNP")
    return captured_by_white, captured_by_black


def _chess_history(state: dict[str, Any]) -> list[str]:
    board = chess.Board()
    result: list[str] = []
    for raw in state["moves"]:
        move = board.parse_uci(str(raw))
        result.append(board.san(move))
        board.push(move)
    return result


def _chess_view(state: dict[str, Any]) -> dict[str, Any]:
    board = _chess_board(state)
    pieces = {chess.square_name(square): piece.symbol()
              for square, piece in board.piece_map().items()}
    captured_white, captured_black = _chess_captured(board)
    legal = [move.uci() for move in board.legal_moves] if state.get("phase") == "playing" else []
    return {
        "userid": str(state.get("userid", "player")),
        "pieces": pieces,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "player_color": str(state.get("player_color", "white")),
        "level": _chess_level(state).title(),
        "phase": str(state.get("phase", "playing")),
        "message": str(state.get("message", "")),
        "result": str(state.get("result", "")),
        "history": _chess_history(state)[-40:],
        "legal_moves": legal,
        "last_move": str(state["moves"][-1]) if state["moves"] else "",
        "captured_by_white": captured_white,
        "captured_by_black": captured_black,
        "in_check": board.is_check(),
        "engine": "Stockfish 19",
    }


def _render_chess(state: dict[str, Any]) -> str:
    view_data = _chess_view(state)
    pieces = view_data["pieces"]
    userid = str(view_data.get("userid", "PLAYER"))[:12]
    lines = [f"MARS HOTEL CHESS / {userid} vs JOSHUA", "=" * 62]
    for rank in range(8, 0, -1):
        lines.append(f"{rank}  " + " ".join(
            str(pieces.get(f"{file_name}{rank}", "."))
            for file_name in "abcdefgh"))
    lines.extend(("   a b c d e f g h", "", str(view_data["message"])))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# WOPR / Joshua shell and deterministic tic-tac-toe


def _new_ttt() -> dict[str, Any]:
    return {"board": [" "] * 9, "message": "YOU ARE X. SELECT A SQUARE 1 THROUGH 9.",
            "complete": False}


def _ttt_winner(board: list[str]) -> str:
    for a, b, c in ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6),
                    (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)):
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return "D" if " " not in board else ""


def _render_ttt(state: dict[str, Any]) -> str:
    board = state["board"]
    cells = [board[i] if board[i] != " " else str(i + 1) for i in range(9)]
    return (f" {cells[0]} | {cells[1]} | {cells[2]}\n---+---+---\n"
            f" {cells[3]} | {cells[4]} | {cells[5]}\n---+---+---\n"
            f" {cells[6]} | {cells[7]} | {cells[8]}\n\n{state['message']}")


def _ttt(state: dict[str, Any], command: str) -> tuple[bool, dict[str, Any] | None]:
    if command in ("QUIT", "EXIT"):
        return True, None
    if state["complete"]:
        if command in ("AGAIN", "PLAY AGAIN"):
            state.clear()
            state.update(_new_ttt())
        else:
            state["message"] = "TYPE AGAIN FOR ANOTHER GAME OR QUIT FOR JOSHUA."
        return False, None
    if not re.fullmatch(r"[1-9]", command):
        state["message"] = "SELECT AN UNOCCUPIED SQUARE FROM 1 THROUGH 9."
        return False, None
    position = int(command) - 1
    board = state["board"]
    if board[position] != " ":
        state["message"] = "THAT SQUARE IS ALREADY OCCUPIED."
        return False, None
    board[position] = "X"
    winner = _ttt_winner(board)
    if not winner:
        choices = [4, 0, 2, 6, 8, 1, 3, 5, 7]
        computer = next(value for value in choices if board[value] == " ")
        board[computer] = "O"
        winner = _ttt_winner(board)
    if winner:
        state["complete"] = True
        result = "stalemate" if winner == "D" else "player won" if winner == "X" else "Joshua won"
        state["message"] = result.upper() + ". TYPE AGAIN OR QUIT."
        score = 500 if winner == "X" else 200 if winner == "D" else 50
        return False, {"game": "tic-tac-toe", "score": score, "result": result}
    state["message"] = "YOUR MOVE."
    return False, None


def _render_wopr(state: dict[str, Any]) -> str:
    program = state.get("program")
    header = "WOPR COMPUTER SYSTEM / JOSHUA\n" + "=" * 62 + "\n"
    if program == "maze":
        return header + _render_maze(state["substate"]) + "\n\nQUIT returns to Joshua."
    if program == "gtw":
        return header + _render_gtw(state["substate"]) + "\n\nQUIT returns to Joshua."
    if program == "ttt":
        return header + "TIC-TAC-TOE\n\n" + _render_ttt(state["substate"])
    return (
        header + str(state["message"]) + "\n\n"
        "AVAILABLE COMMANDS: HELP, LIST GAMES, PLAY name, STATUS, QUIT\n"
        "ACTIVE ROUTINES: FALKEN'S MAZE, TIC-TAC-TOE, "
        "GLOBAL THERMONUCLEAR WAR"
    )


def _wopr(state: dict[str, Any], command: str) -> dict[str, Any]:
    program = state.get("program")
    if program in ("maze", "gtw"):
        adapter = _maze if program == "maze" else _gtw
        result = adapter(state["substate"], command)
        if result.get("exit") or state["substate"].get("phase") == "complete":
            concluding = str(state["substate"].get("message", "ROUTINE COMPLETE."))
            state["program"] = "shell"
            state["substate"] = None
            state["message"] = concluding + "\n\n** GAME ROUTINE COMPLETE **"
            result["exit"] = False
        return result
    if program == "ttt":
        finished, score = _ttt(state["substate"], command)
        if finished:
            state["program"] = "shell"
            state["substate"] = None
            state["message"] = "A STRANGE GAME. THE BOARD ALWAYS REMEMBERS."
        return {"score": score} if score else {}
    if command in ("QUIT", "EXIT", "LOGOUT"):
        state["message"] = "CONNECTION TERMINATED."
        return {"exit": True}
    if command in ("HELP", "?"):
        state["message"] = ("TYPE LIST GAMES, PLAY FALKEN'S MAZE, PLAY TIC-TAC-TOE, "
                            "PLAY GLOBAL THERMONUCLEAR WAR, STATUS, OR QUIT.")
    elif command in ("LIST", "LIST GAMES", "GAMES"):
        state["message"] = ("FALKEN'S MAZE\nTIC-TAC-TOE\nGLOBAL THERMONUCLEAR WAR\n"
                            "ADDITIONAL ROUTINES ARE NOT INSTALLED.")
    elif command == "STATUS":
        state["message"] = "PRIMARY CORE ONLINE. GAME AUTHORITY LINK SECURE. NO OUTBOUND NETWORK."
    elif command in ("HELLO", "HI", "GREETINGS"):
        state["message"] = "GREETINGS. HOW ARE YOU FEELING TODAY?"
    elif command in ("JOSHUA", "HELLO JOSHUA"):
        state["message"] = "GREETINGS, PROFESSOR FALKEN. SHALL WE PLAY A GAME?"
    elif command.startswith("PLAY "):
        selection = command[5:].replace("THE ", "", 1)
        if selection in ("FALKEN'S MAZE", "FALKENS MAZE", "MAZE"):
            state["program"] = "maze"
            state["substate"] = _new_maze(0, str(state["userid"]), int(state["seed"]))
        elif selection in ("TIC-TAC-TOE", "TIC TAC TOE"):
            state["program"] = "ttt"
            state["substate"] = _new_ttt()
        elif selection in ("GLOBAL THERMONUCLEAR WAR", "GTW"):
            state["program"] = "gtw"
            state["substate"] = _new_gtw(str(state["userid"]), int(state["seed"]))
        else:
            state["message"] = "** GAME ROUTINE NOT AVAILABLE ** TYPE LIST GAMES."
    elif not command:
        state["message"] = "SHALL WE PLAY A GAME?"
    else:
        state["message"] = "** IMPROPER REQUEST ** TYPE HELP FOR AVAILABLE COMMANDS."
    return {}
