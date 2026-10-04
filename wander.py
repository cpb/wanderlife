#!/usr/bin/env python3
"""wander.py -- Conway's Game of Life playground for PS3 controller + curses.

You are '@':

  *        living cell
  X        dead cell -- for the Game of Life it is empty AND unoccupiable:
           nothing is born there, nothing survives there, and patterns
           cannot be stamped onto it.
  > < ^ v  baddie -- spawned from a dead cell, roams the grid hunting
           living cells and turning them into more dead cells.

Life cycle:
  stand near stars  + SQUARE -> a patch of X (KILL_RADIUS area of effect)
  stand beside X    + SQUARE -> baddie
  stand on baddie   + X      -> back to X
  stand on * or X   + X      -> erased / reclaimed

Baddies are frozen solid in setup mode, and can be stopped at any time
with R3 (or the menu). In run mode they hunt -- and they are radioactive:
each has a half-life of BADDIE_HALF_LIFE seconds, and on decay it is
reborn as a random form from the gallery, stamped where it died. Baddies
that find themselves too close to another baddie end themselves, leaving
an X corpse.

Other tricks:
  double-tap X -> clear the whole board
  L2/R2 in run mode -> shrink / grow the @ area of effect (SQUARE)
  the board is seeded with a mix of oscillating and static gallery forms
"""
import os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import curses
import json
import random
import time
from datetime import datetime

import pygame

SAVE_PATH = os.path.expanduser("~/wander_save.json")

# Gameplay tuning
KILL_RADIUS = 1          # SQUARE kills a (2r+1)x(2r+1) patch of stars around @
MAX_KILL_RADIUS = 5      # cap for runtime growth via L2/R2 in run mode
BADDIE_TICK = 0.35       # seconds between baddie moves (higher = slower)
BADDIE_HALF_LIFE = 20.0  # seconds; decayed baddies become random gallery forms
BADDIE_CROWD_RADIUS = 1  # baddies this close to another baddie self-destruct
DOUBLE_TAP_TIME = 0.4    # seconds; two X taps inside this window clear the board
SEED_FORMS = 7           # forms scattered at startup / on "Seed new life"

# PS3 button map (pygame / sixaxis)
BTN_X, BTN_CIRCLE, BTN_TRIANGLE, BTN_SQUARE = 0, 1, 2, 3
BTN_L1, BTN_R1, BTN_L2, BTN_R2 = 4, 5, 6, 7
BTN_SELECT, BTN_START, BTN_PS = 8, 9, 10
BTN_L3, BTN_R3 = 11, 12

PATTERNS_OSCILLATORS = [
    ("Single Star", [(0, 0)]),
    ("Glider", [(0, 0), (1, 1), (2, -1), (2, 0), (2, 1)]),
    ("Blinker", [(-1, 0), (0, 0), (1, 0)]),
    ("Toad", [(-1, 0), (0, 0), (1, 0), (0, 1), (1, 1), (2, 1)]),
    ("Beacon", [(-1, -1), (0, -1), (-1, 0), (0, 0), (1, 1), (2, 1), (1, 2), (2, 2)])
]

PATTERNS_STATIC = [
    ("Block", [(0, 0), (1, 0), (0, 1), (1, 1)]),
    ("Beehive", [(0, 0), (1, 0), (-1, 1), (2, 1), (0, 2), (1, 2)]),
    ("Loaf", [(0, 0), (1, 0), (-1, 1), (2, 1), (0, 2), (2, 2), (1, 3)]),
    ("Boat", [(0, 0), (1, 0), (0, 1), (2, 1), (1, 2)]),
    ("Tub", [(0, 0), (-1, 1), (1, 1), (0, 2)])
]

ALL_PATTERNS = PATTERNS_OSCILLATORS + PATTERNS_STATIC


# ---------------------------------------------------------------- Game logic

def step_game_of_life(asterisks, max_x, max_y, blocked=frozenset(), player_pos=None):
    """One Conway tick. `blocked` cells are unoccupiable: no births and no
    survivals there. player_pos counts as a living cell for the tick."""
    active_cells = set(asterisks)
    if player_pos:
        active_cells.add(player_pos)

    neighbor_counts = {}
    for (x, y) in active_cells:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 1 <= nx <= max_x and 1 <= ny <= max_y:
                    neighbor_counts[(nx, ny)] = neighbor_counts.get((nx, ny), 0) + 1

    next_asterisks = set()
    for pos in active_cells:
        if neighbor_counts.get(pos, 0) in (2, 3) and pos not in blocked:
            next_asterisks.add(pos)

    for pos, count in neighbor_counts.items():
        if count == 3 and pos not in blocked:
            next_asterisks.add(pos)

    next_asterisks.difference_update(blocked)
    next_asterisks.discard(player_pos)
    return next_asterisks


