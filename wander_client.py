#!/usr/bin/env python3
"""wander_client.py -- PS3 controller client for the wander game server.

Starts at a world menu: open an existing world, create a new one, or
delete one. While attached, controller input is forwarded to the server
and the board is rendered from the server's state frames; the world keeps
evolving (slowly) on the server after you leave.

  python3 wander_client.py
"""
import os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import curses
import json
import socket
import time

import pygame

from wander_game import ALL_PATTERNS, PATTERNS_OSCILLATORS

BASE = os.path.expanduser("~")
SOCKET_PATH = os.environ.get("WANDER_SOCKET_PATH",
                             os.path.join(BASE, "wander_server.sock"))

# PS3 button map (pygame / sixaxis)
BTN_X, BTN_CIRCLE, BTN_TRIANGLE, BTN_SQUARE = 0, 1, 2, 3
BTN_L1, BTN_R1, BTN_L2, BTN_R2 = 4, 5, 6, 7
BTN_SELECT, BTN_START, BTN_PS = 8, 9, 10
BTN_R3 = 12

# Buttons forwarded to the world as actions (edge-triggered)
BUTTON_ACTIONS = {
    BTN_SQUARE: "square",
    BTN_X: "x",
    BTN_CIRCLE: "circle",
    BTN_TRIANGLE: "triangle",
    BTN_L1: "l1",
    BTN_R1: "r1",
    BTN_L2: "l2",
    BTN_R2: "r2",
}


# --------------------------------------------------------------- Networking

class Conn:
    """Newline-delimited JSON connection to the wander server."""

    def __init__(self, path=SOCKET_PATH):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.setblocking(False)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def poll(self):
        """Drain the socket; return a list of complete messages."""
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
        """Send a request and wait for its response (menu phase only --
        no state frames are streaming yet)."""
        self.send(obj)
        end = time.time() + timeout
        while time.time() < end:
            for m in self.poll():
                if "ok" in m:
                    return m
            time.sleep(0.01)
        raise ConnectionError("server timeout")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------ Input helpers

def pressed(controller, btn):
    return btn < controller.get_numbuttons() and controller.get_button(btn)


def read_nav(controller):
    dx = dy = 0
    if controller.get_numhats() > 0:
        hat_x, hat_y = controller.get_hat(0)
        dx, dy = hat_x, -hat_y
    if dx == 0 and dy == 0:
        ax = controller.get_axis(0)
        ay = controller.get_axis(1)
        if ax < -0.5:
            dx = -1
        elif ax > 0.5:
            dx = 1
        if ay < -0.5:
            dy = -1
        elif ay > 0.5:
            dy = 1
    return dx, dy


def wait_for_release(controller, timeout=2.0):
    start = time.time()
    while time.time() - start < timeout:
        pygame.event.pump()
        if not any(controller.get_button(i) for i in range(controller.get_numbuttons())):
            return
        time.sleep(0.02)


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

def run_menu(stdscr, controller, sw, sh, title, items,
             hint="D-Pad: move   X/O: choose   SELECT: back"):
    wait_for_release(controller)
    sel = 0
    move_cd = time.time()

    while True:
        pygame.event.pump()
        now = time.time()

        _, dy = read_nav(controller)
        if dy and now - move_cd > 0.18:
            move_cd = now
            sel = (sel + (1 if dy > 0 else -1)) % len(items)

        if pressed(controller, BTN_X) or pressed(controller, BTN_CIRCLE):
            wait_for_release(controller)
            return items[sel][0]
        if pressed(controller, BTN_SELECT) or pressed(controller, BTN_START):
            wait_for_release(controller)
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


def run_gallery_menu(stdscr, controller, sw, sh):
    """Browse placeable patterns. Returns ALL_PATTERNS index, or None."""
    wait_for_release(controller)
    sel = 0
    move_cd = time.time()
    hint = "D-Pad: browse   X/O: choose shape   SELECT: back"

    while True:
        pygame.event.pump()
        now = time.time()

        dx, dy = read_nav(controller)
        nav = dy or dx
        if nav and now - move_cd > 0.16:
            move_cd = now
            sel = (sel + (1 if nav > 0 else -1)) % len(ALL_PATTERNS)

        if pressed(controller, BTN_X) or pressed(controller, BTN_CIRCLE):
            wait_for_release(controller)
            return sel
        if pressed(controller, BTN_SELECT) or pressed(controller, BTN_START):
            wait_for_release(controller)
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


