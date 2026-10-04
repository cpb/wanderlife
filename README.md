# wander

Conway's Game of Life playground for a PS3 controller, built with pygame
(controller input) + curses (terminal rendering). Runs on a Raspberry Pi.

## Pieces

| File | Role |
|------|------|
| `wander_game.py` | shared game logic + `World` (pure python: no pygame, no curses) |
| `wander_server.py` | headless game server: hosts persistent worlds over a UNIX socket, systemd-managed |
| `wander_client.py` | PS3+curses client: opens at a world menu, plays attached worlds |
| `wander.py` | standalone single-player build (no server needed) |
| `wander.service` | systemd unit for the server |

## Run (server mode)

The server runs in the background via systemd:

```sh
sudo systemctl status wander     # is it up?
sudo systemctl restart wander    # pick up a fresh deploy
journalctl -u wander -f          # watch its log
```

Play with the client (PS3 controller paired, or keyboard):

```sh
python3 wander_client.py
```

The client opens at the **world menu**: play an existing world (`X`),
watch one (`TRIANGLE` / `w`), create a new one, or delete one (**SQUARE
twice**). **Multiple clients can attach to the same world** — `play`
clients share the one `@` (chaos is a feature), `watch` clients are
read-only. Worlds live in `~/wander_worlds/*.json`, are saved (dirty-only,
on a background thread) every 30 s and on detach/shutdown, and **keep
evolving in the background at 1/20 speed** (`BACKGROUND_SLOWDOWN`) while
no client is attached — full speed while anyone is playing.

Keyboard controls (when no controller is present): arrows/hjkl/wasd move,
`SPACE`=SQUARE, `x`=X, `o`=CIRCLE, `t`=TRIANGLE, `[`/`]`=L1/R1,
`-`/`=`=L2/R2, `b`=R3, `ESC`/`m`=menu, `ENTER`=confirm.

## Run (standalone)

```sh
python3 wander.py        # classic single-player, saves to ~/wander_save.json
```

## You are `@`

| Glyph     | Meaning |
|-----------|---------|
| `*`       | living cell |
| `X`       | dead cell — **empty and unoccupiable** for the Game of Life: nothing is born there, nothing survives there, and patterns cannot be stamped onto it |
| `> < ^ v` | **baddie** — spawned from a dead cell; in run mode it roams the grid hunting living cells and turning them into more dead cells |

The board is seeded at startup with a mix of oscillating and static gallery
forms (no lonely single stars); "Seed new life" in the menu adds more.

### The life cycle

1. Stand near stars and press **SQUARE** → a `(2r+1)²` patch of stars dies
   into `X`s. The radius `r` starts at `KILL_RADIUS` (default 1 → 3×3) and in
   run mode you can **grow/shrink it live with R2/L2** (0 → 1×1, up to
   `MAX_KILL_RADIUS` → 11×11). The kill zone is drawn as a dotted outline
   around `@`, and shown in the HUD as `R<n>`.
   **Baddies caught in the blast are turned into gliders**, stamped where
   they stood — killers return to life, ready to fly.
2. Stand **beside an `X`** and press **SQUARE** → the `X` rises as a baddie.
3. Baddies are **frozen in setup mode**, and can be stopped at any time with
   **R3** (or the menu) — frozen baddies draw red and neither move nor decay.
4. Baddies are **radioactive**: each has a half-life of `BADDIE_HALF_LIFE`
   seconds (default 20). On decay a baddie is reborn as a **random form from
   the gallery**, stamped where it died — killers return to life.
5. Baddies that find themselves **too close to another baddie** (within
   `BADDIE_CROWD_RADIUS`, default: adjacent) end themselves, leaving an `X`
   corpse. Keep your herd spread out.
6. Stand on a baddie and press **X** → it drops back to a harmless `X`.
7. Stand on a `*` or `X` and press **X** → erase / reclaim the cell.
8. **Double-tap X** → clear the whole board.

Tuning knobs live at the top of `wander.py`: `KILL_RADIUS`, `MAX_KILL_RADIUS`,
`BADDIE_TICK`, `BADDIE_HALF_LIFE`, `BADDIE_CROWD_RADIUS`, `DOUBLE_TAP_TIME`,
`SEED_FORMS`.

## Controls

| Button            | Action |
|-------------------|--------|
| D-Pad / left stick | Move `@` · navigate menus |
| □ SQUARE          | Kill stars in the patch around `@` → `X`s · baddies in the patch → gliders · beside `X`: spawn baddie |
| ✕ CROSS           | Erase `*` · reclaim `X` · neutralize baddie → `X` · **double-tap: clear board** |
| ○ CIRCLE          | Stamp the active pattern (setup mode) |
| △ TRIANGLE        | Toggle setup ↔ run mode |
| L1 / R1           | Cycle oscillator patterns (setup) |
| L2 / R2           | Setup: cycle static patterns · **Run: shrink / grow @'s area of effect** |
| R3 (right stick)  | Stop / unleash the baddies |
| SELECT or START   | Open the **menu**: Resume · Gallery · Save game · Load game · Stop/Resume baddies · Seed new life · Clear board · Quit |

In run mode the Game of Life ticks and `@` participates as a living cell.

Save files are written to `~/wander_save.json` on the Pi.

## Development

```sh
python3 test_wander.py          # game-logic tests (pygame stubbed)
python3 test_wander_server.py   # World + server protocol tests (pure python)
```

### Client/server protocol (v2)

Newline-delimited JSON over the UNIX socket `~/wander_server.sock`.
Every message carries the protocol version (`"v": 2`); mismatches are
rejected. Client: `list` / `create` / `delete` / `attach` (with
`role: play|watch`) / `detach` / `input` (move + button actions) /
`command` (save, clear, seed, stop_baddies, set_pattern). Server replies
`{"ok": ...}` and streams frames at 10 Hz: the first frame is full
state, later frames are **deltas** containing only changed fields
(nothing is sent while the world is unchanged; a 5 s heartbeat proves
liveness).

## Deployment

The deploy target is the Pi at `192.168.8.195`; the game lives in the home
directory there. Deployment is a plain git push:

```sh
git push deploy main
```

How it works:

- The Pi has **no system git** (and originally no passwordless sudo), so a
  rootless git is unpacked from the Raspbian `.deb` into `~/opt/git`
  (`apt-get download git` + `dpkg -x`).
  `~/opt/git/receive-pack-wrapper.sh` sets `PATH`/`GIT_EXEC_PATH` and is
  what the local repo invokes over SSH
  (`git config remote.deploy.receivepack /home/cpb/opt/git/receive-pack-wrapper.sh`).
- `~/wander.git` is a bare repo; its `hooks/post-receive` (versioned in
  this repo as `deploy/post-receive`) checks out `main` into `$HOME` on
  every push and **restarts the wander service automatically when game
  code changed**, via `sudo -n systemctl restart wander`.
- `/etc/sudoers.d/wander` (versioned as `deploy/sudoers-wander`) grants
  `cpb` NOPASSWD rights to *exactly* `systemctl restart wander` — nothing
  else, and the password is not stored anywhere.
- The pre-git `wander.py` was backed up to `~/wander.py.bak.*` before the
  first deploy.
