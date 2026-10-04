import os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import curses
import random
import time
import pygame

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

def step_game_of_life(asterisks, max_x, max_y, player_pos=None):
    """Evolves Conway's Game of Life. Treats player_pos as an active living cell if provided."""
    active_cells = set(asterisks)
    if player_pos:
        active_cells.add(player_pos)

    neighbor_counts = {}
    for (x, y) in active_cells:
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 1 <= nx <= max_x and 1 <= ny <= max_y:
                    neighbor_counts[(nx, ny)] = neighbor_counts.get((nx, ny), 0) + 1

    next_asterisks = set()
    for pos in active_cells:
        if neighbor_counts.get(pos, 0) in (2, 3):
            next_asterisks.add(pos)

    for pos, count in neighbor_counts.items():
        if count == 3:
            next_asterisks.add(pos)

    # The player's physical avatar (@) remains rendered independently
    if player_pos in next_asterisks:
        next_asterisks.remove(player_pos)

    return next_asterisks

def safe_addch(stdscr, y, x, char, attr=0):
    try:
        stdscr.addch(y, x, char, attr)
    except curses.error:
        pass

def run_gallery_menu(stdscr, controller, sw, sh):
    """Full-screen grid view to browse and select shapes using PS3 controller."""
    selected_idx = 0
    btn_cooldown = 0
    move_cooldown = 0

    stdscr.clear()

    while True:
        pygame.event.pump()
        now = time.time()

        dx, dy = 0, 0
        if controller.get_numhats() > 0:
            hat_x, hat_y = controller.get_hat(0)
            if hat_x != 0: dx = hat_x
            if hat_y != 0: dy = -hat_y

        if dx == 0 and dy == 0:
            ax = controller.get_axis(0)
            ay = controller.get_axis(1)
            if ax < -0.5: dx = -1
            elif ax > 0.5: dx = 1
            if ay < -0.5: dy = -1
            elif ay > 0.5: dy = 1

        if (dx != 0 or dy != 0) and (now - move_cooldown > 0.15):
            move_cooldown = now
            if dx > 0: selected_idx = min(len(ALL_PATTERNS) - 1, selected_idx + 1)
            elif dx < 0: selected_idx = max(0, selected_idx - 1)
            if dy > 0: selected_idx = min(len(ALL_PATTERNS) - 1, selected_idx + 2)
            elif dy < 0: selected_idx = max(0, selected_idx - 2)

        # Select shape with X (0) or O (1)
        if (controller.get_button(0) or controller.get_button(1)) and (now - btn_cooldown > 0.2):
            return selected_idx

        # Exit gallery with SELECT (8) or START (9)
        if (controller.get_button(8) or controller.get_button(9)) and (now - btn_cooldown > 0.3):
            return selected_idx

        # Render Gallery View
        stdscr.clear()
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
        stdscr.addstr(0, 4, " GALLERY MENU (D-Pad: Navigate | X/O: Select Shape) ")
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)

        # Draw 2-column Grid
        col_width = (sw - 10) // 2
        for i, (p_name, p_offsets) in enumerate(ALL_PATTERNS):
            col = i % 2
            row = i // 2
            start_x = 5 + col * col_width
            start_y = 3 + row * 6

            is_sel = (i == selected_idx)
            attr = curses.color_pair(1) | curses.A_BOLD if is_sel else curses.color_pair(3)
            prefix = "-> " if is_sel else "   "

            try:
                stdscr.addstr(start_y, start_x, f"{prefix}{p_name}", attr)
            except curses.error:
                pass

            # Render mini pattern preview
            for ox, oy in p_offsets:
                px_view = start_x + 4 + ox
                py_view = start_y + 2 + oy
                safe_addch(stdscr, py_view, px_view, '*', curses.color_pair(2))

        stdscr.refresh()
        time.sleep(0.02)

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
    curses.init_pair(1, curses.COLOR_CYAN, -1)    # Cursor / Active Preview
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Placed Stars
    curses.init_pair(3, curses.COLOR_GREEN, -1)   # Run Border
    curses.init_pair(4, curses.COLOR_MAGENTA, -1) # Shape Name Label
    curses.init_pair(5, curses.COLOR_RED, -1)     # Setup Border

    stdscr.nodelay(True)
    stdscr.timeout(30)

    sh, sw = stdscr.getmaxyx()
    max_x, max_y = sw - 2, sh - 2

    px, py = sw // 2, sh // 2

    asterisks = set()
    for _ in range(15):
        asterisks.add((random.randint(2, max_x - 1), random.randint(2, max_y - 1)))

    is_running = False  # Start in Setup Mode
    pattern_idx = 0
    static_pattern_idx = 0
    active_category = "dynamic" # "dynamic" or "static"

    move_cooldown = 0
    stamp_btn_cooldown = 0
    cycle_btn_cooldown = 0
    mode_btn_cooldown = 0
    gol_cooldown = 0

    last_rendered_cursor = set()
    stdscr.clear()

    while True:
        dx, dy = 0, 0
        pygame.event.pump()

        # D-Pad Movement
        if controller.get_numhats() > 0:
            hat_x, hat_y = controller.get_hat(0)
            if hat_x != 0: dx = hat_x
            if hat_y != 0: dy = -hat_y

        # Analog Movement
        if dx == 0 and dy == 0:
            ax = controller.get_axis(0)
            ay = controller.get_axis(1)
            if ax < -0.5: dx = -1
            elif ax > 0.5: dx = 1
            if ay < -0.5: dy = -1
            elif ay > 0.5: dy = 1

        # START (Button 9) or Button 10 to quit
        if controller.get_button(9) or controller.get_button(10):
            break

        now = time.time()
        need_full_redraw = False

        # Open Full-Screen Gallery Menu with SELECT (Button 8)
        if controller.get_button(8) and (now - mode_btn_cooldown > 0.3):
            mode_btn_cooldown = now
            chosen_idx = run_gallery_menu(stdscr, controller, sw, sh)
            if chosen_idx < len(PATTERNS_OSCILLATORS):
                active_category = "dynamic"
                pattern_idx = chosen_idx
            else:
                active_category = "static"
                static_pattern_idx = chosen_idx - len(PATTERNS_OSCILLATORS)
            is_running = False
            need_full_redraw = True

        # Toggle Setup / Run Mode via TRIANGLE (Button 2)
        if controller.get_button(2) and (now - mode_btn_cooldown > 0.3):
            mode_btn_cooldown = now
            is_running = not is_running
            need_full_redraw = True

        # Cycle Active Patterns via L1 (4) / R1 (5) in Setup Mode
        if not is_running and (now - cycle_btn_cooldown > 0.18):
            if controller.get_button(4):
                active_category = "dynamic"
                pattern_idx = (pattern_idx - 1) % len(PATTERNS_OSCILLATORS)
                cycle_btn_cooldown = now
            elif controller.get_button(5):
                active_category = "dynamic"
                pattern_idx = (pattern_idx + 1) % len(PATTERNS_OSCILLATORS)
                cycle_btn_cooldown = now

        # Cycle Static Forms via L2 (6) / R2 (7) in Setup Mode
        if not is_running and (now - cycle_btn_cooldown > 0.18):
            if controller.get_button(6):
                active_category = "static"
                static_pattern_idx = (static_pattern_idx - 1) % len(PATTERNS_STATIC)
                cycle_btn_cooldown = now
            elif controller.get_button(7):
                active_category = "static"
                static_pattern_idx = (static_pattern_idx + 1) % len(PATTERNS_STATIC)
                cycle_btn_cooldown = now

        # Clear Screen with SQUARE (Button 3) in Setup Mode
        if not is_running and controller.get_button(3) and (now - stamp_btn_cooldown > 0.2):
            stamp_btn_cooldown = now
            asterisks.clear()
            need_full_redraw = True

        # Current shape determination
        if active_category == "dynamic":
            p_name, p_offsets = PATTERNS_OSCILLATORS[pattern_idx]
        else:
            p_name, p_offsets = PATTERNS_STATIC[static_pattern_idx]

        # Stamp Pattern with CIRCLE (Button 1) in Setup Mode
        if not is_running and controller.get_button(1) and (now - stamp_btn_cooldown > 0.2):
            stamp_btn_cooldown = now
            for ox, oy in p_offsets:
                tx, ty = px + ox, py + oy
                if 1 <= tx <= max_x and 1 <= ty <= max_y:
                    asterisks.add((tx, ty))

        # Erase single star under cursor with X (Button 0) in Setup Mode
        if not is_running and controller.get_button(0) and (now - stamp_btn_cooldown > 0.2):
            stamp_btn_cooldown = now
            if (px, py) in asterisks:
                asterisks.remove((px, py))
                safe_addch(stdscr, py, px, ' ')

        # Execute Movement
        if (dx != 0 or dy != 0) and (now - move_cooldown > 0.08):
            move_cooldown = now
            px = max(1, min(max_x, px + dx))
            py = max(1, min(max_y, py + dy))

        # Game of Life Tick (Run mode - cursor acts as living cell)
        if is_running and (now - gol_cooldown > 0.4):
            gol_cooldown = now
            for ax_pos, ay_pos in asterisks:
                safe_addch(stdscr, ay_pos, ax_pos, ' ')
            asterisks = step_game_of_life(asterisks, max_x, max_y, player_pos=(px, py))

        # Redraw Screen
        if need_full_redraw:
            stdscr.clear()

        # Border & Mode Header
        border_color = curses.color_pair(3) if is_running else curses.color_pair(5)
        stdscr.attron(border_color)
        stdscr.border(0, 0, 0, 0, 0, 0, 0, 0)
        mode_label = "[ RUNNING ] (△: Setup | @ Interfere)" if is_running else "[ SETUP ] (L1/R1: Oscillators | L2/R2: Static | □: Clear | SELECT: Gallery)"
        stdscr.addstr(0, 4, f" {mode_label} ")
        stdscr.attroff(border_color)

        # Draw Asterisks
        for ax_pos, ay_pos in asterisks:
            safe_addch(stdscr, ay_pos, ax_pos, '*', curses.color_pair(2))

        # Clear previous cursor trail
        for cx, cy in last_rendered_cursor:
            if (cx, cy) in asterisks:
                safe_addch(stdscr, cy, cx, '*', curses.color_pair(2))
            else:
                safe_addch(stdscr, cy, cx, ' ')
        last_rendered_cursor.clear()

        # Render Cursor / Active Stamp Preview
        if not is_running:
            for ox, oy in p_offsets:
                cx, cy = px + ox, py + oy
                if 1 <= cx <= max_x and 1 <= cy <= max_y:
                    safe_addch(stdscr, cy, cx, '*', curses.color_pair(1) | curses.A_BOLD)
                    last_rendered_cursor.add((cx, cy))

            # Shape Footer
            footer = f" Active Shape: {p_name} ({active_category.upper()}) "
            try:
                stdscr.addstr(sh - 1, 4, footer + " " * 5, curses.color_pair(4) | curses.A_BOLD)
            except curses.error:
                pass
        else:
            safe_addch(stdscr, py, px, '@', curses.color_pair(1) | curses.A_BOLD)
            last_rendered_cursor.add((px, py))
            try:
                stdscr.addstr(sh - 1, 4, " " * 45, curses.color_pair(3))
            except curses.error:
                pass

        # Top Right Stats
        header = f" Pos: ({px},{py}) | Stars: {len(asterisks)} "
        try:
            stdscr.addstr(0, max(0, sw - len(header) - 4), header, border_color)
        except curses.error:
            pass

        stdscr.refresh()
        time.sleep(0.01)

if __name__ == "__main__":
    curses.wrapper(main)
