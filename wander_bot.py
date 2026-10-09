#!/usr/bin/env python3
"""wander_bot.py -- an LLM-driven player for wanderlife.

Attaches to a world as a normal play client (one more `@`), periodically
shows the local surroundings to an LLM, and plays whatever action comes
back. Any OpenAI-compatible chat endpoint works: Ollama on the LAN, a
hosted Moonshot/Kimi API, etc.

Config (env):
  WANDER_SOCKET_PATH  server socket      (default ~/wander_server.sock)
  WANDER_BOT_WORLD    world to join      (default world-1)
  LLM_BASE_URL        OpenAI-compat base (default: if the pi coding-agent
                      env is present -- PI_PROVIDER=baseten -- then
                      https://inference.baseten.co/v1, else
                      http://localhost:11434/v1)
  LLM_MODEL           model name/tag     (default: $PI_MODEL, else kimi-k3)
  LLM_API_KEY         API token          (default: $BASETEN_API_KEY, else
                      "ollama" -- Ollama ignores it)
  LLM_AUTH_SCHEME     Authorization scheme (default: Api-Key for baseten.co,
                      Bearer otherwise; unless pinned, a 401/403 retries
                      once with the other scheme)
  BOT_TICK            seconds between decisions (default 2.5)

After a move decision the bot keeps that heading for a few cells, so a
slow reasoning model still wanders smoothly between decisions. If the LLM
call fails or returns garbage it falls back to a random walk (with the
occasional SQUARE), so a flaky endpoint never strands it. Detaches cleanly
on SIGTERM/SIGINT (its avatar despawns like any client's).

Run:  python3 wander_bot.py
Pi:   see deploy/wander-bot.service.template
"""
import collections
import json
import os
import random
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from wander_game import (ALL_PATTERNS, PATTERNS_OSCILLATORS,
                         PROTOCOL_VERSION)

SOCK = os.environ.get("WANDER_SOCKET_PATH",
                      os.path.expanduser("~/wander_server.sock"))
WORLD = os.environ.get("WANDER_BOT_WORLD", "world-1")
_DEFAULT_BASE = ("https://inference.baseten.co/v1"
                 if os.environ.get("PI_PROVIDER") == "baseten"
                 else "http://localhost:11434/v1")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", _DEFAULT_BASE)
LLM_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("PI_MODEL") or "kimi-k3"
LLM_API_KEY = (os.environ.get("LLM_API_KEY")
               or os.environ.get("BASETEN_API_KEY") or "ollama")
BOT_TICK = float(os.environ.get("BOT_TICK", "2.5"))
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "25"))
VIEW = 6  # observation window radius (13x13)

PATTERN_MENU = ", ".join(
    f"{i}:{name}({'osc' if i < len(PATTERNS_OSCILLATORS) else 'static'})"
    for i, (name, _) in enumerate(ALL_PATTERNS))

SYSTEM = f"""You are playing Wanderlife, a multiplayer Conway's Game of Life
playground, as the avatar @ on a grid. You decide ONE action per turn.

World: '*' living cell, 'X' dead cell (unoccupiable), '>v<^' baddies that
hunt living cells, '@' you, 'o' other players, '.' empty.

Actions (reply with exactly one JSON object, nothing else):
  {{"action":"move","dx":-1..1,"dy":-1..1}}   step; the bot keeps the heading
                          for a few cells, so you really travel
  {{"action":"square"}}   kill stars around you; beside an X: raise a baddie
  {{"action":"x"}}        erase * / reclaim X / neutralize a baddie under you
  {{"action":"circle"}}   stamp your active pattern (setup mode only)
  {{"action":"triangle"}} toggle setup <-> run mode
  {{"action":"pattern","idx":0..8}}           select what circle stamps
  {{"action":"seed"}}     scatter new life    {{"action":"wait"}}  do nothing
Optional "why":"short reason".

Stampable patterns (idx): {PATTERN_MENU}

Rhythm: wander until you find open space; in setup mode pick a pattern and
stamp a small garden (circle a few times); triangle into run mode and let
it live; square to prune overgrowth; back to setup to plant again. Change
modes when it makes sense -- don't camp in one mode all game. Use seed
sparingly, and never stamp onto another player. Your choices carry
momentum: a move keeps its heading for several cells and a circle lays a
short trail of stamps spaced by the pattern's footprint, so each decision
shapes the next few seconds. To spread several copies without overlap,
move at least the pattern's footprint (its WxH, shown above) plus one
between stamps."""