def kill_area(px, py, radius, asterisks, dead_cells, max_x, max_y):
    """Turn every living cell within Chebyshev distance `radius` of (px, py)
    into a dead cell (X). Returns the number of stars killed."""
    killed = 0
    for y in range(py - radius, py + radius + 1):
        for x in range(px - radius, px + radius + 1):
            if 1 <= x <= max_x and 1 <= y <= max_y and (x, y) in asterisks:
                asterisks.discard((x, y))
                dead_cells.add((x, y))
                killed += 1
    return killed


def seed_board(asterisks, max_x, max_y, rng, count=SEED_FORMS):
    """Scatter a mix of oscillating and static forms at random positions
    (the lonely Single Star is excluded). Forms are placed fully inside
    the board; overlaps are allowed -- they make interesting soups."""
    forms = PATTERNS_OSCILLATORS[1:] + PATTERNS_STATIC
    for _ in range(count):
        _, offsets = forms[rng.randrange(len(forms))]
        minx = min(o[0] for o in offsets)
        maxx = max(o[0] for o in offsets)
        miny = min(o[1] for o in offsets)
        maxy = max(o[1] for o in offsets)
        lo_x, hi_x = 1 - minx, max_x - maxx
        lo_y, hi_y = 1 - miny, max_y - maxy
        if lo_x > hi_x or lo_y > hi_y:
            continue  # board too small for this form
        ax = rng.randint(lo_x, hi_x)
        ay = rng.randint(lo_y, hi_y)
        for ox, oy in offsets:
            asterisks.add((ax + ox, ay + oy))


class Baddie:
    """An arrow that hunts living cells and turns them into dead cells."""
    __slots__ = ("x", "y", "dx", "dy")

    def __init__(self, x, y, dx=1, dy=0):
        self.x, self.y = x, y
        if dx == 0 and dy == 0:
            dx = 1
        self.dx, self.dy = dx, dy

    @property
    def pos(self):
        return (self.x, self.y)

    def glyph(self):
        """Arrow pointing along the current heading."""
        if abs(self.dx) >= abs(self.dy):
            return ">" if self.dx > 0 else "<"
        return "v" if self.dy > 0 else "^"


