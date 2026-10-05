"""wander_game.py -- shared game logic for wander (pure python: no pygame,
no curses). Used by wander.py (standalone game), wander_server.py (headless
game server) and the test suite.

A World is one persistent Game-of-Life board plus any number of transient
Players (one per attached play-client; they spawn on attach and despawn on
detach). Worlds tick at full speed while a client is attached and
BACKGROUND_SLOWDOWN times slower when nobody is watching.
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

PROTOCOL_VERSION = 3     # client/server protocol; bumped on breaking changes

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

def step_game_of_life(asterisks, max_x, max_y, blocked=frozenset(),
                      player_positions=frozenset()):
    """One Conway tick. `blocked` cells are unoccupiable: no births and no
    survivals there. Every cell in `player_positions` counts as a living
    cell for the tick (avatars never persist as stars themselves)."""
    active_cells = set(asterisks) | set(player_positions)

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
    next_asterisks.difference_update(player_positions)
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


# ------------------------------------------------------------------- Player

class Player:
    """One '@': a transient avatar spawned per attached play-client.
    Position, kill radius and pattern selection are per-player."""
    __slots__ = ("pid", "x", "y", "kill_radius", "category",
                 "pattern_idx", "static_idx", "last_x_tap")

    def __init__(self, pid, x, y):
        self.pid = pid
        self.x, self.y = x, y
        self.kill_radius = KILL_RADIUS
        self.category = "dynamic"
        self.pattern_idx = 0
        self.static_idx = 0
        self.last_x_tap = 0.0

    @property
    def pos(self):
        return (self.x, self.y)


# -------------------------------------------------------------------- World

class World:
    """One persistent game world: board, entities, players, rules, clocks.

    The server ticks worlds at full speed while a client is attached and
    BACKGROUND_SLOWDOWN times slower otherwise. Persistent state is JSON
    serializable via to_dict()/from_dict(); players are transient and are
    NOT persisted (they belong to connections, not to the world file).
    """

    def __init__(self, name, max_x, max_y, rng=None):
        self.name = str(name)
        self.max_x = int(max_x)
        self.max_y = int(max_y)
        self.rng = rng or random.Random()
        self.asterisks = set()
        self.dead_cells = set()
        self.baddies = []
        self.players = {}          # pid -> Player (transient)
        self.next_pid = 0
        self.is_running = False
        self.baddies_stopped = False
        self.gol_clock = 0.0
        self.baddie_clock = 0.0
        self.last_message = ""
        self.message_time = 0.0
        self.created = datetime.now().isoformat(timespec="seconds")
        self.tick_count = 0
        self.rev = 0  # bumped on every mutation; servers send deltas per rev
        seed_board(self.asterisks, self.max_x, self.max_y, self.rng)

    # ------------------------------------------------------------ helpers
    def _touch(self):
        self.rev += 1

    def _prefix(self, pid):
        return f"@p{pid}: " if pid is not None else ""

    def flash(self, text):
        self.last_message = text
        self.message_time = time.time()
        self._touch()

    def current_pattern(self, pid):
        p = self.players[pid]
        if p.category == "dynamic":
            return PATTERNS_OSCILLATORS[p.pattern_idx]
        return PATTERNS_STATIC[p.static_idx]

    # ------------------------------------------------------------ players
    def add_player(self):
        """Spawn a new player at a random free-ish spot. Returns its pid."""
        self.next_pid += 1
        pid = self.next_pid
        taken = {p.pos for p in self.players.values()}
        x, y = self.max_x // 2, self.max_y // 2
        for _ in range(20):
            cx = self.rng.randint(max(2, self.max_x // 6),
                                  max(2, self.max_x - self.max_x // 6))
            cy = self.rng.randint(max(2, self.max_y // 6),
                                  max(2, self.max_y - self.max_y // 6))
            if (cx, cy) not in taken:
                x, y = cx, cy
                break
        self.players[pid] = Player(pid, x, y)
        self.flash(f"@p{pid} joined")
        return pid

    def remove_player(self, pid):
        if pid in self.players:
            del self.players[pid]
            self.flash(f"@p{pid} left")

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
                    blocked=self.dead_cells,
                    player_positions={p.pos for p in self.players.values()})
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

    # ------------------------------------------------------ player actions
    def move(self, pid, dx, dy):
        p = self.players.get(pid)
        if p is None:
            return
        nx = max(1, min(self.max_x, p.x + int(dx)))
        ny = max(1, min(self.max_y, p.y + int(dy)))
        if (nx, ny) != p.pos:
            p.x, p.y = nx, ny
            self._touch()

    def square(self, pid):
        """Kill stars in the player's area, convert baddies to gliders, or
        -- when nothing is in range -- spawn a baddie from a nearby X."""
        p = self.players.get(pid)
        if p is None:
            return
        killed = kill_area(p.x, p.y, p.kill_radius,
                           self.asterisks, self.dead_cells,
                           self.max_x, self.max_y)
        converted = baddies_to_gliders(p.x, p.y, p.kill_radius,
                                       self.baddies, self.asterisks,
                                       self.dead_cells, self.max_x, self.max_y)
        pre = self._prefix(pid)
        if killed or converted:
            parts = []
            if killed:
                parts.append(f"{killed} star{'s' if killed > 1 else ''} -> X")
            if converted:
                parts.append(f"{converted} baddie{'s' if converted > 1 else ''}"
                             f" -> glider{'s' if converted > 1 else ''}")
            self.flash(pre + "; ".join(parts))
            return
        sr = max(1, p.kill_radius)
        near = [(p.x + ox, p.y + oy)
                for oy in range(-sr, sr + 1) for ox in range(-sr, sr + 1)
                if (p.x + ox, p.y + oy) in self.dead_cells]
        if near:
            t = min(near, key=lambda c: abs(c[0] - p.x) + abs(c[1] - p.y))
            self.dead_cells.discard(t)
            self.baddies.append(Baddie(t[0], t[1],
                                       self.rng.choice((-1, 0, 1)),
                                       self.rng.choice((-1, 0, 1))))
            self.flash(pre + "Baddie spawned! It hunts stars.")
        else:
            self.flash(pre + "Stand on * to kill it, or beside X to spawn a baddie")

    def x_tap(self, pid):
        """Double-tap clears the board; single tap erases/reclaims/neutralizes."""
        p = self.players.get(pid)
        if p is None:
            return
        pre = self._prefix(pid)
        now = time.time()
        if now - p.last_x_tap < DOUBLE_TAP_TIME:
            p.last_x_tap = 0.0
            self.asterisks.clear()
            self.dead_cells.clear()
            self.baddies.clear()
            self.flash(pre + "Board cleared (double-tap X)")
            return
        p.last_x_tap = now
        if p.pos in self.asterisks:
            self.asterisks.discard(p.pos)
            self.flash(pre + "Star erased")
        elif p.pos in self.dead_cells:
            self.dead_cells.discard(p.pos)
            self.flash(pre + "Cell reclaimed")
        else:
            hit = next((i for i, b in enumerate(self.baddies) if b.pos == p.pos),
                       None)
            if hit is not None:
                del self.baddies[hit]
                self.dead_cells.add(p.pos)
                self.flash(pre + "Baddie neutralized -> X")

    def cycle(self, pid, category, direction):
        if self.is_running:
            return
        p = self.players.get(pid)
        if p is None:
            return
        p.category = category
        if category == "dynamic":
            p.pattern_idx = (p.pattern_idx + direction) % len(PATTERNS_OSCILLATORS)
        else:
            p.static_idx = (p.static_idx + direction) % len(PATTERNS_STATIC)
        self._touch()

    def set_pattern(self, pid, idx):
        p = self.players.get(pid)
        if p is None:
            return
        idx = int(idx)
        if 0 <= idx < len(PATTERNS_OSCILLATORS):
            p.category, p.pattern_idx = "dynamic", idx
        elif len(PATTERNS_OSCILLATORS) <= idx < len(ALL_PATTERNS):
            p.category = "static"
            p.static_idx = idx - len(PATTERNS_OSCILLATORS)
        else:
            return
        self.is_running = False
        self.flash(self._prefix(pid) + f"Active shape: {ALL_PATTERNS[idx][0]}")

    def stamp(self, pid):
        if self.is_running:
            return
        p = self.players.get(pid)
        if p is None:
            return
        _, offsets = self.current_pattern(pid)
        placed = 0
        for ox, oy in offsets:
            t = (p.x + ox, p.y + oy)
            if (1 <= t[0] <= self.max_x and 1 <= t[1] <= self.max_y
                    and t not in self.dead_cells):
                self.asterisks.add(t)
                placed += 1
        self._touch()
        if placed < len(offsets):
            self.flash(self._prefix(pid) + "Some cells blocked by X")

    def adjust_radius(self, pid, delta):
        if not self.is_running:
            return
        p = self.players.get(pid)
        if p is None:
            return
        new = min(max(p.kill_radius + int(delta), 0), MAX_KILL_RADIUS)
        if new != p.kill_radius:
            p.kill_radius = new
            self.flash(self._prefix(pid) + f"Kill area: {2 * new + 1}x{2 * new + 1}")

    # ------------------------------------------------------- world actions
    def toggle_run(self, pid=None):
        self.is_running = not self.is_running
        self._touch()

    def toggle_baddies(self, pid=None):
        self.baddies_stopped = not self.baddies_stopped
        self.flash(self._prefix(pid) + ("Baddies stopped" if self.baddies_stopped
                                        else "Baddies unleashed"))

    def seed(self, pid=None):
        seed_board(self.asterisks, self.max_x, self.max_y, self.rng)
        self.flash(self._prefix(pid) + "Seeded new life")

    def clear(self, pid=None):
        self.asterisks.clear()
        self.dead_cells.clear()
        self.baddies.clear()
        self.flash(self._prefix(pid) + "Board cleared")

    def button(self, pid, name):
        """Route a controller button by name for a player (mode-aware)."""
        if name == "square":
            self.square(pid)
        elif name == "x":
            self.x_tap(pid)
        elif name == "circle":
            self.stamp(pid)
        elif name == "triangle":
            self.toggle_run(pid)
        elif name == "l1":
            self.cycle(pid, "dynamic", -1)
        elif name == "r1":
            self.cycle(pid, "dynamic", 1)
        elif name == "l2":
            if self.is_running:
                self.adjust_radius(pid, -1)
            else:
                self.cycle(pid, "static", -1)
        elif name == "r2":
            if self.is_running:
                self.adjust_radius(pid, 1)
            else:
                self.cycle(pid, "static", 1)

    # ------------------------------------------------------ serialization
    def info(self):
        return {
            "name": self.name,
            "stars": len(self.asterisks),
            "xs": len(self.dead_cells),
            "baddies": len(self.baddies),
            "players": len(self.players),
            "running": self.is_running,
            "created": self.created,
            "ticks": self.tick_count,
        }

    def client_state(self):
        """Everything a client needs to render one frame (JSON-safe)."""
        players = []
        for p in self.players.values():
            pname, poffsets = (PATTERNS_OSCILLATORS[p.pattern_idx]
                               if p.category == "dynamic"
                               else PATTERNS_STATIC[p.static_idx])
            players.append({
                "pid": p.pid,
                "pos": [p.x, p.y],
                "kill_radius": p.kill_radius,
                "category": p.category,
                "pattern_name": pname,
                "pattern_offsets": poffsets,
            })
        return {
            "name": self.name,
            "max_x": self.max_x,
            "max_y": self.max_y,
            "asterisks": sorted(list(p) for p in self.asterisks),
            "dead_cells": sorted(list(p) for p in self.dead_cells),
            "baddies": [{"pos": [b.x, b.y], "dir": [b.dx, b.dy]}
                        for b in self.baddies],
            "players": players,
            "is_running": self.is_running,
            "baddies_stopped": self.baddies_stopped,
            "last_message": self.last_message,
            "message_time": self.message_time,
        }

    def to_dict(self):
        """Persistent state only -- players are transient and excluded."""
        d = self.client_state()
        del d["players"]
        d.update({
            "version": 1,
            "created": self.created,
            "tick_count": self.tick_count,
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
        w.players = {}
        w.next_pid = 0
        w.is_running = bool(st.get("is_running", False))
        w.baddies_stopped = bool(st.get("baddies_stopped", False))
        w.gol_clock = 0.0
        w.baddie_clock = 0.0
        w.last_message = str(st.get("last_message", ""))
        w.message_time = 0.0
        w.created = str(st.get("created",
                               datetime.now().isoformat(timespec="seconds")))
        w.tick_count = int(st.get("tick_count", 0))
        w.rev = 0
        return w