# --------------------------------------------------------------- protocol

class Server:
    """Newline-JSON protocol client; input/command are fire-and-forget."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCK)
        self.sock.setblocking(False)
        self.buf = b""

    def send(self, obj):
        obj["v"] = PROTOCOL_VERSION
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def poll(self):
        msgs = []
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    raise ConnectionError("server closed")
                self.buf += data
        except BlockingIOError:
            pass
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                msgs.append(json.loads(line))
        return msgs

    def rpc(self, obj, timeout=5.0):
        self.send(obj)
        end = time.time() + timeout
        while time.time() < end:
            for m in self.poll():
                if "ok" in m:
                    return m
            time.sleep(0.02)
        raise ConnectionError("rpc timeout")


# ------------------------------------------------------------------- LLM

def extract_json(text):
    """First balanced {...} block, quote-aware; None if the model rambled."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                in_str = False
            if c != "\\":
                esc = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def resolve_auth_scheme(base_url):
    """Baseten expects 'Authorization: Api-Key ...'; everyone else Bearer."""
    return (os.environ.get("LLM_AUTH_SCHEME")
            or ("Api-Key" if "baseten" in base_url else "Bearer"))


def http_json(url, payload=None):
    """GET/POST with the key; Baseten documents Api-Key but also accepts
    Bearer (pi's baseten provider uses Bearer): unless the scheme was
    pinned explicitly, retry a 401/403 once with the other scheme."""
    schemes = [resolve_auth_scheme(LLM_BASE_URL)]
    if not os.environ.get("LLM_AUTH_SCHEME"):
        schemes.append("Bearer" if schemes[0] == "Api-Key" else "Api-Key")
    for i, scheme in enumerate(schemes):
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"{scheme} {LLM_API_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and i + 1 < len(schemes):
                continue
            raise


def resolve_model():
    """Map LLM_MODEL to the endpoint's canonical id via /models:
    exact > case-insensitive > unique suffix (kimi-k3 -> moonshotai/Kimi-K3)."""
    try:
        data = http_json(f"{LLM_BASE_URL.rstrip('/')}/models")
        ids = [m.get("id", "") for m in data.get("data", [])]
    except Exception as e:
        print(f"bot: cannot list models ({type(e).__name__}: {e}); "
              f"using {LLM_MODEL!r} as-is", flush=True)
        return LLM_MODEL
    if LLM_MODEL in ids:
        return LLM_MODEL
    low = LLM_MODEL.lower()
    ci = [i for i in ids if i.lower() == low]
    if ci:
        picked = ci[0]
    else:
        suffix = sorted({i for i in ids
                         if i.lower() == low or i.lower().endswith("/" + low)})
        picked = suffix[0] if len(suffix) == 1 else None
    if picked:
        print(f"bot: model {LLM_MODEL!r} -> {picked!r} (endpoint's id)", flush=True)
        return picked
    near = [i for i in ids if low.split("/")[-1] in i.lower()][:5]
    print(f"bot: model {LLM_MODEL!r} not listed by endpoint; using as-is"
          + (f" -- did you mean: {', '.join(near)}?" if near else ""), flush=True)
    return LLM_MODEL


def ask_llm(observation, memory):
    msgs = [{"role": "system", "content": SYSTEM}]
    for prev in memory[-5:]:
        msgs.append({"role": "assistant", "content": json.dumps(prev)})
    msgs.append({"role": "user", "content": observation})
    payload = json.dumps({"model": LLM_MODEL, "messages": msgs,
                          "temperature": 0.4, "stream": False}).encode()
    body = http_json(f"{LLM_BASE_URL.rstrip('/')}/chat/completions", payload)
    return extract_json(body["choices"][0]["message"]["content"] or "")


# ------------------------------------------------------------ autonomy

def pattern_dims(name):
    """(w, h) footprint of a gallery pattern; 1x1 if unknown."""
    for n, offsets in ALL_PATTERNS:
        if n == name:
            xs = [o[0] for o in offsets]
            ys = [o[1] for o in offsets]
            return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    return 1, 1


