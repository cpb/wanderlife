#!/usr/bin/env python3
"""wander_server.py -- headless wander game server.

Hosts persistent Game-of-Life worlds over a UNIX socket. A world ticks at
full speed while a client is attached and BACKGROUND_SLOWDOWN times
slower otherwise, so worlds keep evolving gently in the background.
Worlds are persisted as JSON files (one per world) and reloaded on start.

Protocol: newline-delimited JSON.

  client -> server
    {"cmd": "list"}
    {"cmd": "create", "name": "foo", "max_x": 78, "max_y": 22}
    {"cmd": "delete", "name": "foo"}
    {"cmd": "attach", "name": "foo"}     -> server starts streaming frames
    {"cmd": "detach"}
    {"cmd": "input", "action": "move", "dx": 1, "dy": 0}
    {"cmd": "input", "action": "square"}   x/circle/triangle/l1/r1/l2/r2
    {"cmd": "command", "do": "save" | "clear" | "seed" | "stop_baddies"}
    {"cmd": "command", "do": "set_pattern", "idx": 3}

  server -> client
    {"ok": true, ...} / {"ok": false, "error": "..."}
    {"type": "state", "state": {...}}    attached clients only, ~10 Hz

Paths default to the home directory and can be overridden with
WANDER_SOCKET_PATH / WANDER_WORLDS_DIR (used by the test suite).
"""
import json
import os
import selectors
import signal
import socket
import time

from wander_game import BACKGROUND_SLOWDOWN, World

BASE = os.path.expanduser("~")
SOCKET_PATH = os.environ.get("WANDER_SOCKET_PATH",
                             os.path.join(BASE, "wander_server.sock"))
WORLDS_DIR = os.environ.get("WANDER_WORLDS_DIR",
                            os.path.join(BASE, "wander_worlds"))

FRAME_DT = 0.1      # seconds between state frames to attached clients
AUTOSAVE_DT = 30.0  # seconds between periodic saves
NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def sanitize_name(raw, maxlen=24):
    return "".join(c for c in str(raw or "") if c in NAME_OK)[:maxlen]


