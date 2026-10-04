#!/usr/bin/env python3
"""Logic tests for wander.py. Runs with plain python3 (no pygame needed:
it is stubbed out before import). Curses is never exercised."""
import os
import sys
import tempfile
import types
import random

# Stub pygame so wander.py can be imported without it installed.
_stub = types.ModuleType("pygame")
_stub.init = lambda *a, **k: None
_stub.quit = lambda *a, **k: None
_stub.joystick = types.SimpleNamespace(init=lambda: None, get_count=lambda: 0)
sys.modules["pygame"] = _stub

import wander


def test_block_is_stable():
    cells = {(2, 2), (3, 2), (2, 3), (3, 3)}
    assert wander.step_game_of_life(cells, 20, 20) == cells


def test_blinker_oscillates():
    blinker = {(5, 5), (6, 5), (7, 5)}
    step1 = wander.step_game_of_life(blinker, 20, 20)
    assert step1 == {(6, 4), (6, 5), (6, 6)}, step1
    assert wander.step_game_of_life(step1, 20, 20) == blinker


def test_blocked_cells_are_unoccupiable():
    # Three stars would birth a fourth at (3, 3) ...
    cells = {(2, 2), (3, 2), (2, 3)}
    born = wander.step_game_of_life(cells, 20, 20)
    assert (3, 3) in born
    # ... unless that cell is dead (X): nothing is born there.
    born_blocked = wander.step_game_of_life(cells, 20, 20, blocked={(3, 3)})
    assert (3, 3) not in born_blocked
    # Blocked cells never leak into the output, and are not counted alive.
    assert not (born_blocked & {(3, 3)})


def test_player_counts_as_living_cell():
    # Player completes a blinker; the avatar itself never persists as a star.
    cells = {(5, 5), (7, 5)}
    out = wander.step_game_of_life(cells, 20, 20, player_pos=(6, 5))
    assert out == {(6, 4), (6, 6)}, out


def test_baddie_hunts_and_kills():
    rng = random.Random(42)
    asterisks = {(5, 5)}
    dead = set()
    baddies = [wander.Baddie(2, 2, 1, 0)]
    for _ in range(10):
        events = wander.update_baddies(baddies, asterisks, dead, 20, 20, rng,
                                       half_life=1e9)  # no decay
        assert events == []
    assert asterisks == set(), asterisks       # star was eaten ...
    assert (5, 5) in dead                       # ... and became an X
    # baddie stays in bounds
    assert 1 <= baddies[0].x <= 20 and 1 <= baddies[0].y <= 20


def test_baddie_wanders_when_no_prey():
    rng = random.Random(7)
    asterisks = set()
    dead = set()
    baddies = [wander.Baddie(10, 10, 1, 0)]
    for _ in range(50):
        wander.update_baddies(baddies, asterisks, dead, 20, 20, rng,
                              half_life=1e9)  # no decay
    assert asterisks == set() and dead == set()
    assert 1 <= baddies[0].x <= 20 and 1 <= baddies[0].y <= 20


def test_baddie_decays_into_gallery_form():
    # half-life ~ 0  =>  decay probability 1.0 on the first tick
    rng = random.Random(1)
    asterisks = set()
    dead = set()
    baddies = [wander.Baddie(10, 10, 1, 0)]
    events = wander.update_baddies(baddies, asterisks, dead, 30, 30, rng,
                                   dt=1.0, half_life=1e-9)
    assert baddies == []                        # died ...
    assert len(events) == 1                     # ... with one decay event
    x, y, name = events[0]
    assert (x, y) == (10, 10)                   # where it died
    assert name in [n for n, _ in wander.ALL_PATTERNS]
    assert len(asterisks) >= 1                  # form was stamped
    assert all(1 <= cx <= 30 and 1 <= cy <= 30 for cx, cy in asterisks)
    assert not (asterisks & dead)               # dead cells stay unoccupiable


def test_baddie_decay_clipped_at_board_edge():
    rng = random.Random(2)
    asterisks = set()
    dead = set()
    baddies = [wander.Baddie(1, 1, 1, 0)]       # corner: some offsets fall off-board
    events = wander.update_baddies(baddies, asterisks, dead, 30, 30, rng,
                                   dt=1.0, half_life=1e-9)
    assert len(events) == 1
    assert all(1 <= cx <= 30 and 1 <= cy <= 30 for cx, cy in asterisks)


def test_baddie_survives_when_half_life_is_long():
    rng = random.Random(3)
    baddies = [wander.Baddie(10, 10, 1, 0)]
    for _ in range(50):
        events = wander.update_baddies(baddies, set(), set(), 20, 20, rng,
                                       dt=0.2, half_life=1e9)
        assert events == []
    assert len(baddies) == 1


def test_kill_area_turns_stars_to_x():
    asterisks = {(5, 5), (6, 5), (4, 4), (9, 9), (5, 8)}
    dead = set()
    n = wander.kill_area(5, 5, 1, asterisks, dead, 20, 20)
    assert n == 3                               # the 3x3 patch around (5,5)
    assert asterisks == {(9, 9), (5, 8)}        # outside the patch: untouched
    assert dead == {(5, 5), (6, 5), (4, 4)}
    # radius respects the board border without crashing
    asterisks2 = {(1, 1), (2, 1)}
    dead2 = set()
    n2 = wander.kill_area(1, 1, 1, asterisks2, dead2, 20, 20)
    assert n2 == 2 and asterisks2 == set() and dead2 == {(1, 1), (2, 1)}


def test_baddie_glyph_matches_heading():
    assert wander.Baddie(1, 1, 1, 0).glyph() == ">"
    assert wander.Baddie(1, 1, -1, 0).glyph() == "<"
    assert wander.Baddie(1, 1, 0, 1).glyph() == "v"
    assert wander.Baddie(1, 1, 0, -1).glyph() == "^"
    # zero heading is normalized (never a silent '.')
    assert wander.Baddie(1, 1, 0, 0).glyph() == ">"


def test_save_load_roundtrip():
    state = {
        "version": 1,
        "player": [3, 4],
        "is_running": True,
        "asterisks": [[1, 2], [5, 6]],
        "dead_cells": [[2, 2]],
        "baddies": [{"pos": [7, 8], "dir": [-1, 0]}],
        "category": "static",
        "pattern_idx": 2,
        "static_idx": 1,
    }
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "save.json")
        wander.save_game(path, state)
        loaded = wander.load_game(path)
    for k, v in state.items():
        assert loaded[k] == v, k
    assert "saved_at" in loaded


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    sys.exit(1 if failed else 0)