def pick_world(stdscr, controller, conn, sw, sh):
    """World menu: open / create / delete. Returns ("play", state) or
    ("quit", None)."""
    wait_for_release(controller)
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
                flash, flash_until = f"server error: {e}", now + 3.0
            dirty = False
            refresh_at = now + 2.0

        items = [w["name"] for w in worlds] + ["+ Create new world", "Quit"]
        sel = min(sel, len(items) - 1)

        pygame.event.pump()
        _, dy = read_nav(controller)
        if dy and now - move_cd > 0.18:
            move_cd = now
            sel = (sel + (1 if dy > 0 else -1)) % len(items)

        choice = items[sel]
        if pressed(controller, BTN_X) or pressed(controller, BTN_CIRCLE):
            wait_for_release(controller)
            dirty = True
            if choice == "Quit":
                return ("quit", None)
            try:
                if choice == "+ Create new world":
                    r = conn.rpc({"cmd": "create", "name": next_world_name(worlds),
                                  "max_x": sw - 2, "max_y": sh - 2})
                    if not r.get("ok"):
                        flash, flash_until = f"create failed: {r.get('error')}", now + 3.0
                        continue
                    choice = r["name"]
                r = conn.rpc({"cmd": "attach", "name": choice})
                if r.get("ok"):
                    return ("play", r["state"])
                flash, flash_until = f"attach failed: {r.get('error')}", now + 3.0
            except (ConnectionError, OSError, ValueError) as e:
                flash, flash_until = f"server error: {e}", now + 3.0

        if pressed(controller, BTN_SQUARE) and choice not in ("+ Create new world", "Quit"):
            wait_for_release(controller)
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
        safe_addstr(stdscr, 1, 4, f"server: {SOCKET_PATH}", curses.color_pair(3))

        y0 = 4
        for i, w in enumerate(worlds):
            if y0 + i >= sh - 4:
                break
            flags = "run" if w["running"] else "setup"
            if w.get("attached"):
                flags += " (in use)"
            label = f"{w['name']:<16} *{w['stars']:<4} X{w['xs']:<4} B{w['baddies']:<3} {flags}"
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y0 + i, 6, ("-> " if i == sel else "   ") + label, line_attr)
        for j in range(len(items) - len(worlds)):
            i = len(worlds) + j
            if y0 + i >= sh - 4:
                break
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y0 + i, 6, ("-> " if i == sel else "   ") + items[i], line_attr)

        hint = "D-Pad: move   X/O: open / create   SQUARE x2: delete world"
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


def render_state(stdscr, state, px, py, sw, sh, local_msg="", local_until=0.0):
    max_x, max_y = state["max_x"], state["max_y"]
    is_running = state["is_running"]
    kill_radius = state["kill_radius"]
    now = time.time()

    stdscr.erase()
    border_attr = curses.color_pair(3) if is_running else curses.color_pair(5)
    stdscr.attron(border_attr)
    stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
    stdscr.attroff(border_attr)

    if is_running:
        head = "[ RUN ] □:kill ✕:erase(✕✕:clear) L2/R2:area R3:stop △:setup SEL:menu"
    else:
        head = "[ SETUP ] □:kill/spawn ○:stamp ✕:erase(✕✕:clear) △:run SEL:menu"
    safe_addstr(stdscr, 0, 4, f" {head} ", border_attr | curses.A_BOLD)
    bmark = "s" if state["baddies_stopped"] else ""
    stats = (f" *{len(state['asterisks'])} X{len(state['dead_cells'])}"
             f" B{len(state['baddies'])}{bmark} R{kill_radius} ({px},{py}) ")
    safe_addstr(stdscr, 0, max(1, sw - len(stats) - 2), stats, border_attr)

    # Area-of-effect outline of the SQUARE kill zone around @
    if kill_radius > 0:
        for oy in range(-kill_radius, kill_radius + 1):
            for ox in range(-kill_radius, kill_radius + 1):
                if max(abs(ox), abs(oy)) != kill_radius:
                    continue
                cx, cy = px + ox, py + oy
                if 1 <= cx <= max_x and 1 <= cy <= max_y:
                    safe_addch(stdscr, cy, cx, ".", curses.A_DIM)

    for cx, cy in state["dead_cells"]:
        safe_addch(stdscr, cy, cx, "X", curses.color_pair(5))
    for cx, cy in state["asterisks"]:
        safe_addch(stdscr, cy, cx, "*", curses.color_pair(2))
    battr = (curses.color_pair(6) | curses.A_BOLD
             if is_running and not state["baddies_stopped"]
             else curses.color_pair(5))
    for b in state["baddies"]:
        safe_addch(stdscr, b["pos"][1], b["pos"][0],
                   baddie_glyph(b["dir"][0], b["dir"][1]), battr)

    if not is_running:
        for ox, oy in state["pattern_offsets"]:
            cx, cy = px + ox, py + oy
            if 1 <= cx <= max_x and 1 <= cy <= max_y:
                safe_addch(stdscr, cy, cx, "*", curses.color_pair(1) | curses.A_BOLD)
        foot = f" Shape: {state['pattern_name']} ({state['category']})   L1/R1: osc   L2/R2: static "
    else:
        foot = " @ is a living cell in the sim "
    safe_addstr(stdscr, sh - 1, 4, foot, curses.color_pair(4) | curses.A_BOLD)

    safe_addch(stdscr, py, px, "@", curses.color_pair(1) | curses.A_BOLD)

    msg = local_msg if now < local_until else ""
    s_msg = state.get("last_message", "")
    if s_msg and now - state.get("message_time", 0.0) < 2.8:
        msg = s_msg  # server messages win
    if msg:
        safe_addstr(stdscr, sh - 1, max(1, sw - len(msg) - 3), msg,
                    curses.color_pair(4) | curses.A_BOLD)
    stdscr.refresh()


