#!/usr/bin/env python3
"""wander_client.py -- client for the wander game server (protocol v2).

Opens at a world menu: play an existing world, create a new one, watch a
world, or delete one. Multiple clients can attach to the same world --
several players share the one '@' (chaos is a feature), or attach with
the watch role to just observe.

Input: a paired PS3 controller if present, otherwise the keyboard:

  move            arrows / hjkl / wasd
  SQUARE kill     SPACE          X erase       x
  CIRCLE stamp    o              TRIANGLE run  t
  L1 / R1         [ / ]          L2 / R2       - / =
  R3 (baddies)    b              menu          ESC or m
  confirm         ENTER          (menus)       arrows + ENTER

  python3 wander_client.py
"""
import os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ.setdefault("ESCDELAY", "25")  # snappy ESC key in curses

import curses
import json
import socket
import time

import pygame

from wander_game import (ALL_PATTERNS, PATTERNS_OSCILLATORS, PROTOCOL_VERSION)

BASE = os.path.expanduser("~")
SOCKET_PATH = os.environ.get("WANDER_SOCKET_PATH",
                             os.path.join(BASE, "wander_server.sock"))

# PS3 button map (pygame / sixaxis)
BTN_X, BTN_CIRCLE, BTN_TRIANGLE, BTN_SQUARE = 0, 1, 2, 3
BTN_L1, BTN_R1, BTN_L2, BTN_R2 = 4, 5, 6, 7
BTN_SELECT, BTN_START, BTN_PS = 8, 9, 10
BTN_R3 = 12

PAD_BUTTONS = {
    "square": BTN_SQUARE, "x": BTN_X, "circle": BTN_CIRCLE,
    "triangle": BTN_TRIANGLE, "l1": BTN_L1, "r1": BTN_R1,
    "l2": BTN_L2, "r2": BTN_R2, "select": BTN_SELECT,
    "start": BTN_START, "ps": BTN_PS, "r3": BTN_R3,
}

KEYMAP = {
    "square": {ord(" ")},
    "x": {ord("x")},
    "circle": {ord("o")},
    "triangle": {ord("t")},
    "l1": {ord("[")}, "r1": {ord("]")},
    "l2": {ord("-")}, "r2": {ord("=")},
    "r3": {ord("b")},
    "select": {27, ord("m")},   # ESC or m
    "start": {27},
    "confirm": {curses.KEY_ENTER, 10, 13},
    "watch": {ord("w")},
}

# Logical buttons forwarded to the world as game actions
ACTION_BUTTONS = ("square", "x", "circle", "triangle", "l1", "r1", "l2", "r2")


# ------------------------------------------------------------------- Input

class PadInput:
    kind = "pad"

    def __init__(self):
        pygame.init()
        pygame.joystick.init()
        self.js = pygame.joystick.Joystick(0)
        self.js.init()
        self.prev = {}

    def pump(self):
        pygame.event.pump()

    def nav(self):
        dx = dy = 0
        if self.js.get_numhats() > 0:
            hx, hy = self.js.get_hat(0)
            dx, dy = hx, -hy
        if dx == 0 and dy == 0:
            ax, ay = self.js.get_axis(0), self.js.get_axis(1)
            if ax < -0.5:
                dx = -1
            elif ax > 0.5:
                dx = 1
            if ay < -0.5:
                dy = -1
            elif ay > 0.5:
                dy = 1
        return dx, dy

    def _down(self, name):
        btn = PAD_BUTTONS[name]
        return btn < self.js.get_numbuttons() and self.js.get_button(btn)

    def edge(self, name):
        now = self._down(name)
        was = self.prev.get(name, False)
        self.prev[name] = now
        return now and not was

    def release_sync(self):
        start = time.time()
        while time.time() - start < 2.0:
            pygame.event.pump()
            if not any(self.js.get_button(i) for i in range(self.js.get_numbuttons())):
                return
            time.sleep(0.02)


