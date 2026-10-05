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


def test_players_count_as_living_cells():
    # One player completes a blinker; avatars never persist as stars.
    cells = {(5, 5), (7, 5)}
    out = wander.step_game_of_life(cells, 20, 20, player_positions={(6, 5)})
    assert out == {(6, 4), (6, 6)}, out
    # multiple players each count as living cells
    out2 = wander.step_game_of_life(set(), 20, 20,
                                    player_positions={(10, 10), (11, 10),
                                                      (10, 11), (11, 11)})
    assert out2 == set()  # players survive the tick but never become stars


def test_baddie_hunts_and_kills():
    rng = random.Random(42)
    asterisks = {(5, 5)}
    dead = set()
    baddies = [wander.Baddie(2, 2, 1, 0)]
    for _ in range(10):
        decayed, crowded = wander.update_baddies(baddies, asterisks, dead,
                                                 20, 20, rng, half_life=1e9)
        assert decayed == [] and crowded == []
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
                              half_life=1e9)  # no decay, single baddie: no crowding
    assert asterisks == set() and dead == set()
    assert 1 <= baddies[0].x <= 20 and 1 <= baddies[0].y <= 20


def test_baddie_decays_into_gallery_form():
    # half-life ~ 0  =>  decay probability 1.0 on the first tick
    rng = random.Random(1)
    asterisks = set()
    dead = set()
    baddies = [wander.Baddie(10, 10, 1, 0)]
    decayed, crowded = wander.update_baddies(baddies, asterisks, dead, 30, 30,
                                             rng, dt=1.0, half_life=1e-9)
    assert baddies == []                        # died ...
    assert len(decayed) == 1 and crowded == []  # ... with one decay event
    x, y, name = decayed[0]
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
    decayed, _ = wander.update_baddies(baddies, asterisks, dead, 30, 30, rng,
                                       dt=1.0, half_life=1e-9)
    assert len(decayed) == 1
    assert all(1 <= cx <= 30 and 1 <= cy <= 30 for cx, cy in asterisks)


def test_baddie_survives_when_half_life_is_long():
    rng = random.Random(3)
    baddies = [wander.Baddie(10, 10, 1, 0)]
    for _ in range(50):
        decayed, crowded = wander.update_baddies(baddies, set(), set(), 20, 20,
                                                 rng, dt=0.2, half_life=1e9)
        assert decayed == [] and crowded == []
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


def test_kill_area_radius_zero_and_two():
    # radius 0: only the cell under @
    asterisks = {(5, 5), (6, 5)}
    dead = set()
    n = wander.kill_area(5, 5, 0, asterisks, dead, 20, 20)
    assert n == 1 and asterisks == {(6, 5)} and dead == {(5, 5)}
    # radius 2: a 5x5 patch
    asterisks = {(5, 5), (7, 7), (8, 8)}
    dead = set()
    n = wander.kill_area(5, 5, 2, asterisks, dead, 20, 20)
    assert n == 2 and asterisks == {(8, 8)} and dead == {(5, 5), (7, 7)}


def test_baddie_glyph_matches_heading():
    assert wander.Baddie(1, 1, 1, 0).glyph() == ">"
    assert wander.Baddie(1, 1, -1, 0).glyph() == "<"
    assert wander.Baddie(1, 1, 0, 1).glyph() == "v"
    assert wander.Baddie(1, 1, 0, -1).glyph() == "^"
    # zero heading is normalized (never a silent '.')
    assert wander.Baddie(1, 1, 0, 0).glyph() == ">"


def test_baddies_self_destruct_when_crowded():
    rng = random.Random(11)
    asterisks = set()
    dead = set()
    baddies = [wander.Baddie(5, 5, 1, 0), wander.Baddie(6, 5, 1, 0)]  # adjacent
    decayed, crowded = wander.update_baddies(baddies, asterisks, dead, 30, 30,
                                             rng, half_life=1e9, crowd_radius=1)
    assert decayed == []
    assert sorted(crowded) == [(5, 5), (6, 5)]
    assert baddies == []                         # both ended themselves ...
    assert dead == {(5, 5), (6, 5)}              # ... leaving X corpses


def test_baddies_chain_reaction_clears_cluster():
    rng = random.Random(12)
    baddies = [wander.Baddie(5, 5), wander.Baddie(6, 5), wander.Baddie(7, 5)]
    decayed, crowded = wander.update_baddies(baddies, set(), set(), 30, 30,
                                             rng, half_life=1e9, crowd_radius=1)
    assert len(crowded) == 3 and baddies == []


def test_baddies_survive_when_spread_out():
    rng = random.Random(13)
    baddies = [wander.Baddie(2, 2), wander.Baddie(18, 18)]
    decayed, crowded = wander.update_baddies(baddies, set(), set(), 30, 30,
                                             rng, half_life=1e9, crowd_radius=1)
    assert crowded == [] and len(baddies) == 2


def test_kill_action_turns_baddies_into_gliders():
    asterisks = set()
    dead = set()
    baddies = [wander.Baddie(10, 10, 1, 0), wander.Baddie(18, 18, 1, 0)]
    n = wander.baddies_to_gliders(10, 10, 1, baddies, asterisks, dead, 30, 30)
    assert n == 1                                # only the baddie in range
    assert len(baddies) == 1 and baddies[0].pos == (18, 18)
    glider = {(10 + ox, 10 + oy) for ox, oy in dict(wander.ALL_PATTERNS)["Glider"]}
    assert asterisks == glider                   # stamped where it stood


def test_baddie_glider_conversion_respects_blocked_and_bounds():
    dead = {(10, 10)}                            # anchor cell is an X
    baddies = [wander.Baddie(10, 10, 1, 0)]
    asterisks = set()
    n = wander.baddies_to_gliders(10, 10, 1, baddies, asterisks, dead, 30, 30)
    assert n == 1
    assert (10, 10) not in asterisks             # X stays unoccupiable ...
    assert len(asterisks) == 4                   # ... glider loses that cell
    # corner: off-board cells are clipped
    baddies2 = [wander.Baddie(1, 1, 1, 0)]
    asterisks2 = set()
    wander.baddies_to_gliders(1, 1, 1, baddies2, asterisks2, set(), 30, 30)
    assert all(1 <= x <= 30 and 1 <= y <= 30 for x, y in asterisks2)


def test_seed_board_places_mixed_forms():
    rng = random.Random(5)
    asterisks = set()
    wander.seed_board(asterisks, 60, 40, rng, count=7)
    # every seedable form has >= 3 cells; allow slack for rare overlaps
    assert len(asterisks) >= 14
    assert all(1 <= x <= 60 and 1 <= y <= 40 for x, y in asterisks)


def test_save_load_roundtrip():
    state = {
        "version": 1,
        "player": [3, 4],
        "is_running": True,
        "baddies_stopped": True,
        "kill_radius": 3,
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
