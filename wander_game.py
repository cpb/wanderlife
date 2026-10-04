"""wander_game.py -- shared game logic for wander (pure python: no pygame,
no curses). Used by wander.py (standalone game), wander_server.py (headless
game server) and the test suite.

A World is one persistent Game-of-Life board with its own player avatar,
baddies, pattern selection and tuning-driven rules. Worlds tick at full
speed while a client is attached and BACKGROUND_SLOWDOWN times slower
when nobody is watching.
"""
import random
import time
from datetime import datetime

# Gameplay tuning
KILL_RADIUS = 1          # SQUARE kills a (2r+1)x(2r+1) patch of stars around @
MAX_KILL_RADIUS = 5      # cap for runtime growth via L2/R2 in run mode
BADDIE_TICK = 0.35       # seconds between baddie moves (higher = slower)
BADDIE_HALF_LIFE = 20.0  # seconds of GAME time; decayed baddies -> gallery forms
BADDIE_CROWD_RADIUS = 1  # baddies this close to another baddie self-destruct
DOUBLE_TAP_TIME = 0.4    # seconds; two X taps inside this window clear the board
SEED_FORMS = 7           # forms scattered at startup / on "Seed new life"
GOL_TICK = 0.4           # seconds between Game of Life steps when running
BACKGROUND_SLOWDOWN = 20.0  # idle (clientless) worlds tick this many times slower

PROTOCOL_VERSION = 2     # client/server protocol; bumped on breaking changes

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

GLIDER_OFFSETS = dict(ALL_PATTERNS)["Glider"]


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


def baddies_to_gliders(px, py, radius, baddies, asterisks, dead_cells,
                       max_x, max_y):
    """Turn every baddie within Chebyshev distance `radius` of (px, py) into
    a glider stamped where it stood (dead cells stay unoccupiable; the
    board edge clips). Returns the number converted."""
    converted = 0
    survivors = []
    for b in baddies:
        if abs(b.x - px) <= radius and abs(b.y - py) <= radius:
            for ox, oy in GLIDER_OFFSETS:
                t = (b.x + ox, b.y + oy)
                if 1 <= t[0] <= max_x and 1 <= t[1] <= max_y and t not in dead_cells:
                    asterisks.add(t)
            converted += 1
        else:
            survivors.append(b)
    baddies[:] = survivors
    return converted


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


# -------------------------------------------------------------------- World

