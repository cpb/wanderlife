# wanderlife

Conway's Game of Life playground for a PS3 controller, built with pygame
(controller input) + curses (terminal rendering). Runs on a Raspberry Pi.

![Wanderlife demo](demo.gif)

## Pieces

| File | Role |
|------|------|
| `wander_game.py` | shared game logic + `World` (pure python: no pygame, no curses) |
| `wander_server.py` | headless game server: hosts persistent worlds over a UNIX socket |
| `wander_client.py` | curses client: PS3 controller, keyboard and touchpad/mouse all work; opens at a world menu, plays attached worlds |
| `wander.py` | standalone single-player build (no server needed; requires pygame) |
| `wander_bot.py` | LLM-driven player (Kimi K3 via Baseten, Ollama, ...) — attaches like any client |
| `wanderctl` | dev-machine launcher: start the server if needed and play, stop, optional launchd autostart |
| `deploy/` | push-deploy provisioning: `setup-host.sh`, systemd/sudoers/launchd templates, `post-receive` hook |

## Install & run (macOS / Linux)

Requires Python 3 -- no dependencies beyond the standard library
(pygame is optional; you only need it for a PS3 controller).

```sh
git clone https://github.com/cpb/wanderlife.git
cd wanderlife
./wanderctl play
```

`wanderctl play` starts the server in the background if it isn't running,
then launches the client (just the client if the server is already up):

No pygame needed here -- the client plays with keyboard + touchpad:
**left-click a cell to walk there, right-click to kill (SQUARE),
double-click to erase (X)**, and click menu rows to select/choose.

```sh
./wanderctl stop               # shut the server down (worlds are saved)
./wanderctl status             # what's running?
./wanderctl install-service    # optional: launchd, server starts at login
./wanderctl uninstall-service  # remove that again
```