def update_baddies(baddies, asterisks, dead_cells, max_x, max_y, rng=None,
                   dt=0.2, half_life=20.0, crowd_radius=1):
    """Advance every baddie one tick.

    Crowding: baddies that begin the tick within Chebyshev distance
    `crowd_radius` of another baddie end themselves, leaving an X
    corpse. Resolved simultaneously on the positions at tick entry.

    Radioactive: each tick a surviving baddie decays with probability
    p = 1 - 0.5 ** (dt / half_life), an exponential lifetime with the
    requested half-life. A decaying baddie is reborn as a random form
    from the gallery, stamped where it died (dead cells stay
    unoccupiable; the board edge clips).

    Survivors hunt the nearest living cell; with no prey they wander.
    Any living cell a baddie occupies dies -> X.

    Returns (decayed, crowded):
      decayed = [(x, y, pattern_name), ...]  reborn as gallery forms
      crowded = [(x, y), ...]                self-destructed, left an X
    """
    rng = rng or random
    decay_chance = 1.0 - 0.5 ** (dt / half_life) if half_life > 0 else 1.0
    decayed = []
    crowded = []
    survivors = []
    occupied = {b.pos for b in baddies}

    doomed = set()
    if crowd_radius > 0:
        for i in range(len(baddies)):
            for j in range(i + 1, len(baddies)):
                if (abs(baddies[i].x - baddies[j].x) <= crowd_radius
                        and abs(baddies[i].y - baddies[j].y) <= crowd_radius):
                    doomed.add(i)
                    doomed.add(j)

    for i, b in enumerate(baddies):
        occupied.discard(b.pos)

        # Crowding suicide: too close to another baddie -> X corpse
        if i in doomed:
            dead_cells.add(b.pos)
            crowded.append(b.pos)
            continue

        # Half-life decay: reborn as a random gallery form
        if rng.random() < decay_chance:
            name, offsets = ALL_PATTERNS[rng.randrange(len(ALL_PATTERNS))]
            for ox, oy in offsets:
                t = (b.x + ox, b.y + oy)
                if 1 <= t[0] <= max_x and 1 <= t[1] <= max_y and t not in dead_cells:
                    asterisks.add(t)
            decayed.append((b.x, b.y, name))
            continue
        survivors.append(b)

        # Eat anything that appeared under us (e.g. a birth on our cell)
        if b.pos in asterisks:
            asterisks.discard(b.pos)
            dead_cells.add(b.pos)

        steps = []
        if asterisks:
            tx, ty = min(asterisks, key=lambda s: abs(s[0] - b.x) + abs(s[1] - b.y))
            ox = (tx > b.x) - (tx < b.x)
            oy = (ty > b.y) - (ty < b.y)
            if ox and oy:
                steps.append((ox, oy))
            if abs(tx - b.x) >= abs(ty - b.y):
                if ox:
                    steps.append((ox, 0))
                if oy:
                    steps.append((0, oy))
            else:
                if oy:
                    steps.append((0, oy))
                if ox:
                    steps.append((ox, 0))
            if rng.random() < 0.2:
                rng.shuffle(steps)  # jitter to break gridlock
        if not steps:
            if rng.random() < 0.35:
                steps = [(rng.choice((-1, 0, 1)), rng.choice((-1, 0, 1)))]
            else:
                steps = [(b.dx, b.dy)]

        for sx, sy in steps + [(0, 0)]:
            nx, ny = b.x + sx, b.y + sy
            if not (1 <= nx <= max_x and 1 <= ny <= max_y):
                continue
            if (nx, ny) in occupied:
                continue
            b.x, b.y = nx, ny
            if sx or sy:
                b.dx, b.dy = sx, sy
            break
        occupied.add(b.pos)

        # Eat whatever we landed on
        if b.pos in asterisks:
            asterisks.discard(b.pos)
            dead_cells.add(b.pos)

    baddies[:] = survivors
    return decayed, crowded


def save_game(path, state):
    state["saved_at"] = datetime.now().isoformat(timespec="seconds")
    with open(path, "w") as f:
        json.dump(state, f, indent=1)


def load_game(path):
    with open(path) as f:
        return json.load(f)


# ------------------------------------------------------------ Input helpers

def pressed(controller, btn):
    return btn < controller.get_numbuttons() and controller.get_button(btn)


