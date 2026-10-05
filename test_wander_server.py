#!/usr/bin/env python3
"""Tests for wander_game.World and the wander_server protocol (v3).
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
                         PATTERNS_STATIC, PROTOCOL_VERSION, World)
from wander_server import Saver, Server


# ------------------------------------------------------------------ World

def test_world_created_seeded_no_players():
    w = World("t", 60, 40, rng=random.Random(1))
    assert len(w.asterisks) >= 14
    assert all(1 <= x <= 60 and 1 <= y <= 40 for x, y in w.asterisks)
    assert w.players == {} and not w.is_running


def test_world_add_and_remove_player():
    w = World("t", 60, 40, rng=random.Random(1))
    p1 = w.add_player()
    p2 = w.add_player()
    assert p1 != p2 and len(w.players) == 2
    for pid in (p1, p2):
        pl = w.players[pid]
        assert 1 <= pl.x <= 60 and 1 <= pl.y <= 40
        assert pl.kill_radius == KILL_RADIUS and pl.category == "dynamic"
    w.remove_player(p1)
    assert p1 not in w.players and p2 in w.players


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
    w.tick(GOL_TICK + 0.01, active=False)
    assert w.asterisks == {(5, 5), (6, 5), (7, 5)}
    for _ in range(int(BACKGROUND_SLOWDOWN) + 4):
        w.tick(GOL_TICK, active=False)
    assert w.asterisks == {(6, 4), (6, 5), (6, 6)}


def test_world_setup_mode_freezes_everything():
    w = World("t", 30, 30, rng=random.Random(1))
    before = set(w.asterisks)
    for _ in range(50):
        w.tick(0.1, active=True)
    assert w.asterisks == before  # is_running False: nothing ticks


def test_world_players_are_living_cells_in_run_mode():
    w = World("t", 30, 30, rng=random.Random(1))
    pid = w.add_player()
    pl = w.players[pid]
    pl.x, pl.y = 6, 5  # completes a blinker with the two stars
    w.asterisks = {(5, 5), (7, 5)}
    w.is_running = True
    w.tick(GOL_TICK + 0.01, active=True)
    assert w.asterisks == {(6, 4), (6, 6)}
    assert pl.pos == (6, 5)  # avatar untouched by the tick


def test_world_square_kill_then_spawn_baddie():
    w = World("t", 30, 30, rng=random.Random(2))
    pid = w.add_player()
    pl = w.players[pid]
    pl.x, pl.y = 10, 10
    w.asterisks = {(10, 10)}
    w.square(pid)
    assert (10, 10) in w.dead_cells and not w.asterisks
    w.square(pid)  # now beside/on an X with no stars in range -> baddie
    assert len(w.baddies) == 1 and w.baddies[0].pos == (10, 10)
    assert (10, 10) not in w.dead_cells
    assert "@p" in w.last_message and str(pid) in w.last_message


def test_world_square_converts_baddies_to_gliders():
    w = World("t", 30, 30, rng=random.Random(2))
    pid = w.add_player()
    pl = w.players[pid]
    pl.x, pl.y = 10, 10
    w.asterisks = set()
    w.baddies = [wander_game.Baddie(10, 10, 1, 0)]
    w.square(pid)
    assert w.baddies == []
    glider = {(10 + ox, 10 + oy) for ox, oy in wander_game.GLIDER_OFFSETS}
    assert w.asterisks == glider


def test_world_x_double_tap_clears_board():
    w = World("t", 30, 30, rng=random.Random(2))
    pid = w.add_player()
    w.players[pid].x, w.players[pid].y = 15, 15
    w.asterisks = {(3, 3)}
    w.dead_cells = {(4, 4)}
    w.baddies = [wander_game.Baddie(6, 6, 1, 0)]
    w.x_tap(pid)
    w.x_tap(pid)  # immediate second tap = double tap
    assert not w.asterisks and not w.dead_cells and not w.baddies


def test_world_button_routing_depends_on_mode():
    w = World("t", 30, 30, rng=random.Random(2))
    pid = w.add_player()
    pl = w.players[pid]
    w.is_running = False
    s0 = pl.static_idx
    w.button(pid, "l2")  # setup: cycle static patterns
    assert pl.static_idx == (s0 - 1) % len(PATTERNS_STATIC)
    assert pl.kill_radius == KILL_RADIUS
    w.is_running = True
    w.button(pid, "r2")  # run: grow kill radius
    assert pl.kill_radius == KILL_RADIUS + 1
    d0 = pl.pattern_idx
    w.button(pid, "r1")  # run: pattern cycling disabled
    assert pl.pattern_idx == d0


def test_world_serialization_excludes_transient_players():
    w = World("t", 30, 30, rng=random.Random(3))
    pid = w.add_player()
    w.baddies_stopped = True
    w.is_running = True
    w.baddies = [wander_game.Baddie(5, 5, -1, 0)]
    d = json.loads(json.dumps(w.to_dict()))  # prove JSON-safety
    assert "players" not in d
    w2 = World.from_dict(d)
    assert w2.name == "t" and w2.players == {}
    assert w2.baddies_stopped and w2.is_running
    assert w2.asterisks == w.asterisks and w2.dead_cells == w.dead_cells
    assert len(w2.baddies) == 1 and w2.baddies[0].pos == (5, 5)
    assert pid in w.players  # original world keeps its (runtime) players


# --------------------------------------------------------- Server protocol

def _client(sock_path):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10.0)  # generous: the Pi Zero can be heavily contended
    c.connect(sock_path)
    f = c.makefile("r", encoding="utf-8")

    def call(obj):
        obj.setdefault("v", PROTOCOL_VERSION)
        c.sendall((json.dumps(obj) + "\n").encode())
        while True:  # skip streaming frames; wait for the response
            m = json.loads(f.readline())
            if "ok" in m:
                return m

    def send(obj):
        obj.setdefault("v", PROTOCOL_VERSION)
        c.sendall((json.dumps(obj) + "\n").encode())

    def _players(m):
        return m["state"]["players"] if m.get("full") \
            else m.get("delta", {}).get("players")

    def wait_player_pos(pid, target, timeout=10.0):
        """Frames until player `pid` is at [x, y]. Tolerant of stale and
        coalesced frames: only the final value is deterministic."""
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(f.readline())
            if m.get("type") != "state":
                continue
            players = _players(m)
            if not players:
                continue
            for p in players:
                if p["pid"] == pid and p["pos"] == list(target):
                    return m
        raise AssertionError(f"player {pid} never reached {target}")

    def wait_delta_with(key, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(f.readline())
            if m.get("type") == "state" and not m.get("full") \
                    and key in m.get("delta", {}):
                return m
        raise AssertionError(f"no delta frame carrying {key!r}")

    def wait_stars_empty(timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(f.readline())
            if m.get("type") != "state":
                continue
            ast = m["state"]["asterisks"] if m.get("full") \
                else m.get("delta", {}).get("asterisks")
            if ast == []:
                return m
        raise AssertionError("board never cleared")

    return (c, f, call, send, wait_player_pos, wait_delta_with, wait_stars_empty)


def test_saver_writes_files_offthread():
    with tempfile.TemporaryDirectory() as td:
        s = Saver()
        p = os.path.join(td, "w.json")
        s.submit(p, {"a": 1})
        s.flush()  # returns only when the write has landed
        with open(p) as fh:
            assert json.load(fh) == {"a": 1}


def test_server_end_to_end_multi_player():
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

            c, f, call, send, wait_pos, wait_dkey, wait_stars = _client(sock_path)

            r = call({"cmd": "list"})
            assert r["ok"] and r["worlds"] == [] and r["v"] == PROTOCOL_VERSION

            bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            bad.settimeout(10.0)
            bad.connect(sock_path)
            bad.sendall(b'{"cmd": "list", "v": 999}\n')
            bf = bad.makefile("r", encoding="utf-8")
            m = json.loads(bf.readline())
            assert not m["ok"] and "protocol" in m["error"]
            bad.close()

            r = call({"cmd": "create", "name": "alpha", "max_x": 40, "max_y": 20})
            assert r["ok"]
            assert not call({"cmd": "create", "name": "alpha"})["ok"]  # dup

            # two play clients -> two distinct avatars in the same world
            r = call({"cmd": "attach", "name": "alpha"})
            assert r["ok"] and r["role"] == "play"
            pid1 = r["pid"]
            players1 = {p["pid"]: p for p in r["state"]["players"]}
            assert len(players1) == 1 and pid1 in players1
            pos1 = players1[pid1]["pos"]

            c2, f2, call2, send2, wait_pos2, wait_dkey2, _ = _client(sock_path)
            r2 = call2({"cmd": "attach", "name": "alpha"})
            pid2 = r2["pid"]
            assert pid2 != pid1 and len(r2["state"]["players"]) == 2
            pos2 = {p["pid"]: p for p in r2["state"]["players"]}[pid2]["pos"]

            # a watcher spawns no avatar
            c3, f3, call3, send3, wait_pos3, _, _ = _client(sock_path)
            r3 = call3({"cmd": "attach", "name": "alpha", "role": "watch"})
            assert r3["ok"] and r3["role"] == "watch" and r3["pid"] is None
            assert len(r3["state"]["players"]) == 2

            assert not call({"cmd": "delete", "name": "alpha"})["ok"]  # busy

            # moving c1 moves only c1's avatar; everyone sees it
            send({"cmd": "input", "action": "move", "dx": 1, "dy": 0})
            m = wait_pos(pid1, (pos1[0] + 1, pos1[1]))
            for p in m.get("state", m.get("delta", {})).get("players", []):
                if p["pid"] == pid2:
                    assert p["pos"] == pos2  # c2's avatar untouched
            wait_pos2(pid1, (pos1[0] + 1, pos1[1]))   # second player sees it
            wait_pos3(pid1, (pos1[0] + 1, pos1[1]))   # watcher sees it too

            # frames after attach are deltas; a pure move carries players only
            send({"cmd": "input", "action": "move", "dx": 1, "dy": 0})
            m = wait_dkey("players")
            assert m["v"] == PROTOCOL_VERSION
            assert "asterisks" not in m["delta"]

            # watcher input is ignored; c1's next move lands on its own avatar
            send3({"cmd": "input", "action": "move", "dx": 9, "dy": 9})
            send({"cmd": "input", "action": "move", "dx": 0, "dy": 1})
            wait_pos(pid1, (pos1[0] + 2, pos1[1] + 1))

            # world command works and is attributable (clear via c1)
            send({"cmd": "command", "do": "clear"})
            wait_stars()

            # detaching despawns that client's avatar
            r = call({"cmd": "detach"})
            assert r["ok"]
            infos = call2({"cmd": "list"})["worlds"]
            assert infos[0]["players"] == 1  # only c2's avatar remains

            call2({"cmd": "detach"})
            call3({"cmd": "detach"})
            assert call({"cmd": "delete", "name": "alpha"})["ok"]

            # recreate + attach/detach to leave a world on disk for reload
            call({"cmd": "create", "name": "beta", "max_x": 33, "max_y": 21})
            call({"cmd": "attach", "name": "beta"})
            call({"cmd": "detach"})
            c.close()
            c2.close()
            c3.close()
        finally:
            srv.stop = True
            th.join(timeout=5)

        with open(os.path.join(td, "beta.json")) as fh:
            w = World.from_dict(json.load(fh))
        assert w.name == "beta" and w.max_x == 33 and w.max_y == 21
        assert w.players == {}  # players are transient, never persisted


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