## Run (server mode, on the Pi)

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
twice**). **Multiple clients can attach to the same world, and every
`play` client spawns its own `@`** — your avatar is cyan, everyone else's
is green; kill radius, pattern selection and movement are all per-player.
Avatars spawn on attach and despawn on detach (they're never persisted);
`watch` clients are read-only and get no avatar. Action flashes are
prefixed `@pN:` so you can tell who did what. Worlds live in
`~/wander_worlds/*.json`, are saved (dirty-only, on a background thread)
every 30 s and on detach/shutdown, and **keep evolving in the background
at 1/20 speed** (`BACKGROUND_SLOWDOWN`) while no client is attached —
full speed while anyone is playing.

Keyboard controls (when no controller is present): arrows/hjkl/wasd move,
`SPACE`=SQUARE, `x`=X, `o`=CIRCLE, `t`=TRIANGLE, `[`/`]`=L1/R1,
`-`/`=`=L2/R2, `b`=R3, `ESC`/`m`=menu, `ENTER`=confirm, `?`=controls guide. `TAB`/
`shift-TAB` cycles the placeable patterns (all nine gallery forms; like
picking from the gallery, this switches to setup/paint mode).

Mouse/touchpad controls (any terminal with mouse reporting, e.g. macOS
Terminal.app or iTerm2): **left-click a cell** = walk toward it,
**right-click** = SQUARE (kill / spawn baddie), **double-click** = X
(erase / reclaim), **click the `[ SETUP ]`/`[ RUN ]` badge** in the top
border = toggle paint ↔ run mode, **click a menu row** = select,
**click it again** = choose. Keyboard, controller and mouse can all be
used at the same time.

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
| TAB / shift-TAB   | Cycle the nine placeable patterns forward / backward (switches to setup/paint mode, like the gallery) |
| Mouse / touchpad  | Left-click a cell: walk there · right-click: kill / spawn baddie · double-click: erase · **click the mode badge: paint ↔ run** · menus: click to select, click again to choose |
| ?                 | Pop the in-client controls guide (keyboard + mouse) — works in-game and in every menu |

In run mode the Game of Life ticks and `@` participates as a living cell.

Save files are written to `~/wander_save.json` on the Pi.

## LLM bot (Kimi K3)

`wander_bot.py` attaches to a world as a normal play client and lets an
LLM drive its `@`: every `BOT_TICK` seconds it shows the model the 13×13
neighborhood, reads back one JSON action (`move`/`square`/`x`/`circle`/
`triangle`/`pattern`/`seed`/`wait`), and plays it. If the endpoint errors
or rambles, the bot takes a random step and carries on; it detaches
cleanly on SIGTERM like any client.

### Baseten

```sh
export BASETEN_API_KEY=...        # from your Baseten dashboard -- see note
export LLM_BASE_URL=https://inference.baseten.co/v1   # or your deployment's /v1 URL
export LLM_MODEL=moonshotai/Kimi-K3   # the exact model name your endpoint lists
python3 wander_bot.py             # joins world-1 (set WANDER_BOT_WORLD)
```

If you already run the `pi` coding agent with Baseten
(`PI_PROVIDER=baseten` / `PI_MODEL` / `BASETEN_API_KEY` exported), the bot
picks those up and `python3 wander_bot.py` works with no extra config.

Baseten's `Authorization: Api-Key ...` scheme is auto-detected from the
hostname, with one automatic retry using `Bearer` on 401/403 (pi's own
baseten provider uses Bearer; override with `LLM_AUTH_SCHEME`). **The key only ever exists as
an environment variable** — it is never logged, never passed via argv, and
must never be committed (this repo is public; `*.env` is gitignored).

Ollama works the same way, no key needed:

```sh
LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=llama3.2 python3 wander_bot.py
```

### On the Pi

```sh
sed -e "s|__WANDER_USER__|$USER|g" -e "s|__WANDER_HOME__|$HOME|g" \
    deploy/wander-bot.service.template | sudo tee /etc/systemd/system/wander-bot.service
printf 'LLM_API_KEY=%s\n' "$BASETEN_API_KEY" > ~/wander-bot.env
chmod 600 ~/wander-bot.env
sudo systemctl daemon-reload && sudo systemctl enable --now wander-bot
journalctl -u wander-bot -f
```

Once the unit is enabled, the `post-receive` deploy hook restarts
`wander-bot` alongside `wander` whenever bot/game code changes.

## Development

```sh
python3 test_wander.py          # game-logic tests (pygame stubbed)
python3 test_wander_server.py   # World + server protocol tests (pure python)
```

### Client/server protocol (v3)

Newline-delimited JSON over the UNIX socket `~/wander_server.sock`.
Every message carries the protocol version (`"v": 3`); mismatches are
rejected. Client: `list` / `create` / `delete` / `attach` (with
`role: play|watch`; play clients get a `pid` and a spawned avatar) /
`detach` / `input` (move + button actions, applied to the caller's own
avatar) / `command` (save, clear, seed, stop_baddies, set_pattern).
Server replies `{"ok": ...}` to the five rpc-style commands
(`input`/`command` are fire-and-forget and get no reply, so a stale `ok`
is never mistaken for a later one) and streams frames at 10 Hz: the attach
response is the client's full snapshot, later frames are **deltas**
containing only changed fields (nothing is sent while the world is
unchanged; a 5 s heartbeat proves liveness).

## Deployment

The deploy target is the Pi at `pi.local` (a `/etc/hosts` entry maps the
name to its LAN address: `192.168.8.195 pi.local`); the game lives in the
home directory there. Deployment is a plain git push:

```sh
git push deploy main
```

### Provisioning a fresh host

One command, run from the workstation (it renders the systemd unit and
sudoers rule for the user you log in as, installs the bare repo and the
post-receive hook, enables and starts the service):

```sh
ssh pi.local 'sh -s' < deploy/setup-host.sh
git remote add deploy pi.local:wander.git   # or: git remote set-url deploy ...
```

The pieces it installs are versioned here as templates:
`deploy/wander.service.template` and `deploy/sudoers-wander.template`
(placeholders `__WANDER_USER__` / `__WANDER_HOME__`), plus the ready-made
`deploy/post-receive` hook.

### How it works

- The Pi has **no system git** (and originally no passwordless sudo), so a
  rootless git is unpacked from the Raspbian `.deb` into `~/opt/git`
  (`apt-get download git` + `dpkg -x`).
  `~/opt/git/receive-pack-wrapper.sh` sets `PATH`/`GIT_EXEC_PATH` and is
  what the local repo invokes over SSH
  (`git config remote.deploy.receivepack ~/opt/git/receive-pack-wrapper.sh`,
  expanded on the Pi).
- `~/wander.git` is a bare repo; its `hooks/post-receive` (versioned in
  this repo as `deploy/post-receive`) checks out `main` into `$HOME` on
  every push and **restarts the wander service automatically when game
  code changed**, via `sudo -n systemctl restart wander`.
- `/etc/sudoers.d/wander` (rendered from `deploy/sudoers-wander.template`)
  grants the deploy user NOPASSWD rights to *exactly* `systemctl restart
  wander` — nothing else, and the password is not stored anywhere.
- The pre-git `wander.py` was backed up to `~/wander.py.bak.*` before the
  first deploy.

## License

[MIT](LICENSE).
