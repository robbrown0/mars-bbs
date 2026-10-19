#!/usr/bin/env python3
"""Bounded deterministic malformed-command and saved-state testing."""

from __future__ import annotations

import json
import random
import string

from marsv2.games import GAMES, GameError, apply, new_game, render


rng = random.Random(1983)
alphabet = string.ascii_letters + string.digits + " ./;:'\"[]{}_-"
accepted = rejected = 0
for game in GAMES:
    for index in range(1000):
        state = new_game(game, index + 1, "fuzzer")
        if index % 7 == 0:
            command = "A" * 161
        elif index % 11 == 0:
            command = "PLAY\x1b]52;c;ZmFrZQ==\x07"
        elif index % 13 == 0:
            command = "GO\x00NORTH"
        else:
            command = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 160)))
        try:
            apply(game, state, command)
            encoded = json.dumps(state, ensure_ascii=True)
            assert len(encoded) <= 24_000
            assert "\x1b" not in render(game, state)
            accepted += 1
        except GameError:
            rejected += 1

for game in GAMES:
    for malformed in ({}, [], {"schema": 999, "game": game},
                      {"schema": 1, "game": "other"}):
        try:
            render(game, malformed)  # type: ignore[arg-type]
        except (GameError, KeyError, TypeError):
            pass
        else:
            raise AssertionError((game, malformed))

print(f"PASS: {accepted + rejected} bounded commands "
      f"({accepted} accepted, {rejected} rejected); corrupt states denied")