class KeyInput:
    kind = "keys"

    def __init__(self, stdscr):
        stdscr.keypad(True)
        self.stdscr = stdscr
        self.keys = []

    def pump(self):
        self.keys = []
        while True:
            k = self.stdscr.getch()
            if k == -1:
                break
            self.keys.append(k)

    def nav(self):
        dx = dy = 0
        for k in self.keys:
            if k in (curses.KEY_UP, ord("k"), ord("w")):
                dy = -1
            elif k in (curses.KEY_DOWN, ord("j"), ord("s")):
                dy = 1
            elif k in (curses.KEY_LEFT, ord("h"), ord("a")):
                dx = -1
            elif k in (curses.KEY_RIGHT, ord("l"), ord("d")):
                dx = 1
        return dx, dy

    def edge(self, name):
        keys = KEYMAP.get(name)
        return bool(keys) and any(k in keys for k in self.keys)

    def release_sync(self):
        pass  # keyboard is event-based; nothing is "held" here


# --------------------------------------------------------------- Networking

class Conn:
    """Newline-delimited JSON connection to the wander server (v2)."""

    def __init__(self, path=SOCKET_PATH):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
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
                    raise ConnectionError("server closed the connection")
                self.buf += data
        except BlockingIOError:
            pass
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                msgs.append(json.loads(line))
        return msgs

    def rpc(self, obj, timeout=3.0):
        self.send(obj)
        end = time.time() + timeout
        while time.time() < end:
            for m in self.poll():
                if "ok" not in m:
                    continue
                if m.get("v") != PROTOCOL_VERSION:
                    raise ConnectionError(
                        f"protocol mismatch: server v{m.get('v')}, "
                        f"client v{PROTOCOL_VERSION} -- restart the server "
                        f"(sudo systemctl restart wander)")
                return m
            time.sleep(0.01)
        raise ConnectionError("server timeout")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ----------------------------------------------------------- Curses helpers

def safe_addch(win, y, x, ch, attr=0):
    try:
        win.addch(y, x, ch, attr)
    except (curses.error, UnicodeEncodeError):
        pass


def safe_addstr(win, y, x, s, attr=0):
    try:
        win.addstr(y, x, s, attr)
    except (curses.error, UnicodeEncodeError):
        pass


# --------------------------------------------------------------------- Menus