def play_world(stdscr, controller, conn, state, sw, sh):
    """Attached gameplay: inputs -> server, server frames -> render."""
    wait_for_release(controller)
    move_cd = 0.0
    prev = {btn: False for btn in BUTTON_ACTIONS}
    prev_r3 = False
    pred_x, pred_y = state["px"], state["py"]  # optimistic @ position

    while True:
        pygame.event.pump()
        now = time.time()
        dx, dy = read_nav(controller)

        # Drain server frames (latest state wins)
        try:
            for m in conn.poll():
                if m.get("type") == "state":
                    state = m["state"]
                    pred_x, pred_y = state["px"], state["py"]
        except (ConnectionError, OSError, ValueError):
            return "lost"

        # In-game menu
        if (pressed(controller, BTN_SELECT) or pressed(controller, BTN_START)
                or pressed(controller, BTN_PS)):
            action = run_menu(stdscr, controller, sw, sh,
                              f"WORLD: {state['name']}", [
                                  ("resume", "Resume"),
                                  ("gallery", "Gallery of placeables ..."),
                                  ("save", "Save game"),
                                  ("stop", "Resume baddies" if state["baddies_stopped"]
                                   else "Stop baddies"),
                                  ("seed", "Seed new life"),
                                  ("clear", "Clear board"),
                                  ("leave", "Leave world"),
                                  ("quit", "Quit client"),
                              ])
            if action == "leave":
                conn.send({"cmd": "detach"})
                return "menu"
            if action == "quit":
                conn.send({"cmd": "detach"})
                return "quit"
            elif action == "gallery":
                chosen = run_gallery_menu(stdscr, controller, sw, sh)
                if chosen is not None:
                    conn.send({"cmd": "command", "do": "set_pattern", "idx": chosen})
            elif action in ("save", "clear", "seed"):
                conn.send({"cmd": "command", "do": action})
            elif action == "stop":
                conn.send({"cmd": "command", "do": "stop_baddies"})
            continue

        # Movement: forward + optimistic local prediction
        if (dx or dy) and now - move_cd > 0.08:
            move_cd = now
            pred_x = max(1, min(state["max_x"], pred_x + dx))
            pred_y = max(1, min(state["max_y"], pred_y + dy))
            conn.send({"cmd": "input", "action": "move", "dx": dx, "dy": dy})

        # Action buttons (edge-triggered)
        for btn, act in BUTTON_ACTIONS.items():
            b_now = pressed(controller, btn)
            if b_now and not prev[btn]:
                conn.send({"cmd": "input", "action": act})
            prev[btn] = b_now

        # R3 toggles baddies
        r3_now = pressed(controller, BTN_R3)
        if r3_now and not prev_r3:
            conn.send({"cmd": "command", "do": "stop_baddies"})
        prev_r3 = r3_now

        render_state(stdscr, state, pred_x, pred_y, sw, sh)
        time.sleep(0.02)


# -------------------------------------------------------------------- Main

def main(stdscr):
    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        stdscr.addstr(0, 0, "No PS3 controller detected! Connect controller and try again.")
        stdscr.refresh()
        time.sleep(3)
        return

    controller = pygame.joystick.Joystick(0)
    controller.init()

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

    try:
        conn = Conn(SOCKET_PATH)
    except OSError as e:
        stdscr.addstr(0, 0, f"Cannot reach the wander server at {SOCKET_PATH}", curses.A_BOLD)
        stdscr.addstr(2, 0, f"({e})")
        stdscr.addstr(4, 0, "Is the service running?   sudo systemctl status wander")
        stdscr.addstr(5, 0, "Start it with:            sudo systemctl start wander")
        stdscr.refresh()
        time.sleep(6)
        return

    while True:
        outcome, payload = pick_world(stdscr, controller, conn, sw, sh)
        if outcome == "quit":
            break
        result = play_world(stdscr, controller, conn, payload, sw, sh)
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
    pygame.quit()


if __name__ == "__main__":
    curses.wrapper(main)