class World:
    """One persistent game world: board, entities, player, rules and clocks.

    The server ticks worlds at full speed while a client is attached and
    BACKGROUND_SLOWDOWN times slower otherwise. All state is JSON
    serializable via to_dict()/from_dict().
    """

    def __init__(self, name, max_x, max_y, rng=None):
        self.name = str(name)
        self.max_x = int(max_x)
        self.max_y = int(max_y)
        self.rng = rng or random.Random()
        self.asterisks = set()
        self.dead_cells = set()
        self.baddies = []
        self.px, self.py = self.max_x // 2, self.max_y // 2
        self.is_running = False
        self.baddies_stopped = False
        self.kill_radius = KILL_RADIUS
        self.pattern_idx = 0
        self.static_idx = 0
        self.category = "dynamic"
        self.gol_clock = 0.0
        self.baddie_clock = 0.0
        self.last_x_tap = 0.0
        self.last_message = ""
        self.message_time = 0.0
        self.created = datetime.now().isoformat(timespec="seconds")
        self.tick_count = 0
        self.rev = 0  # bumped on every mutation; servers send deltas per rev
        seed_board(self.asterisks, self.max_x, self.max_y, self.rng)

    # ------------------------------------------------------------ helpers
    def _touch(self):
        self.rev += 1

    def flash(self, text):
        self.last_message = text
        self.message_time = time.time()
        self._touch()

    def current_pattern(self):
        if self.category == "dynamic":
            return PATTERNS_OSCILLATORS[self.pattern_idx]
        return PATTERNS_STATIC[self.static_idx]

    # ------------------------------------------------------------ ticking
    def tick(self, dt, active=True):
        """Advance the world clocks by dt wall-clock seconds. Inactive
        (background) worlds run BACKGROUND_SLOWDOWN times slower."""
        scale = 1.0 if active else BACKGROUND_SLOWDOWN
        if self.is_running:
            self.gol_clock += dt
            if self.gol_clock >= GOL_TICK * scale:
                self.gol_clock = 0.0
                self.asterisks = step_game_of_life(
                    self.asterisks, self.max_x, self.max_y,
                    blocked=self.dead_cells, player_pos=(self.px, self.py))
                self._touch()
            if self.baddies and not self.baddies_stopped:
                self.baddie_clock += dt
                if self.baddie_clock >= BADDIE_TICK * scale:
                    self.baddie_clock = 0.0
                    self._touch()
                    decayed, crowded = update_baddies(
                        self.baddies, self.asterisks, self.dead_cells,
                        self.max_x, self.max_y, self.rng, dt=BADDIE_TICK,
                        half_life=BADDIE_HALF_LIFE,
                        crowd_radius=BADDIE_CROWD_RADIUS)
                    if len(decayed) == 1:
                        self.flash(f"Baddie decayed into {decayed[0][2]}")
                    elif decayed:
                        self.flash(f"{len(decayed)} baddies decayed into new life")
                    if len(crowded) == 1:
                        self.flash("Baddie self-destructed (too crowded)")
                    elif crowded:
                        self.flash(f"{len(crowded)} baddies self-destructed"
                                   " (too crowded)")
        self.tick_count += 1

    # ------------------------------------------------------------ actions
    def move(self, dx, dy):
        nx = max(1, min(self.max_x, self.px + int(dx)))
        ny = max(1, min(self.max_y, self.py + int(dy)))
        if (nx, ny) != (self.px, self.py):
            self.px, self.py = nx, ny
            self._touch()

    def toggle_run(self):
        self.is_running = not self.is_running
        self._touch()

    def square(self):
        """Kill stars in the area, convert baddies to gliders, or -- when
        nothing is in range -- spawn a baddie from a nearby X."""
        killed = kill_area(self.px, self.py, self.kill_radius,
                           self.asterisks, self.dead_cells,
                           self.max_x, self.max_y)
        converted = baddies_to_gliders(self.px, self.py, self.kill_radius,
                                       self.baddies, self.asterisks,
                                       self.dead_cells, self.max_x, self.max_y)
        if killed or converted:
            parts = []
            if killed:
                parts.append(f"{killed} star{'s' if killed > 1 else ''} -> X")
            if converted:
                parts.append(f"{converted} baddie{'s' if converted > 1 else ''}"
                             f" -> glider{'s' if converted > 1 else ''}")
            self.flash("; ".join(parts))
            return
        sr = max(1, self.kill_radius)
        near = [(self.px + ox, self.py + oy)
                for oy in range(-sr, sr + 1) for ox in range(-sr, sr + 1)
                if (self.px + ox, self.py + oy) in self.dead_cells]
        if near:
            t = min(near, key=lambda c: abs(c[0] - self.px) + abs(c[1] - self.py))
            self.dead_cells.discard(t)
            self.baddies.append(Baddie(t[0], t[1],
                                       self.rng.choice((-1, 0, 1)),
                                       self.rng.choice((-1, 0, 1))))
            self.flash("Baddie spawned! It hunts stars.")
        else:
            self.flash("Stand on * to kill it, or beside X to spawn a baddie")

    def x_tap(self):
        """Double-tap clears the board; single tap erases/reclaims/neutralizes."""
        now = time.time()
        if now - self.last_x_tap < DOUBLE_TAP_TIME:
            self.last_x_tap = 0.0
            self.asterisks.clear()
            self.dead_cells.clear()
            self.baddies.clear()
            self.flash("Board cleared (double-tap X)")
            return
        self.last_x_tap = now
        p = (self.px, self.py)
        if p in self.asterisks:
            self.asterisks.discard(p)
            self.flash("Star erased")
        elif p in self.dead_cells:
            self.dead_cells.discard(p)
            self.flash("Cell reclaimed")
        else:
            hit = next((i for i, b in enumerate(self.baddies) if b.pos == p), None)
            if hit is not None:
                del self.baddies[hit]
                self.dead_cells.add(p)
                self.flash("Baddie neutralized -> X")

    def toggle_baddies(self):
        self.baddies_stopped = not self.baddies_stopped
        self.flash("Baddies stopped" if self.baddies_stopped
                   else "Baddies unleashed")

    def adjust_radius(self, delta):
        if not self.is_running:
            return
        new = min(max(self.kill_radius + int(delta), 0), MAX_KILL_RADIUS)
        if new != self.kill_radius:
            self.kill_radius = new
            self.flash(f"Kill area: {2 * new + 1}x{2 * new + 1}")

    def cycle(self, category, direction):
        if self.is_running:
            return
        self.category = category
        if category == "dynamic":
            self.pattern_idx = (self.pattern_idx + direction) % len(PATTERNS_OSCILLATORS)
        else:
            self.static_idx = (self.static_idx + direction) % len(PATTERNS_STATIC)

    def set_pattern(self, idx):
        idx = int(idx)
        if 0 <= idx < len(PATTERNS_OSCILLATORS):
            self.category, self.pattern_idx = "dynamic", idx
        elif len(PATTERNS_OSCILLATORS) <= idx < len(ALL_PATTERNS):
            self.category = "static"
            self.static_idx = idx - len(PATTERNS_OSCILLATORS)
        else:
            return
        self.is_running = False
        self.flash(f"Active shape: {ALL_PATTERNS[idx][0]}")

    def stamp(self):
        if self.is_running:
            return
        _, offsets = self.current_pattern()
        placed = 0
        for ox, oy in offsets:
            t = (self.px + ox, self.py + oy)
            if (1 <= t[0] <= self.max_x and 1 <= t[1] <= self.max_y
                    and t not in self.dead_cells):
                self.asterisks.add(t)
                placed += 1
        if placed < len(offsets):
            self.flash("Some cells blocked by X")

    def seed(self):
        seed_board(self.asterisks, self.max_x, self.max_y, self.rng)
        self.flash("Seeded new life")

    def clear(self):
        self.asterisks.clear()
        self.dead_cells.clear()
        self.baddies.clear()
        self.flash("Board cleared")

    def button(self, name):
        """Route a controller button by name (mode-aware)."""
        if name == "square":
            self.square()
        elif name == "x":
            self.x_tap()
        elif name == "circle":
            self.stamp()
        elif name == "triangle":
            self.toggle_run()
        elif name == "l1":
            self.cycle("dynamic", -1)
        elif name == "r1":
            self.cycle("dynamic", 1)
        elif name == "l2":
            if self.is_running:
                self.adjust_radius(-1)
            else:
                self.cycle("static", -1)
        elif name == "r2":
            if self.is_running:
                self.adjust_radius(1)
            else:
                self.cycle("static", 1)

    # ------------------------------------------------------ serialization
    def info(self):
        return {
            "name": self.name,
            "stars": len(self.asterisks),
            "xs": len(self.dead_cells),
            "baddies": len(self.baddies),
            "running": self.is_running,
            "created": self.created,
            "ticks": self.tick_count,
        }

    def client_state(self):
        """Everything a client needs to render one frame (JSON-safe)."""
        name, offsets = self.current_pattern()
        return {
            "name": self.name,
            "max_x": self.max_x,
            "max_y": self.max_y,
            "px": self.px,
            "py": self.py,
            "asterisks": sorted(list(p) for p in self.asterisks),
            "dead_cells": sorted(list(p) for p in self.dead_cells),
            "baddies": [{"pos": [b.x, b.y], "dir": [b.dx, b.dy]}
                        for b in self.baddies],
            "is_running": self.is_running,
            "baddies_stopped": self.baddies_stopped,
            "kill_radius": self.kill_radius,
            "category": self.category,
            "pattern_name": name,
            "pattern_offsets": offsets,
            "last_message": self.last_message,
            "message_time": self.message_time,
        }

    def to_dict(self):
        d = self.client_state()
        d.update({
            "version": 1,
            "created": self.created,
            "tick_count": self.tick_count,
            "pattern_idx": self.pattern_idx,
            "static_idx": self.static_idx,
        })
        return d

    @classmethod
    def from_dict(cls, st):
        w = cls.__new__(cls)
        w.rng = random.Random()
        w.name = str(st.get("name", "world"))
        w.max_x = int(st.get("max_x", 78))
        w.max_y = int(st.get("max_y", 22))
        w.asterisks = {(int(x), int(y)) for x, y in st.get("asterisks", [])}
        w.dead_cells = {(int(x), int(y)) for x, y in st.get("dead_cells", [])}
        w.asterisks = {p for p in w.asterisks
                       if 1 <= p[0] <= w.max_x and 1 <= p[1] <= w.max_y}
        w.dead_cells = {p for p in w.dead_cells
                        if 1 <= p[0] <= w.max_x and 1 <= p[1] <= w.max_y}
        w.asterisks -= w.dead_cells
        w.baddies = []
        for bd in st.get("baddies", []):
            bx = min(max(int(bd["pos"][0]), 1), w.max_x)
            by = min(max(int(bd["pos"][1]), 1), w.max_y)
            bdx, bdy = (bd.get("dir") or [1, 0])[:2]
            w.baddies.append(Baddie(bx, by, int(bdx), int(bdy)))
        w.px = min(max(int(st.get("px", w.max_x // 2)), 1), w.max_x)
        w.py = min(max(int(st.get("py", w.max_y // 2)), 1), w.max_y)
        w.is_running = bool(st.get("is_running", False))
        w.baddies_stopped = bool(st.get("baddies_stopped", False))
        w.kill_radius = min(max(int(st.get("kill_radius", KILL_RADIUS)), 0),
                            MAX_KILL_RADIUS)
        w.category = st.get("category") if st.get("category") in ("dynamic", "static") \
            else "dynamic"
        w.pattern_idx = int(st.get("pattern_idx", 0)) % len(PATTERNS_OSCILLATORS)
        w.static_idx = int(st.get("static_idx", 0)) % len(PATTERNS_STATIC)
        w.gol_clock = 0.0
        w.baddie_clock = 0.0
        w.last_x_tap = 0.0
        w.last_message = str(st.get("last_message", ""))
        w.message_time = 0.0
        w.created = str(st.get("created",
                               datetime.now().isoformat(timespec="seconds")))
        w.tick_count = int(st.get("tick_count", 0))
        w.rev = 0
        return w