def read_nav(controller):
    """D-pad (hat) first, left analog as fallback. Returns (dx, dy) in -1..1."""
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
    """Block until every button is up. Prevents the press that opened a
    menu from instantly triggering actions inside it."""
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
    """Generic vertical menu. items = [(key, label), ...].
    Returns the chosen key, or None when cancelled."""
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
        x0 = max(4, sw // 2 - 18)
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

        # Left: pattern list
        for i, (name, _) in enumerate(ALL_PATTERNS):
            y = 3 + i
            if y >= sh - 3:
                break
            cat = "OSC" if i < len(PATTERNS_OSCILLATORS) else "STATIC"
            line_attr = (curses.color_pair(1) | curses.A_BOLD) if i == sel else curses.color_pair(3)
            safe_addstr(stdscr, y, 4, f"{'-> ' if i == sel else '   '}{name:<13} {cat}", line_attr)

        # Right: preview of the selected pattern ('@' marks the stamp anchor)
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
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # @ / preview / selection
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # living cells
    curses.init_pair(3, curses.COLOR_GREEN, -1)    # run border / menu text
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # titles / messages
    curses.init_pair(5, curses.COLOR_RED, -1)      # X dead cells / setup border
    curses.init_pair(6, curses.COLOR_WHITE, -1)    # baddies

    stdscr.nodelay(True)
    stdscr.timeout(30)

    sh, sw = stdscr.getmaxyx()
    max_x, max_y = sw - 2, sh - 2
    rng = random.Random()

    px, py = sw // 2, sh // 2

    asterisks = set()
    seed_board(asterisks, max_x, max_y, rng)
    dead_cells = set()
    baddies = []
    baddies_stopped = False
    kill_radius = KILL_RADIUS

    is_running = False
    pattern_idx = 0
    static_pattern_idx = 0
    active_category = "dynamic"

    move_cd = stamp_cd = cycle_cd = 0.0
    mode_cd = menu_cd = gol_cd = baddie_cd = 0.0
    last_x_tap = 0.0
    prev_x = prev_sq = False
    message, message_until = "", 0.0

    def flash(text, dur=2.2):
        nonlocal message, message_until
        message, message_until = text, time.time() + dur

    while True:
        pygame.event.pump()
        now = time.time()
        dx, dy = read_nav(controller)

        # Edge-triggered buttons (one action per physical press)
        x_now = pressed(controller, BTN_X)
        sq_now = pressed(controller, BTN_SQUARE)
        x_edge = x_now and not prev_x
        sq_edge = sq_now and not prev_sq
        prev_x, prev_sq = x_now, sq_now

        # ---- System menu: SELECT / START / PS ----
        if (pressed(controller, BTN_SELECT) or pressed(controller, BTN_START)
                or pressed(controller, BTN_PS)) and now - menu_cd > 0.3:
            menu_cd = now
            items = [("resume", "Resume"),
                     ("gallery", "Gallery of placeables ..."),
                     ("save", "Save game")]
            if os.path.exists(SAVE_PATH):
                items.append(("load", "Load game"))
            items.append(("freeze",
                          "Resume baddies" if baddies_stopped else "Stop baddies"))
            items += [("seed", "Seed new life"),
                      ("clear", "Clear board"),
                      ("quit", "Quit")]
            action = run_menu(stdscr, controller, sw, sh, "WANDER MENU", items)

            if action == "quit":
                break
            elif action == "gallery":
                chosen = run_gallery_menu(stdscr, controller, sw, sh)
                if chosen is not None:
                    if chosen < len(PATTERNS_OSCILLATORS):
                        active_category, pattern_idx = "dynamic", chosen
                    else:
                        active_category = "static"
                        static_pattern_idx = chosen - len(PATTERNS_OSCILLATORS)
                    is_running = False
                    flash(f"Active shape: {ALL_PATTERNS[chosen][0]}")
            elif action == "save":
                try:
                    save_game(SAVE_PATH, {
                        "version": 1,
                        "player": [px, py],
                        "is_running": is_running,
                        "baddies_stopped": baddies_stopped,
                        "kill_radius": kill_radius,
                        "asterisks": sorted(list(p) for p in asterisks),
                        "dead_cells": sorted(list(p) for p in dead_cells),
                        "baddies": [{"pos": [b.x, b.y], "dir": [b.dx, b.dy]}
                                    for b in baddies],
                        "category": active_category,
                        "pattern_idx": pattern_idx,
                        "static_idx": static_pattern_idx,
                    })
                    flash("Game saved")
                except Exception as e:
                    flash(f"Save failed: {e}", 3.5)
            elif action == "load":
                try:
                    st = load_game(SAVE_PATH)
                    asterisks = {(int(x), int(y)) for x, y in st.get("asterisks", [])}
                    dead_cells = {(int(x), int(y)) for x, y in st.get("dead_cells", [])}
                    asterisks = {p for p in asterisks if 1 <= p[0] <= max_x and 1 <= p[1] <= max_y}
                    dead_cells = {p for p in dead_cells if 1 <= p[0] <= max_x and 1 <= p[1] <= max_y}
                    asterisks -= dead_cells
                    baddies = []
                    for bd in st.get("baddies", []):
                        bx = min(max(int(bd["pos"][0]), 1), max_x)
                        by = min(max(int(bd["pos"][1]), 1), max_y)
                        bdx, bdy = (bd.get("dir") or [1, 0])[:2]
                        baddies.append(Baddie(bx, by, int(bdx), int(bdy)))
                    px = min(max(int(st.get("player", [px, py])[0]), 1), max_x)
                    py = min(max(int(st.get("player", [px, py])[1]), 1), max_y)
                    is_running = bool(st.get("is_running", False))
                    baddies_stopped = bool(st.get("baddies_stopped", False))
                    kill_radius = min(max(int(st.get("kill_radius", KILL_RADIUS)), 0),
                                      MAX_KILL_RADIUS)
                    if st.get("category") in ("dynamic", "static"):
                        active_category = st["category"]
                    pattern_idx = int(st.get("pattern_idx", 0)) % len(PATTERNS_OSCILLATORS)
                    static_pattern_idx = int(st.get("static_idx", 0)) % len(PATTERNS_STATIC)
                    flash(f"Loaded (saved {st.get('saved_at', '?')})")
                except Exception as e:
                    flash(f"Load failed: {e}", 3.5)
            elif action == "freeze":
                baddies_stopped = not baddies_stopped
                flash("Baddies stopped" if baddies_stopped else "Baddies unleashed")
            elif action == "seed":
                seed_board(asterisks, max_x, max_y, rng)
                flash("Seeded new life")
            elif action == "clear":
                asterisks.clear()
                dead_cells.clear()
                baddies.clear()
                flash("Board cleared")

        # ---- Toggle setup / run (TRIANGLE) ----
        if pressed(controller, BTN_TRIANGLE) and now - mode_cd > 0.3:
            mode_cd = now
            is_running = not is_running

        # ---- R3: stop / unleash baddies ----
        if pressed(controller, BTN_R3) and now - mode_cd > 0.3:
            mode_cd = now
            baddies_stopped = not baddies_stopped
            flash("Baddies stopped" if baddies_stopped else "Baddies unleashed")

        # ---- SQUARE: kill stars in the @ area / spawn baddie from a near X ----
        if sq_edge:
            killed = kill_area(px, py, kill_radius, asterisks, dead_cells, max_x, max_y)
            if killed:
                flash(f"Slain {killed} star{'s' if killed > 1 else ''} -> X")
            else:
                sr = max(1, kill_radius)
                near = [(px + ox, py + oy)
                        for oy in range(-sr, sr + 1) for ox in range(-sr, sr + 1)
                        if (px + ox, py + oy) in dead_cells]
                if near:
                    t = min(near, key=lambda c: abs(c[0] - px) + abs(c[1] - py))
                    dead_cells.discard(t)
                    baddies.append(Baddie(t[0], t[1],
                                          rng.choice((-1, 0, 1)), rng.choice((-1, 0, 1))))
                    flash("Baddie spawned! It hunts stars.")
                else:
                    flash("Stand on * to kill it, or beside X to spawn a baddie", 2.8)

        # ---- X button: double-tap clears the board; single tap erases ----
        if x_edge:
            if now - last_x_tap < DOUBLE_TAP_TIME:
                last_x_tap = 0.0
                asterisks.clear()
                dead_cells.clear()
                baddies.clear()
                flash("Board cleared (double-tap X)")
            else:
                last_x_tap = now
                if (px, py) in asterisks:
                    asterisks.discard((px, py))
                    flash("Star erased")
                elif (px, py) in dead_cells:
                    dead_cells.discard((px, py))
                    flash("Cell reclaimed")
                else:
                    hit = next((i for i, b in enumerate(baddies) if b.pos == (px, py)), None)
                    if hit is not None:
                        del baddies[hit]
                        dead_cells.add((px, py))
                        flash("Baddie neutralized -> X")

        # ---- Cycle patterns (setup only) ----
        if not is_running and now - cycle_cd > 0.18:
            if pressed(controller, BTN_L1):
                active_category = "dynamic"
                pattern_idx = (pattern_idx - 1) % len(PATTERNS_OSCILLATORS)
                cycle_cd = now
            elif pressed(controller, BTN_R1):
                active_category = "dynamic"
                pattern_idx = (pattern_idx + 1) % len(PATTERNS_OSCILLATORS)
                cycle_cd = now
            elif pressed(controller, BTN_L2):
                active_category = "static"
                static_pattern_idx = (static_pattern_idx - 1) % len(PATTERNS_STATIC)
                cycle_cd = now
            elif pressed(controller, BTN_R2):
                active_category = "static"
                static_pattern_idx = (static_pattern_idx + 1) % len(PATTERNS_STATIC)
                cycle_cd = now

        # ---- L2/R2 in run mode: shrink / grow the @ area of effect ----
        if is_running and now - cycle_cd > 0.18:
            if pressed(controller, BTN_L2) and kill_radius > 0:
                kill_radius -= 1
                cycle_cd = now
                flash(f"Kill area: {2 * kill_radius + 1}x{2 * kill_radius + 1}")
            elif pressed(controller, BTN_R2) and kill_radius < MAX_KILL_RADIUS:
                kill_radius += 1
                cycle_cd = now
                flash(f"Kill area: {2 * kill_radius + 1}x{2 * kill_radius + 1}")

        if active_category == "dynamic":
            p_name, p_offsets = PATTERNS_OSCILLATORS[pattern_idx]
        else:
            p_name, p_offsets = PATTERNS_STATIC[static_pattern_idx]

        # ---- CIRCLE: stamp pattern (setup only; X cells are unoccupiable) ----
        if not is_running and pressed(controller, BTN_CIRCLE) and now - stamp_cd > 0.2:
            stamp_cd = now
            placed = 0
            for ox, oy in p_offsets:
                t = (px + ox, py + oy)
                if 1 <= t[0] <= max_x and 1 <= t[1] <= max_y and t not in dead_cells:
                    asterisks.add(t)
                    placed += 1
            if placed < len(p_offsets):
                flash("Some cells blocked by X")

        # ---- Movement ----
        if (dx or dy) and now - move_cd > 0.08:
            move_cd = now
            px = max(1, min(max_x, px + dx))
            py = max(1, min(max_y, py + dy))

        # ---- Game of Life tick (run mode; @ is a living cell) ----
        if is_running and now - gol_cd > 0.4:
            gol_cd = now
            asterisks = step_game_of_life(asterisks, max_x, max_y,
                                          blocked=dead_cells, player_pos=(px, py))

        # ---- Baddies tick: frozen in setup mode or when stopped (R3) ----
        if baddies and now - baddie_cd > BADDIE_TICK:
            baddie_cd = now
            if is_running and not baddies_stopped:
                decayed, crowded = update_baddies(baddies, asterisks, dead_cells,
                                                  max_x, max_y, rng,
                                                  dt=BADDIE_TICK,
                                                  half_life=BADDIE_HALF_LIFE,
                                                  crowd_radius=BADDIE_CROWD_RADIUS)
                if len(decayed) == 1:
                    flash(f"Baddie decayed into {decayed[0][2]}")
                elif decayed:
                    flash(f"{len(decayed)} baddies decayed into new life")
                if len(crowded) == 1:
                    flash("Baddie self-destructed (too crowded)")
                elif crowded:
                    flash(f"{len(crowded)} baddies self-destructed (too crowded)")

        # ---- Render ----
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
        bmark = "s" if baddies_stopped else ""
        stats = (f" *{len(asterisks)} X{len(dead_cells)} B{len(baddies)}{bmark}"
                 f" R{kill_radius} ({px},{py}) ")
        safe_addstr(stdscr, 0, max(1, sw - len(stats) - 2), stats, border_attr)

        # Area-of-effect outline: the SQUARE kill zone around @ (drawn first,
        # so stars/X/baddies overwrite it -- dots remain only on empty cells)
        if kill_radius > 0:
            for oy in range(-kill_radius, kill_radius + 1):
                for ox in range(-kill_radius, kill_radius + 1):
                    if max(abs(ox), abs(oy)) != kill_radius:
                        continue  # boundary ring only
                    cx, cy = px + ox, py + oy
                    if 1 <= cx <= max_x and 1 <= cy <= max_y:
                        safe_addch(stdscr, cy, cx, ".", curses.A_DIM)

        for cx, cy in dead_cells:
            safe_addch(stdscr, cy, cx, "X", curses.color_pair(5))
        for cx, cy in asterisks:
            safe_addch(stdscr, cy, cx, "*", curses.color_pair(2))
        baddie_attr = (curses.color_pair(6) | curses.A_BOLD
                       if is_running and not baddies_stopped
                       else curses.color_pair(5))
        for b in baddies:
            safe_addch(stdscr, b.y, b.x, b.glyph(), baddie_attr)

        if not is_running:
            for ox, oy in p_offsets:
                cx, cy = px + ox, py + oy
                if 1 <= cx <= max_x and 1 <= cy <= max_y:
                    safe_addch(stdscr, cy, cx, "*", curses.color_pair(1) | curses.A_BOLD)
            foot = f" Shape: {p_name} ({active_category})   L1/R1: osc   L2/R2: static "
        else:
            foot = " @ is a living cell in the sim "
        safe_addstr(stdscr, sh - 1, 4, foot, curses.color_pair(4) | curses.A_BOLD)

        # @ always drawn on top
        safe_addch(stdscr, py, px, "@", curses.color_pair(1) | curses.A_BOLD)

        if now < message_until:
            safe_addstr(stdscr, sh - 1, max(1, sw - len(message) - 3), message,
                        curses.color_pair(4) | curses.A_BOLD)

        stdscr.refresh()
        time.sleep(0.02)

    pygame.quit()


if __name__ == "__main__":
    curses.wrapper(main)
