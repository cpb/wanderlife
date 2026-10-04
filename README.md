# wander

Conway's Game of Life playground for a PS3 controller, built with pygame
(controller input) + curses (terminal rendering). Runs on a Raspberry Pi.

## Run

```sh
python3 wander.py        # on the Pi, with the PS3 controller paired
```

## You are `@`

| Glyph     | Meaning |
|-----------|---------|
| `*`       | living cell |
| `X`       | dead cell — **empty and unoccupiable** for the Game of Life: nothing is born there, nothing survives there, and patterns cannot be stamped onto it |
| `> < ^ v` | **baddie** — spawned from a dead cell; in run mode it roams the grid hunting living cells and turning them into more dead cells |

### The life cycle

1. Stand near stars and press **SQUARE** → a `(2·KILL_RADIUS+1)²` patch of
   stars dies into `X`s (default 3×3).
2. Stand **beside an `X`** and press **SQUARE** → the `X` rises as a baddie.
3. Baddies are **frozen in setup mode** — they neither move nor decay until
   the sim is running.
4. Baddies are **radioactive**: each has a half-life of `BADDIE_HALF_LIFE`
   seconds (default 20). On decay a baddie is reborn as a **random form from
   the gallery**, stamped where it died — killers return to life.
5. Stand on a baddie and press **X** → it drops back to a harmless `X`.
6. Stand on a `*` or `X` and press **X** → erase / reclaim the cell.

Tuning knobs live at the top of `wander.py`: `KILL_RADIUS`, `BADDIE_TICK`,
`BADDIE_HALF_LIFE`.

## Controls

| Button            | Action |
|-------------------|--------|
| D-Pad / left stick | Move `@` · navigate menus |
| □ SQUARE          | Kill stars in a patch around `@` → `X`s · beside `X`: spawn baddie |
| ✕ CROSS           | Erase `*` · reclaim `X` · neutralize baddie → `X` |
| ○ CIRCLE          | Stamp the active pattern (setup mode) |
| △ TRIANGLE        | Toggle setup ↔ run mode |
| L1 / R1           | Cycle oscillator patterns (setup) |
| L2 / R2           | Cycle static patterns (setup) |
| SELECT or START   | Open the **menu**: Resume · Gallery · Save game · Load game · Clear board · Quit |

In run mode the Game of Life ticks and `@` participates as a living cell.

Save files are written to `~/wander_save.json` on the Pi.

## Development

```sh
python3 test_wander.py   # logic tests (no pygame needed, it is stubbed)
```

## Deployment

The deploy target is the Pi at `192.168.8.195`; the game lives in the home
directory there. Deployment is a plain git push:

```sh
git push deploy main
```

How it works:

- The Pi has **no system git** (and no passwordless sudo), so a rootless git
  is unpacked from the Raspbian `.deb` into `~/opt/git` (`apt-get download git`
  + `dpkg -x`). `~/opt/git/receive-pack-wrapper.sh` sets `PATH`/`GIT_EXEC_PATH`
  and is what the local repo invokes over SSH
  (`git config remote.deploy.receivepack /home/cpb/opt/git/receive-pack-wrapper.sh`).
- `~/wander.git` is a bare repo; its `hooks/post-receive` checks out `main`
  into `$HOME` (`git --work-tree=$HOME checkout -f main`) on every push.
- The pre-git `wander.py` was backed up to `~/wander.py.bak.*` before the
  first deploy.
