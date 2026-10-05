#!/usr/bin/env python3
"""wander_server.py -- headless wander game server (protocol v2).

Hosts persistent Game-of-Life worlds over a UNIX socket. A world ticks at
full speed while at least one client is attached and BACKGROUND_SLOWDOWN
times slower otherwise, so worlds keep evolving gently in the background.
Worlds are persisted as JSON files (one per world) and reloaded on start.

Multiple clients may attach to the same world: role "play" can send
inputs, role "watch" receives frames only. Attached clients receive
*delta* frames: the first frame is full state, later frames carry only
the fields that changed since that client's last frame (nothing is sent
at all while the world is unchanged; a heartbeat frame proves liveness).

All file I/O happens on a background Saver thread so SD-card writes never
stall the tick loop.

Protocol: newline-delimited JSON. Every server message carries the
protocol version as "v". Clients should send "v" too; mismatches are
rejected.

  client -> server
    {"v": 2, "cmd": "list"}
    {"v": 2, "cmd": "create", "name": "foo", "max_x": 78, "max_y": 22}
    {"v": 2, "cmd": "delete", "name": "foo"}
    {"v": 2, "cmd": "attach", "name": "foo", "role": "play"|"watch"}
    {"v": 2, "cmd": "detach"}
    {"v": 2, "cmd": "input", "action": "move", "dx": 1, "dy": 0}
    {"v": 2, "cmd": "input", "action": "square"}  x/circle/triangle/l1/r1/l2/r2
    {"v": 2, "cmd": "command", "do": "save"|"clear"|"seed"|"stop_baddies"}
    {"v": 2, "cmd": "command", "do": "set_pattern", "idx": 3}

  server -> client
    {"v": 2, "ok": true, ...} / {"v": 2, "ok": false, "error": "..."}
    {"v": 2, "type": "state", "rev": n, "full": true,  "state": {...}}
    {"v": 2, "type": "state", "rev": n, "full": false, "delta": {...}}

Paths default to the home directory and can be overridden with
WANDER_SOCKET_PATH / WANDER_WORLDS_DIR (used by the test suite).
"""
import json
import os
import queue
import selectors
import signal
import socket
import threading
import time

from wander_game import BACKGROUND_SLOWDOWN, PROTOCOL_VERSION, World

BASE = os.path.expanduser("~")
SOCKET_PATH = os.environ.get("WANDER_SOCKET_PATH",
                             os.path.join(BASE, "wander_server.sock"))
WORLDS_DIR = os.environ.get("WANDER_WORLDS_DIR",
                            os.path.join(BASE, "wander_worlds"))

FRAME_DT = 0.1      # seconds between frame opportunities for attached clients
HEARTBEAT_DT = 5.0  # send a (possibly empty) frame at least this often
AUTOSAVE_DT = 30.0  # seconds between dirty-world saves
NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def sanitize_name(raw, maxlen=24):
    return "".join(c for c in str(raw or "") if c in NAME_OK)[:maxlen]


class Saver(threading.Thread):
    """Background file writer: json.dump + os.replace off the tick thread
    so slow SD-card writes never block world simulation."""

    def __init__(self):
        super().__init__(daemon=True, name="wander-saver")
        self.q = queue.Queue()
        self.start()

    def submit(self, path, data):
        self.q.put((path, data))

    def run(self):
        while True:
            path, data = self.q.get()
            try:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
            except Exception as e:
                print(f"save failed {path}: {e}", flush=True)
            finally:
                self.q.task_done()

    def flush(self):
        """Block until every queued write has landed."""
        self.q.join()