def run_menu(stdscr, inp, sw, sh, title, items,
             hint="move: D-Pad/keys   choose: X/ENTER   back: SELECT/ESC"):
    inp.release_sync()
    sel = 0
    move_cd = time.time()

    while True:
        inp.pump()
        now = time.time()

        _, dy = inp.nav()
        if dy and now - move_cd > 0.18:
            move_cd = now
            sel = (sel + (1 if dy > 0 else -1)) % len(items)

        if (inp.edge("x") or inp.edge("circle")
                or (inp.kind == "keys" and inp.edge("confirm"))):
            inp.release_sync()
            return items[sel][0]
        if inp.edge("select") or inp.edge("start"):
            inp.release_sync()
            return None

        stdscr.erase()
        attr = curses.color_pair(4) | curses.A_BOLD
        stdscr.attron(attr)
        stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
        stdscr.attroff(attr)
        safe_addstr(stdscr, 0, max(2, (sw - len(title) - 2) // 2), f" {title} ", attr)

        y0 = max(2, sh // 2 - len(items))
        x0 = max(4, sw // 2 - 20)
        for i, (_, label) in enumerate(items):
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y0 + i, x0, ("-> " if i == sel else "   ") + label, line_attr)

        safe_addstr(stdscr, sh - 2, max(2, (sw - len(hint)) // 2), hint, curses.color_pair(2))
        stdscr.refresh()
        time.sleep(0.02)


def run_gallery_menu(stdscr, inp, sw, sh):
    """Browse placeable patterns. Returns ALL_PATTERNS index, or None."""
    inp.release_sync()
    sel = 0
    move_cd = time.time()
    hint = "browse: D-Pad/keys   choose: X/ENTER   back: SELECT/ESC"

    while True:
        inp.pump()
        now = time.time()

        dx, dy = inp.nav()
        nav = dy or dx
        if nav and now - move_cd > 0.16:
            move_cd = now
            sel = (sel + (1 if nav > 0 else -1)) % len(ALL_PATTERNS)

        if (inp.edge("x") or inp.edge("circle")
                or (inp.kind == "keys" and inp.edge("confirm"))):
            inp.release_sync()
            return sel
        if inp.edge("select") or inp.edge("start"):
            inp.release_sync()
            return None

        stdscr.erase()
        attr = curses.color_pair(4) | curses.A_BOLD
        stdscr.attron(attr)
        stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
        stdscr.attroff(attr)
        safe_addstr(stdscr, 0, 4, " GALLERY OF PLACEABLES ", attr)

        for i, (name, _) in enumerate(ALL_PATTERNS):
            y = 3 + i
            if y >= sh - 3:
                break
            cat = "OSC" if i < len(PATTERNS_OSCILLATORS) else "STATIC"
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y, 4, f"{'-> ' if i == sel else '   '}{name:<13} {cat}", line_attr)

        name, offsets = ALL_PATTERNS[sel]
        minx = min(o[0] for o in offsets)
        maxx = max(o[0] for o in offsets)
        miny = min(o[1] for o in offsets)
        maxy = max(o[1] for o in offsets)
        w = maxx - minx + 1
        h = maxy - miny + 1
        base_x = min(sw - w - 4, 3 * sw // 4 - w // 2)
        base_y = max(4, sh // 2 - h // 2)
        safe_addstr(stdscr, base_y - 2, base_x, f"{name}  (@ = you)",
                    curses.color_pair(4) | curses.A_BOLD)
        for ox, oy in offsets:
            cx = base_x + ox - minx
            cy = base_y + oy - miny
            if (ox, oy) == (0, 0):
                safe_addch(stdscr, cy, cx, "@", curses.color_pair(1) | curses.A_BOLD)
            else:
                safe_addch(stdscr, cy, cx, "*", curses.color_pair(2))

        safe_addstr(stdscr, sh - 2, max(2, (sw - len(hint)) // 2), hint, curses.color_pair(2))
        stdscr.refresh()
        time.sleep(0.02)


# --------------------------------------------------------------- World menu

def next_world_name(worlds):
    taken = {w["name"] for w in worlds}
    n = 1
    while f"world-{n}" in taken:
        n += 1
    return f"world-{n}"


def pick_world(stdscr, inp, conn, sw, sh):
    """World menu: play / create / watch / delete.
    Returns ("play", state, role) or ("quit", None, None)."""
    inp.release_sync()
    sel = 0
    move_cd = time.time()
    worlds = []
    dirty = True
    refresh_at = 0.0
    flash, flash_until = "", 0.0
    arm_delete = (None, 0.0)

    while True:
        now = time.time()
        if dirty or now >= refresh_at:
            try:
                r = conn.rpc({"cmd": "list"})
                worlds = r.get("worlds", []) if r.get("ok") else []
            except (ConnectionError, OSError, ValueError) as e:
                flash, flash_until = f"server error: {e}", now + 4.0
            dirty = False
            refresh_at = now + 2.0

        items = [w["name"] for w in worlds] + ["+ Create new world", "Quit"]
        sel = min(sel, len(items) - 1)

        inp.pump()
        _, dy = inp.nav()
        if dy and now - move_cd > 0.18:
            move_cd = now
            sel = (sel + (1 if dy > 0 else -1)) % len(items)

        choice = items[sel]

        def attach(role):
            r = conn.rpc({"cmd": "attach", "name": choice, "role": role})
            if r.get("ok"):
                return ("play", r["state"], r.get("role", role))
            raise ConnectionError(r.get("error", "attach failed"))

        if (inp.edge("x") or inp.edge("circle")
                or (inp.kind == "keys" and inp.edge("confirm"))):
            inp.release_sync()
            dirty = True
            if choice == "Quit":
                return ("quit", None, None)
            try:
                if choice == "+ Create new world":
                    r = conn.rpc({"cmd": "create", "name": next_world_name(worlds),
                                  "max_x": sw - 2, "max_y": sh - 2})
                    if not r.get("ok"):
                        flash, flash_until = f"create failed: {r.get('error')}", now + 3.0
                        continue
                    choice = r["name"]
                return attach("play")
            except (ConnectionError, OSError, ValueError) as e:
                flash, flash_until = f"attach failed: {e}", now + 4.0

        watch_edge = inp.edge("triangle") or (inp.kind == "keys" and inp.edge("watch"))
        if watch_edge and choice not in ("+ Create new world", "Quit"):
            inp.release_sync()
            dirty = True
            try:
                return attach("watch")
            except (ConnectionError, OSError, ValueError) as e:
                flash, flash_until = f"watch failed: {e}", now + 4.0

        if inp.edge("square") and choice not in ("+ Create new world", "Quit"):
            inp.release_sync()
            dirty = True
            if arm_delete[0] == choice and now - arm_delete[1] < 3.0:
                arm_delete = (None, 0.0)
                try:
                    r = conn.rpc({"cmd": "delete", "name": choice})
                    flash, flash_until = ("World deleted" if r.get("ok")
                                          else f"delete failed: {r.get('error')}",
                                          now + 3.0)
                except (ConnectionError, OSError, ValueError) as e:
                    flash, flash_until = f"server error: {e}", now + 3.0
            else:
                arm_delete = (choice, now)
                flash, flash_until = f"Press SQUARE again to delete '{choice}'", now + 3.0

        # ---- draw ----
        stdscr.erase()
        attr = curses.color_pair(4) | curses.A_BOLD
        stdscr.attron(attr)
        stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
        stdscr.attroff(attr)
        safe_addstr(stdscr, 0, 4, " WANDER WORLDS ", attr)
        mode = "PS3 controller" if inp.kind == "pad" else "keyboard"
        safe_addstr(stdscr, 1, 4, f"server: {SOCKET_PATH}   input: {mode}",
                    curses.color_pair(3))

        y0 = 4
        for i, w in enumerate(worlds):
            if y0 + i >= sh - 4:
                break
            flags = "run" if w["running"] else "setup"
            if w.get("online"):
                flags += f" ({w['online']} online)"
            label = f"{w['name']:<16} *{w['stars']:<4} X{w['xs']:<4} B{w['baddies']:<3} {flags}"
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y0 + i, 6, ("-> " if i == sel else "   ") + label, line_attr)
        for j in range(len(items) - len(worlds)):
            i = len(worlds) + j
            if y0 + i >= sh - 4:
                break
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y0 + i, 6, ("-> " if i == sel else "   ") + items[i], line_attr)

        hint = "X/ENTER: play   TRIANGLE/w: watch   SQUARE x2: delete"
        safe_addstr(stdscr, sh - 2, max(2, (sw - len(hint)) // 2), hint, curses.color_pair(2))
        if now < flash_until:
            safe_addstr(stdscr, sh - 1, max(1, sw - len(flash) - 3), flash,
                        curses.color_pair(4) | curses.A_BOLD)
        stdscr.refresh()
        time.sleep(0.02)


# -------------------------------------------------------------- Game screen

def baddie_glyph(bdx, bdy):
    if abs(bdx) >= abs(bdy):
        return ">" if bdx > 0 else "<"
    return "v" if bdy > 0 else "^"


def render_state(stdscr, state, ast_set, dead_set, px, py, sw, sh, role):
    max_x, max_y = state["max_x"], state["max_y"]
    is_running = state["is_running"]
    kill_radius = state["kill_radius"]
    now = time.time()

    stdscr.erase()
    border_attr = curses.color_pair(3) if is_running else curses.color_pair(5)
    stdscr.attron(border_attr)
    stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
    stdscr.attroff(border_attr)

    if role == "watch":
        head = f"[ WATCHING: {state['name']} ]  SEL/ESC: menu"
    elif is_running:
        head = "[ RUN ] □:kill ✕:erase(✕✕:clear) L2/R2:area R3:stop △:setup SEL:menu"
    else:
        head = "[ SETUP ] □:kill/spawn ○:stamp ✕:erase(✕✕:clear) △:run SEL:menu"
    safe_addstr(stdscr, 0, 4, f" {head} ", border_attr | curses.A_BOLD)
    bmark = "s" if state["baddies_stopped"] else ""
    stats = (f" *{len(ast_set)} X{len(dead_set)}"
             f" B{len(state['baddies'])}{bmark} R{kill_radius} ({px},{py}) ")
    safe_addstr(stdscr, 0, max(1, sw - len(stats) - 2), stats, border_attr)

    if kill_radius > 0 and role != "watch":
        for oy in range(-kill_radius, kill_radius + 1):
            for ox in range(-kill_radius, kill_radius + 1):
                if max(abs(ox), abs(oy)) != kill_radius:
                    continue
                cx, cy = px + ox, py + oy
                if 1 <= cx <= max_x and 1 <= cy <= max_y:
                    safe_addch(stdscr, cy, cx, ".", curses.A_DIM)

    for cx, cy in dead_set:
        safe_addch(stdscr, cy, cx, "X", curses.color_pair(5))
    for cx, cy in ast_set:
        safe_addch(stdscr, cy, cx, "*", curses.color_pair(2))
    battr = (curses.color_pair(6) | curses.A_BOLD
             if is_running and not state["baddies_stopped"]
             else curses.color_pair(5))
    for b in state["baddies"]:
        safe_addch(stdscr, b["pos"][1], b["pos"][0],
                   baddie_glyph(b["dir"][0], b["dir"][1]), battr)

    if not is_running and role != "watch":
        for ox, oy in state["pattern_offsets"]:
            cx, cy = px + ox, py + oy
            if 1 <= cx <= max_x and 1 <= cy <= max_y:
                safe_addch(stdscr, cy, cx, "*", curses.color_pair(1) | curses.A_BOLD)
        foot = f" Shape: {state['pattern_name']} ({state['category']})   L1/R1: osc   L2/R2: static "
    elif role == "watch":
        foot = f" watching {state['name']} -- inputs ignored "
    else:
        foot = " @ is a living cell in the sim "
    safe_addstr(stdscr, sh - 1, 4, foot, curses.color_pair(4) | curses.A_BOLD)

    safe_addch(stdscr, py, px, "@", curses.color_pair(1) | curses.A_BOLD)

    msg = state.get("last_message", "")
    if msg and now - state.get("message_time", 0.0) < 2.8:
        safe_addstr(stdscr, sh - 1, max(1, sw - len(msg) - 3), msg,
                    curses.color_pair(4) | curses.A_BOLD)
    stdscr.refresh()


def play_world(stdscr, inp, conn, state, sw, sh, role):
    """Attached gameplay: inputs -> server, server delta frames -> render."""
    inp.release_sync()
    move_cd = 0.0
    pred_x, pred_y = state["px"], state["py"]  # optimistic @ position
    ast_set = {tuple(p) for p in state["asterisks"]}
    dead_set = {tuple(p) for p in state["dead_cells"]}

    while True:
        inp.pump()
        now = time.time()
        dx, dy = inp.nav()

        # Drain server frames; apply deltas to the cached state
        try:
            for m in conn.poll():
                if m.get("type") != "state":
                    continue
                if m.get("full"):
                    state = dict(m["state"])
                    ast_set = {tuple(p) for p in state["asterisks"]}
                    dead_set = {tuple(p) for p in state["dead_cells"]}
                else:
                    delta = m.get("delta", {})
                    state.update(delta)
                    if "asterisks" in delta:
                        ast_set = {tuple(p) for p in state["asterisks"]}
                    if "dead_cells" in delta:
                        dead_set = {tuple(p) for p in state["dead_cells"]}
                pred_x, pred_y = state["px"], state["py"]
        except (ConnectionError, OSError, ValueError):
            return "lost"

        # In-game menu
        if inp.edge("select") or inp.edge("start") or inp.edge("ps"):
            if role == "watch":
                items = [("resume", "Resume"),
                         ("leave", "Leave world"),
                         ("quit", "Quit client")]
            else:
                items = [("resume", "Resume"),
                         ("gallery", "Gallery of placeables ..."),
                         ("save", "Save game"),
                         ("stop", "Resume baddies" if state["baddies_stopped"]
                          else "Stop baddies"),
                         ("seed", "Seed new life"),
                         ("clear", "Clear board"),
                         ("leave", "Leave world"),
                         ("quit", "Quit client")]
            action = run_menu(stdscr, inp, sw, sh, f"WORLD: {state['name']}", items)
            if action == "leave":
                conn.send({"cmd": "detach"})
                return "menu"
            if action == "quit":
                conn.send({"cmd": "detach"})
                return "quit"
            if action == "gallery":
                chosen = run_gallery_menu(stdscr, inp, sw, sh)
                if chosen is not None:
                    conn.send({"cmd": "command", "do": "set_pattern", "idx": chosen})
            elif action in ("save", "clear", "seed"):
                conn.send({"cmd": "command", "do": action})
            elif action == "stop":
                conn.send({"cmd": "command", "do": "stop_baddies"})
            continue

        if role == "play":
            # Movement: forward + optimistic local prediction
            if (dx or dy) and now - move_cd > 0.08:
                move_cd = now
                pred_x = max(1, min(state["max_x"], pred_x + dx))
                pred_y = max(1, min(state["max_y"], pred_y + dy))
                conn.send({"cmd": "input", "action": "move", "dx": dx, "dy": dy})

            # Action buttons (edge-triggered)
            for name in ACTION_BUTTONS:
                if inp.edge(name):
                    conn.send({"cmd": "input", "action": name})
            if inp.edge("r3"):
                conn.send({"cmd": "command", "do": "stop_baddies"})

        render_state(stdscr, state, ast_set, dead_set, pred_x, pred_y, sw, sh, role)
        time.sleep(0.02)


# -------------------------------------------------------------------- Main

def main(stdscr):
    # Input: PS3 controller if present, keyboard otherwise
    pygame.init()
    pygame.joystick.init()
    have_pad = pygame.joystick.get_count() > 0
    if not have_pad:
        pygame.quit()

    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)
    curses.init_pair(5, curses.COLOR_RED, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)

    stdscr.nodelay(True)
    stdscr.timeout(30)
    sh, sw = stdscr.getmaxyx()

    if have_pad:
        inp = PadInput()
    else:
        inp = KeyInput(stdscr)

    try:
        conn = Conn(SOCKET_PATH)
    except OSError as e:
        stdscr.addstr(0, 0, f"Cannot reach the wander server at {SOCKET_PATH}", curses.A_BOLD)
        stdscr.addstr(2, 0, f"({e})")
        stdscr.addstr(4, 0, "Is the service running?   sudo systemctl status wander")
        stdscr.addstr(5, 0, "Start it with:            sudo systemctl start wander")
        stdscr.refresh()
        time.sleep(6)
        if have_pad:
            pygame.quit()
        return

    while True:
        outcome, payload, role = pick_world(stdscr, inp, conn, sw, sh)
        if outcome == "quit":
            break
        result = play_world(stdscr, inp, conn, payload, sw, sh, role)
        if result == "quit":
            break
        if result == "lost":
            conn.close()
            try:
                conn = Conn(SOCKET_PATH)
            except OSError:
                stdscr.erase()
                stdscr.addstr(0, 0, "Lost the wander server and cannot reconnect.",
                              curses.A_BOLD)
                stdscr.addstr(2, 0, "Check: sudo systemctl status wander")
                stdscr.refresh()
                time.sleep(5)
                break

    conn.close()
    if have_pad:
        pygame.quit()


if __name__ == "__main__":
    curses.wrapper(main)
