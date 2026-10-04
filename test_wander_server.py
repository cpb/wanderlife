#!/usr/bin/env python3
"""Tests for wander_game.World and the wander_server protocol.
Pure python -- no pygame, no curses needed."""
import json
import os
import random
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wander_game
from wander_game import (BACKGROUND_SLOWDOWN, GOL_TICK, KILL_RADIUS,
                         PATTERNS_STATIC, World)
from wander_server import Server


# ------------------------------------------------------------------ World

def test_world_created_seeded():
    w = World("t", 60, 40, rng=random.Random(1))
    assert len(w.asterisks) >= 14
    assert all(1 <= x <= 60 and 1 <= y <= 40 for x, y in w.asterisks)
    assert w.px == 30 and w.py == 20
    assert not w.is_running and w.kill_radius == KILL_RADIUS


def test_world_tick_evolves_blinker_at_full_speed():
    w = World("t", 30, 30, rng=random.Random(1))
    w.asterisks = {(5, 5), (6, 5), (7, 5)}
    w.is_running = True
    w.tick(GOL_TICK + 0.01, active=True)
    assert w.asterisks == {(6, 4), (6, 5), (6, 6)}


def test_world_background_tick_is_much_slower():
    w = World("t", 30, 30, rng=random.Random(1))
    w.asterisks = {(5, 5), (6, 5), (7, 5)}
    w.is_running = True
    # one connected-speed interval is nowhere near enough in background
    w.tick(GOL_TICK + 0.01, active=False)
    assert w.asterisks == {(5, 5), (6, 5), (7, 5)}
    # but the world does keep evolving, just slowly
    for _ in range(int(BACKGROUND_SLOWDOWN) + 4):
        w.tick(GOL_TICK, active=False)
    assert w.asterisks == {(6, 4), (6, 5), (6, 6)}


def test_world_setup_mode_freezes_everything():
    w = World("t", 30, 30, rng=random.Random(1))
    before = set(w.asterisks)
    for _ in range(50):
        w.tick(0.1, active=True)
    assert w.asterisks == before  # is_running False: nothing ticks


def test_world_square_kill_then_spawn_baddie():
    w = World("t", 30, 30, rng=random.Random(2))
    w.asterisks = {(10, 10)}
    w.px, w.py = 10, 10
    w.square()
    assert (10, 10) in w.dead_cells and not w.asterisks
    w.square()  # now beside/on an X with no stars in range -> baddie
    assert len(w.baddies) == 1 and w.baddies[0].pos == (10, 10)
    assert (10, 10) not in w.dead_cells


def test_world_square_converts_baddies_to_gliders():
    w = World("t", 30, 30, rng=random.Random(2))
    w.asterisks = set()
    w.baddies = [wander_game.Baddie(10, 10, 1, 0)]
    w.px, w.py = 10, 10
    w.square()
    assert w.baddies == []
    glider = {(10 + ox, 10 + oy) for ox, oy in wander_game.GLIDER_OFFSETS}
    assert w.asterisks == glider


def test_world_x_double_tap_clears_board():
    w = World("t", 30, 30, rng=random.Random(2))
    w.asterisks = {(3, 3)}
    w.dead_cells = {(4, 4)}
    w.baddies = [wander_game.Baddie(6, 6, 1, 0)]
    w.px, w.py = 15, 15
    w.x_tap()
    w.x_tap()  # immediate second tap = double tap
    assert not w.asterisks and not w.dead_cells and not w.baddies


def test_world_button_routing_depends_on_mode():
    w = World("t", 30, 30, rng=random.Random(2))
    w.is_running = False
    s0 = w.static_idx
    w.button("l2")  # setup: cycle static patterns
    assert w.static_idx == (s0 - 1) % len(PATTERNS_STATIC)
    assert w.kill_radius == KILL_RADIUS
    w.is_running = True
    w.button("r2")  # run: grow kill radius
    assert w.kill_radius == KILL_RADIUS + 1
    d0 = w.pattern_idx
    w.button("r1")  # run: pattern cycling disabled
    assert w.pattern_idx == d0