class Server:
    def __init__(self, socket_path=SOCKET_PATH, worlds_dir=WORLDS_DIR):
        self.socket_path = socket_path
        self.worlds_dir = worlds_dir
        os.makedirs(worlds_dir, exist_ok=True)
        self.worlds = {}
        self.attached = {}   # conn -> {"name", "role", "sent_rev", "last", "last_send"}
        self.buffers = {}    # conn -> received-but-unparsed bytes
        self.stop = False
        self.lsock = None
        self.saver = Saver()
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
        """Enqueue a write (returns immediately)."""
        self.saver.submit(self.world_path(w.name), w.to_dict())
        w.saved_rev = w.rev

    def save_dirty(self):
        for w in self.worlds.values():
            if w.rev != getattr(w, "saved_rev", -1):
                self.save_world(w)

    def save_all(self, flush=False):
        for w in self.worlds.values():
            self.save_world(w)
        if flush:
            self.saver.flush()

    # -------------------------------------------------------- connections
    def send(self, conn, obj):
        obj["v"] = PROTOCOL_VERSION
        conn.sendall((json.dumps(obj) + "\n").encode())

    def online_count(self, name):
        return sum(1 for a in self.attached.values() if a["name"] == name)

    def drop(self, conn, sel):
        entry = self.attached.pop(conn, None)
        if entry and entry["name"] in self.worlds:
            w = self.worlds[entry["name"]]
            if entry.get("pid") is not None:
                w.remove_player(entry["pid"])
            self.save_world(w)
            print(f"client detached from {entry['name']!r}", flush=True)
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
        v = msg.get("v")
        if v is not None and v != PROTOCOL_VERSION:
            self.send(conn, {"ok": False,
                             "error": f"protocol mismatch: server speaks "
                                      f"v{PROTOCOL_VERSION}, you sent v{v}"})
            return
        cmd = msg.get("cmd")
        try:
            if cmd == "list":
                infos = []
                for name in sorted(self.worlds):
                    info = self.worlds[name].info()
                    info["online"] = self.online_count(name)
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
                if self.online_count(name):
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
                else:
                    role = msg.get("role", "play")
                    if role not in ("play", "watch"):
                        role = "play"
                    # Play clients spawn their own '@'; watchers get none.
                    pid = w.add_player() if role == "play" else None
                    # The attach response doubles as the client's full
                    # frame; streamed frames are deltas from here on.
                    st = w.client_state()
                    self.attached[conn] = {"name": name, "role": role,
                                           "pid": pid, "sent_rev": w.rev,
                                           "last": st, "last_send": time.time()}
                    print(f"client attached to {name!r} as {role} "
                          f"(pid={pid}, {self.online_count(name)} online)",
                          flush=True)
                    self.send(conn, {"ok": True, "role": role, "pid": pid,
                                     "state": st})

            elif cmd == "detach":
                entry = self.attached.pop(conn, None)
                if entry and entry["name"] in self.worlds:
                    w = self.worlds[entry["name"]]
                    if entry.get("pid") is not None:
                        w.remove_player(entry["pid"])
                    self.save_world(w)
                    print(f"client detached from {entry['name']!r}", flush=True)
                self.send(conn, {"ok": True})

            elif cmd == "input":
                entry = self.attached.get(conn)
                w = self.worlds.get(entry["name"]) if entry else None
                if w is None:
                    self.send(conn, {"ok": False, "error": "not attached"})
                    return
                if entry["role"] != "play":
                    return  # watchers are read-only: ignore inputs silently
                act = msg.get("action")
                if act == "move":
                    w.move(entry["pid"], msg.get("dx", 0), msg.get("dy", 0))
                else:
                    w.button(entry["pid"], str(act))

            elif cmd == "command":
                entry = self.attached.get(conn)
                w = self.worlds.get(entry["name"]) if entry else None
                if w is None:
                    self.send(conn, {"ok": False, "error": "not attached"})
                    return
                if entry["role"] != "play":
                    self.send(conn, {"ok": False, "error": "watch-only"})
                    return
                do = msg.get("do")
                if do == "save":
                    self.save_world(w)
                    w.flash("Game saved")
                elif do == "clear":
                    w.clear(entry.get("pid"))
                elif do == "seed":
                    w.seed(entry.get("pid"))
                elif do == "stop_baddies":
                    w.toggle_baddies(entry.get("pid"))
                elif do == "set_pattern":
                    w.set_pattern(entry.get("pid"), int(msg.get("idx", 0)))

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
        print(f"wander server v{PROTOCOL_VERSION} listening on {self.socket_path}",
              flush=True)
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
                busy = {a["name"] for a in self.attached.values()}
                for name, w in self.worlds.items():
                    w.tick(dt, active=(name in busy))

                if now - last_frame >= FRAME_DT:
                    last_frame = now
                    for name in {a["name"] for a in self.attached.values()}:
                        w = self.worlds.get(name)
                        if w is None:
                            continue
                        conns = [c for c, a in self.attached.items()
                                 if a["name"] == name
                                 and (a["sent_rev"] != w.rev
                                      or now - a["last_send"] >= HEARTBEAT_DT)]
                        if not conns:
                            continue
                        cur = w.client_state()
                        for c in conns:
                            a = self.attached[c]
                            if a["sent_rev"] == -1:
                                frame = {"type": "state", "rev": w.rev,
                                         "full": True, "state": cur}
                            else:
                                delta = {k: v for k, v in cur.items()
                                         if a["last"].get(k) != v}
                                frame = {"type": "state", "rev": w.rev,
                                         "full": False, "delta": delta}
                            try:
                                self.send(c, frame)
                                a["sent_rev"] = w.rev
                                a["last"] = cur
                                a["last_send"] = now
                            except OSError:
                                self.drop(c, sel)

                if now - last_save >= AUTOSAVE_DT:
                    last_save = now
                    self.save_dirty()
        finally:
            print("shutting down: saving all worlds", flush=True)
            self.save_all(flush=True)
            for conn in list(self.buffers):
                self.drop(conn, sel)
            self.saver.flush()
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