class Server:
    def __init__(self, socket_path=SOCKET_PATH, worlds_dir=WORLDS_DIR):
        self.socket_path = socket_path
        self.worlds_dir = worlds_dir
        os.makedirs(worlds_dir, exist_ok=True)
        self.worlds = {}
        self.attached = {}   # conn -> world name
        self.buffers = {}    # conn -> received-but-unparsed bytes
        self.stop = False
        self.lsock = None
        self.load_worlds()

    # -------------------------------------------------------- persistence
    def load_worlds(self):
        for fn in sorted(os.listdir(self.worlds_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.worlds_dir, fn)
            try:
                with open(path) as f:
                    w = World.from_dict(json.load(f))
                self.worlds[w.name] = w
                print(f"loaded world {w.name!r} "
                      f"({len(w.asterisks)} stars, {len(w.baddies)} baddies)",
                      flush=True)
            except Exception as e:
                print(f"skipping corrupt world file {fn}: {e}", flush=True)

    def world_path(self, name):
        return os.path.join(self.worlds_dir, name + ".json")

    def save_world(self, w):
        tmp = self.world_path(w.name) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(w.to_dict(), f)
        os.replace(tmp, self.world_path(w.name))

    def save_all(self):
        for w in self.worlds.values():
            try:
                self.save_world(w)
            except Exception as e:
                print(f"save failed for {w.name!r}: {e}", flush=True)

    # -------------------------------------------------------- connections
    def send(self, conn, obj):
        conn.sendall((json.dumps(obj) + "\n").encode())

    def drop(self, conn, sel):
        name = self.attached.pop(conn, None)
        if name and name in self.worlds:
            self.save_world(self.worlds[name])  # save on detach
            print(f"client detached from {name!r}", flush=True)
        self.buffers.pop(conn, None)
        try:
            sel.unregister(conn)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    # ----------------------------------------------------------- commands
    def handle(self, conn, msg):
        cmd = msg.get("cmd")
        try:
            if cmd == "list":
                busy = set(self.attached.values())
                infos = []
                for name in sorted(self.worlds):
                    info = self.worlds[name].info()
                    info["attached"] = name in busy
                    infos.append(info)
                self.send(conn, {"ok": True, "worlds": infos})

            elif cmd == "create":
                name = sanitize_name(msg.get("name"))
                if not name:
                    self.send(conn, {"ok": False, "error": "bad name"})
                elif name in self.worlds:
                    self.send(conn, {"ok": False, "error": "world exists"})
                else:
                    mx = min(max(int(msg.get("max_x", 78)), 20), 400)
                    my = min(max(int(msg.get("max_y", 22)), 10), 200)
                    w = World(name, mx, my)
                    self.worlds[name] = w
                    self.save_world(w)
                    print(f"created world {name!r} ({mx}x{my})", flush=True)
                    self.send(conn, {"ok": True, "name": name})

            elif cmd == "delete":
                name = str(msg.get("name", ""))
                if name in self.attached.values():
                    self.send(conn, {"ok": False, "error": "world in use"})
                elif name not in self.worlds:
                    self.send(conn, {"ok": False, "error": "no such world"})
                else:
                    del self.worlds[name]
                    try:
                        os.remove(self.world_path(name))
                    except FileNotFoundError:
                        pass
                    print(f"deleted world {name!r}", flush=True)
                    self.send(conn, {"ok": True})

            elif cmd == "attach":
                name = str(msg.get("name", ""))
                w = self.worlds.get(name)
                if w is None:
                    self.send(conn, {"ok": False, "error": "no such world"})
                elif name in self.attached.values() \
                        and self.attached.get(conn) != name:
                    self.send(conn, {"ok": False, "error": "world in use"})
                else:
                    self.attached[conn] = name
                    print(f"client attached to {name!r}", flush=True)
                    self.send(conn, {"ok": True, "state": w.client_state()})

            elif cmd == "detach":
                name = self.attached.pop(conn, None)
                if name and name in self.worlds:
                    self.save_world(self.worlds[name])
                    print(f"client detached from {name!r}", flush=True)
                self.send(conn, {"ok": True})

            elif cmd == "input":
                w = self.worlds.get(self.attached.get(conn, ""))
                if w is None:
                    self.send(conn, {"ok": False, "error": "not attached"})
                    return
                act = msg.get("action")
                if act == "move":
                    w.move(msg.get("dx", 0), msg.get("dy", 0))
                else:
                    w.button(str(act))

            elif cmd == "command":
                w = self.worlds.get(self.attached.get(conn, ""))
                if w is None:
                    self.send(conn, {"ok": False, "error": "not attached"})
                    return
                do = msg.get("do")
                if do == "save":
                    self.save_world(w)
                    w.flash("Game saved")
                elif do == "clear":
                    w.clear()
                elif do == "seed":
                    w.seed()
                elif do == "stop_baddies":
                    w.toggle_baddies()
                elif do == "set_pattern":
                    w.set_pattern(int(msg.get("idx", 0)))

            else:
                self.send(conn, {"ok": False, "error": "unknown command"})
        except Exception as e:
            try:
                self.send(conn, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    # -------------------------------------------------------------- serve
    def serve(self):
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        self.lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lsock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.lsock.listen(8)
        self.lsock.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(self.lsock, selectors.EVENT_READ)
        print(f"wander server listening on {self.socket_path}", flush=True)
        print(f"worlds: {sorted(self.worlds) or '(none yet)'}; idle worlds "
              f"tick {BACKGROUND_SLOWDOWN:.0f}x slower", flush=True)

        last_tick = time.time()
        last_frame = time.time()
        last_save = time.time()
        try:
            while not self.stop:
                for key, _ in sel.select(0.02):
                    if key.fileobj is self.lsock:
                        conn, _ = self.lsock.accept()
                        conn.setblocking(False)
                        self.buffers[conn] = b""
                        sel.register(conn, selectors.EVENT_READ)
                    else:
                        conn = key.fileobj
                        try:
                            data = conn.recv(65536)
                        except OSError:
                            data = b""
                        if not data:
                            self.drop(conn, sel)
                            continue
                        self.buffers[conn] += data
                        while b"\n" in self.buffers[conn]:
                            line, self.buffers[conn] = \
                                self.buffers[conn].split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                msg = json.loads(line)
                            except ValueError:
                                continue
                            self.handle(conn, msg)

                now = time.time()
                dt = now - last_tick
                last_tick = now
                busy = set(self.attached.values())
                for name, w in self.worlds.items():
                    w.tick(dt, active=(name in busy))

                if now - last_frame >= FRAME_DT:
                    last_frame = now
                    for conn, name in list(self.attached.items()):
                        w = self.worlds.get(name)
                        if w is None:
                            continue
                        try:
                            self.send(conn, {"type": "state",
                                             "state": w.client_state()})
                        except OSError:
                            self.drop(conn, sel)

                if now - last_save >= AUTOSAVE_DT:
                    last_save = now
                    self.save_all()
        finally:
            print("shutting down: saving all worlds", flush=True)
            self.save_all()
            for conn in list(self.buffers):
                self.drop(conn, sel)
            try:
                sel.unregister(self.lsock)
            except Exception:
                pass
            try:
                self.lsock.close()
            except Exception:
                pass
            try:
                os.remove(self.socket_path)
            except FileNotFoundError:
                pass


def main():
    server = Server()

    def _stop(signum, frame):
        server.stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server.serve()


if __name__ == "__main__":
    main()