def test_world_serialization_roundtrip():
    w = World("t", 30, 30, rng=random.Random(3))
    w.px, w.py = 7, 9
    w.kill_radius = 3
    w.baddies_stopped = True
    w.is_running = True
    w.baddies = [wander_game.Baddie(5, 5, -1, 0)]
    d = json.loads(json.dumps(w.to_dict()))  # prove JSON-safety
    w2 = World.from_dict(d)
    assert w2.name == "t" and (w2.px, w2.py) == (7, 9)
    assert w2.kill_radius == 3 and w2.baddies_stopped and w2.is_running
    assert w2.asterisks == w.asterisks and w2.dead_cells == w.dead_cells
    assert len(w2.baddies) == 1 and w2.baddies[0].pos == (5, 5)


# --------------------------------------------------------- Server protocol

def test_server_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        sock_path = os.path.join(td, "test.sock")
        srv = Server(socket_path=sock_path, worlds_dir=td)
        th = threading.Thread(target=srv.serve, daemon=True)
        th.start()
        try:
            for _ in range(200):
                if os.path.exists(sock_path):
                    break
                time.sleep(0.02)
            assert os.path.exists(sock_path)

            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(sock_path)
            f = c.makefile("r", encoding="utf-8")

            def call(obj):
                c.sendall((json.dumps(obj) + "\n").encode())
                while True:  # skip streaming frames; wait for the response
                    m = json.loads(f.readline())
                    if "ok" in m:
                        return m

            r = call({"cmd": "list"})
            assert r["ok"] and r["worlds"] == []

            r = call({"cmd": "create", "name": "alpha", "max_x": 40, "max_y": 20})
            assert r["ok"] and r["name"] == "alpha"

            r = call({"cmd": "create", "name": "alpha"})  # duplicate rejected
            assert not r["ok"]

            r = call({"cmd": "attach", "name": "alpha"})
            assert r["ok"] and r["state"]["name"] == "alpha"
            assert len(r["state"]["asterisks"]) > 0
            px0 = r["state"]["px"]

            # a second client cannot attach to the busy world
            c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c2.connect(sock_path)
            f2 = c2.makefile("r", encoding="utf-8")
            c2.sendall((json.dumps({"cmd": "attach", "name": "alpha"}) + "\n").encode())
            m2 = json.loads(f2.readline())
            assert not m2["ok"] and "in use" in m2["error"]

            # movement input is reflected in streamed state frames
            c.sendall((json.dumps({"cmd": "input", "action": "move",
                                   "dx": 1, "dy": 0}) + "\n").encode())
            deadline = time.time() + 3
            seen = None
            while time.time() < deadline:
                m = json.loads(f.readline())
                if m.get("type") == "state":
                    seen = m["state"]
                    if seen["px"] == px0 + 1:
                        break
            assert seen and seen["px"] == px0 + 1

            # a world command reaches the world (clear empties the board)
            c.sendall((json.dumps({"cmd": "command", "do": "clear"}) + "\n").encode())
            deadline = time.time() + 3
            while time.time() < deadline:
                m = json.loads(f.readline())
                if m.get("type") == "state" and not m["state"]["asterisks"]:
                    break
            else:
                raise AssertionError("board never cleared")

            r = call({"cmd": "detach"})
            assert r["ok"]
            c2.sendall((json.dumps({"cmd": "detach"}) + "\n").encode())
            c.close()
            c2.close()
        finally:
            srv.stop = True
            th.join(timeout=5)

        # world persisted to disk and reloads
        with open(os.path.join(td, "alpha.json")) as fh:
            w = World.from_dict(json.load(fh))
        assert w.name == "alpha" and w.max_x == 40 and w.max_y == 20
        assert not w.asterisks  # cleared above


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    sys.exit(1 if failed else 0)