def build_program(act, dx, dy, rng, dims=(1, 1)):
    """Follow-through between decisions: moves keep their heading; circle
    lays a trail of stamps spaced by the pattern's footprint so copies
    land side by side instead of overlapping; square gets a second swing."""
    if act == "move" and (dx or dy):
        return [("move", dx, dy)] * 6
    if act == "circle":
        ux, uy = ((dx, dy) if (dx or dy) else rng.choice(
            [(-1, -1), (-1, 0), (-1, 1), (0, -1),
             (0, 1), (1, -1), (1, 0), (1, 1)]))
        gap = max(dims) + 1  # one cell of daylight between copies
        step = (ux * gap, uy * gap)
        prog = []
        for _ in range(3):
            prog += [("input", "circle", None), ("move", *step)]
        prog.append(("input", "circle", None))
        return prog
    if act == "square":
        return [("input", "square", None)]
    return []


def fallback_action(state, pid, rng, fb):
    """Autopilot gardener: plants, flips modes and wanders with zero LLM,
    so the world is always being played."""
    now = time.time()
    if now - fb.get("last_triangle", 0) > 45:
        fb["last_triangle"] = now
        return {"action": "triangle", "why": "fallback: change of pace"}
    r = rng.random()
    if not state.get("is_running"):
        if r < 0.35:
            return {"action": "pattern", "idx": rng.randrange(len(ALL_PATTERNS)),
                    "why": "fallback: pick a pattern"}
        if r < 0.75:
            return {"action": "circle", "why": "fallback: stamp"}
    elif r < 0.08:
        return {"action": "square", "why": "fallback: cull"}
    return {"action": "move", "dx": rng.choice((-1, 0, 1)),
            "dy": rng.choice((-1, 0, 1)), "why": "fallback wander"}


# ------------------------------------------------------------ observation

def observe(state, pid, nudge=""):
    me = next((p for p in state.get("players", []) if p["pid"] == pid), None)
    if me is None:
        return None
    px, py = me["pos"]
    stars = {tuple(p) for p in state.get("asterisks", [])}
    dead = {tuple(p) for p in state.get("dead_cells", [])}
    bad = {tuple(b["pos"]): b for b in state.get("baddies", [])}
    others = {tuple(p["pos"]) for p in state.get("players", [])
              if p["pid"] != pid}
    rows = []
    for y in range(py - VIEW, py + VIEW + 1):
        row = []
        for x in range(px - VIEW, px + VIEW + 1):
            if (x, y) == (px, py):
                row.append("@")
            elif (x, y) in others:
                row.append("o")
            elif (x, y) in bad:
                d = bad[(x, y)]["dir"]
                row.append(">" if d[0] > 0 else "<" if d[0] < 0
                           else "v" if d[1] > 0 else "^")
            elif (x, y) in stars:
                row.append("*")
            elif (x, y) in dead:
                row.append("X")
            else:
                row.append(".")
        rows.append("".join(row))
    try:
        idx = [n for n, _ in ALL_PATTERNS].index(me["pattern_name"])
    except ValueError:
        idx = -1
    w, h = pattern_dims(me["pattern_name"])
    return (
        f"mode:{'run' if state.get('is_running') else 'setup'} "
        f"pos:({px},{py}) kill_r:{me.get('kill_radius')} "
        f"pattern:{me.get('pattern_name')}(idx {idx},{me.get('category')}) {w}x{h}\n"
        f"stars:{len(stars)} dead:{len(dead)} baddies:{len(bad)}"
        f"{' STOPPED' if state.get('baddies_stopped') else ''}"
        + (f"\nnudge: {nudge}" if nudge else "")
        + "\n" + "\n".join(rows))


def placeable_idx(me, step):
    names = [n for n, _ in ALL_PATTERNS]
    try:
        return (names.index(me["pattern_name"]) + step) % len(names)
    except ValueError:
        return 0


# ------------------------------------------------------------------- main

def clamp(v):
    try:
        return max(-1, min(1, int(v)))
    except (TypeError, ValueError):
        return 0


def safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def play():
    global LLM_MODEL
    LLM_MODEL = resolve_model()
    rng = random.Random()
    memory = []
    while True:  # reconnect loop
        try:
            srv = Server()
            r = srv.rpc({"cmd": "attach", "name": WORLD, "role": "play"})
            if not r.get("ok"):
                print(f"bot: attach to {WORLD!r} failed: {r.get('error')}; "
                      f"retrying in 5s", flush=True)
                time.sleep(5)
                continue
            pid = r["pid"]
            state = dict(r["state"])
            print(f"bot: attached to {WORLD!r} as @p{pid} "
                  f"(model {LLM_MODEL} via {LLM_BASE_URL})", flush=True)

            stop = {"flag": False}

            def on_term(signum, frame):
                stop["flag"] = True
            signal.signal(signal.SIGTERM, on_term)
            signal.signal(signal.SIGINT, on_term)

            def absorb():
                nonlocal state
                for m in srv.poll():
                    if m.get("type") != "state":
                        continue
                    if m.get("full"):
                        state = dict(m["state"])
                    else:
                        state.update(m.get("delta", {}))

            next_decision = 0.0
            program, next_step = collections.deque(), 0.0
            fb = {"last_triangle": 0.0}
            last_mode, mode_turns, since_stamp = None, 0, 0
            while not stop["flag"]:
                absorb()
                now = time.time()
                if now < next_decision:
                    # follow-through program: real travel and stamp trails
                    # between (possibly slow) LLM decisions
                    if program and now >= next_step:
                        kind, a, b = program.popleft()
                        if kind == "move":
                            srv.send({"cmd": "input", "action": "move",
                                      "dx": a, "dy": b})
                        else:
                            srv.send({"cmd": "input", "action": a})
                        next_step = now + 0.35
                    time.sleep(0.05)
                    continue

                running = bool(state.get("is_running"))
                nudge = ""
                if mode_turns >= 8:
                    nudge = (f"you have been in {'run' if last_mode else 'setup'} "
                             f"mode for {mode_turns} turns; consider triangle")
                elif not last_mode and since_stamp >= 8:
                    nudge = f"{since_stamp} turns without stamping; consider circle"
                obs = observe(state, pid, nudge)
                if obs is None:
                    continue
                # LLM call in a daemon thread: a SIGTERM mid-request must
                # still detach promptly instead of hanging until timeout.
                res = {}

                def work():
                    try:
                        res["action"] = ask_llm(obs, memory)
                    except Exception as e:
                        res["error"] = e
                th = threading.Thread(target=work, daemon=True)
                th.start()
                while th.is_alive() and not stop["flag"]:
                    th.join(0.1)
                    absorb()  # keep the socket drained while the model thinks;
                            # the server drops clients whose buffer fills
                if stop["flag"]:
                    break
                action = res.get("action")
                if "error" in res:
                    e = res["error"]
                    print(f"bot: LLM error ({type(e).__name__}: {e}); "
                          f"random step", flush=True)
                if not isinstance(action, dict):
                    action = fallback_action(state, pid, rng, fb)

                act = str(action.get("action", "wait")).lower()
                me = next((p for p in state.get("players", [])
                           if p["pid"] == pid), {})
                dx, dy = clamp(action.get("dx")), clamp(action.get("dy"))
                if act == "move":
                    srv.send({"cmd": "input", "action": "move",
                              "dx": dx, "dy": dy})
                elif act in ("square", "x", "circle", "triangle"):
                    srv.send({"cmd": "input", "action": act})
                elif act == "r3":
                    srv.send({"cmd": "command", "do": "stop_baddies"})
                elif act == "pattern":
                    srv.send({"cmd": "command", "do": "set_pattern",
                              "idx": safe_int(action.get("idx"))
                              % len(ALL_PATTERNS)})
                elif act == "seed":
                    srv.send({"cmd": "command", "do": "seed"})
                memory.append({"action": act,
                               **({"dx": dx, "dy": dy} if act == "move" else {}),
                               "why": str(action.get("why", ""))[:80]})
                program.clear()
                program.extend(build_program(
                    act, dx, dy, rng, pattern_dims(me.get("pattern_name"))))
                if running != last_mode:
                    last_mode, mode_turns = running, 0
                else:
                    mode_turns += 1
                since_stamp = 0 if act == "circle" else since_stamp + 1
                next_decision = time.time() + BOT_TICK  # pace after acting
                print(f"bot: {memory[-1]}", flush=True)

            try:
                srv.send({"cmd": "detach"})
            except OSError:
                pass
            print("bot: detached", flush=True)
            return 0
        except (ConnectionError, OSError) as e:
            print(f"bot: connection problem ({e}); retry in 5s", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(play() or 0)
