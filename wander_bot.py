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

If the LLM call fails or returns garbage the bot takes a random step
instead, so a flaky endpoint never strands it. Detaches cleanly on
SIGTERM/SIGINT (its avatar despawns like any client's).

Run:  python3 wander_bot.py
Pi:   see deploy/wander-bot.service.template
"""
import json
import os
import random
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.request

from wander_game import ALL_PATTERNS, PROTOCOL_VERSION

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

SYSTEM = """You are playing Wanderlife, a multiplayer Conway's Game of Life
playground, as the avatar @ on a grid. You decide ONE action per turn.

World: '*' living cell, 'X' dead cell (unoccupiable), '>v<^' baddies that
hunt living cells, '@' you, 'o' other players, '.' empty.

Actions (reply with exactly one JSON object, nothing else):
  {"action":"move","dx":-1..1,"dy":-1..1}   step one cell
  {"action":"square"}   kill stars around you; beside an X: raise a baddie
  {"action":"x"}        erase * / reclaim X / neutralize a baddie under you
  {"action":"circle"}   stamp your active pattern (setup mode only)
  {"action":"triangle"} toggle setup <-> run mode
  {"action":"pattern","idx":0..8}           select the pattern circle stamps
  {"action":"seed"}     scatter new life    {"action":"wait"}  do nothing
Optional "why":"short reason".

Play style: wander with purpose. Stamp oscillators/gliders in setup mode,
flip to run to watch them live, kill overgrowth with square, keep baddies
spread out (crowded baddies self-destruct). Don't spam triangle/seed."""


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


def ask_llm(observation, memory):
    msgs = [{"role": "system", "content": SYSTEM}]
    for prev in memory[-5:]:
        msgs.append({"role": "assistant", "content": json.dumps(prev)})
    msgs.append({"role": "user", "content": observation})
    payload = json.dumps({"model": LLM_MODEL, "messages": msgs,
                          "temperature": 0.4, "stream": False}).encode()
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    # Baseten documents Api-Key but also accepts Bearer (pi's baseten
    # provider uses Bearer): unless the scheme was pinned explicitly,
    # retry a 401/403 once with the other scheme.
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
                body = json.loads(r.read())
            return extract_json(body["choices"][0]["message"]["content"] or "")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and i + 1 < len(schemes):
                continue
            raise


# ------------------------------------------------------------ observation

def observe(state, pid):
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
    return (
        f"mode:{'run' if state.get('is_running') else 'setup'} "
        f"pos:({px},{py}) kill_r:{me.get('kill_radius')} "
        f"pattern:{me.get('pattern_name')}(idx {idx},{me.get('category')})\n"
        f"stars:{len(stars)} dead:{len(dead)} baddies:{len(bad)}"
        f"{' STOPPED' if state.get('baddies_stopped') else ''}\n"
        + "\n".join(rows))


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

            next_decision = 0.0
            while not stop["flag"]:
                for m in srv.poll():
                    if m.get("type") != "state":
                        continue
                    if m.get("full"):
                        state = dict(m["state"])
                    else:
                        state.update(m.get("delta", {}))
                now = time.time()
                if now < next_decision:
                    time.sleep(0.05)
                    continue

                obs = observe(state, pid)
                if obs is None:
                    continue
                action = None
                try:
                    action = ask_llm(obs, memory)
                except Exception as e:
                    print(f"bot: LLM error ({type(e).__name__}: {e}); "
                          f"random step", flush=True)
                if not isinstance(action, dict):
                    action = {"action": "move",
                              "dx": rng.choice((-1, 0, 1)),
                              "dy": rng.choice((-1, 0, 1)),
                              "why": "fallback wander"}

                act = str(action.get("action", "wait")).lower()
                me = next((p for p in state.get("players", [])
                           if p["pid"] == pid), {})
                if act == "move":
                    srv.send({"cmd": "input", "action": "move",
                              "dx": clamp(action.get("dx")),
                              "dy": clamp(action.get("dy"))})
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
                               **({"dx": clamp(action.get("dx")),
                                   "dy": clamp(action.get("dy"))}
                                  if act == "move" else {}),
                               "why": str(action.get("why", ""))[:80]})
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
