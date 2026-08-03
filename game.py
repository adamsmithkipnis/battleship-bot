"""Battleship rules, state, ship placement, and win detection.

Pure logic only — no I/O, no network, no rendering. Grids are 10x10 lists
of lists of ints: 0 = water, 1 = ship, 2 = hit, 3 = miss.
"""

import random
from dataclasses import dataclass, field

# Cell values
WATER, SHIP, HIT, MISS = 0, 1, 2, 3

SIZE = 10
ROWS = "ABCDEFGHIJ"

# (name, length) for the standard fleet
FLEET = [
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2),
]


@dataclass
class GameState:
    game_id: int
    active_team: str          # 'red' or 'blue'
    turn_number: int
    status: str               # 'active' | 'red_won' | 'blue_won'
    red_grid: list            # Red's own board (ships + hits from Blue)
    blue_grid: list
    red_ships: list           # [{'name': str, 'cells': [(row,col),...], 'sunk': bool}]
    blue_ships: list
    red_last_post_uri: str = ""
    blue_last_post_uri: str = ""
    log_last_post_uri: str = ""
    red_vote_options: list = field(default_factory=list)   # ['A1', 'B2', ...]
    blue_vote_options: list = field(default_factory=list)
    # The most recent shot in the game. Teams post on alternating ticks,
    # so a team's post has to report the opponent's shot from the tick
    # before it — that's what these carry across ticks.
    last_shot_team: str = ""
    last_shot_coord: str = ""
    last_shot_result: str = ""   # 'hit' | 'miss' | 'sunk:ShipName'
    last_shot_ship: str = ""     # ship struck, when the shot was a hit


def coord_to_index(row: str, col: int) -> tuple:
    """Convert ('A', 1) -> (0, 0)."""
    return ROWS.index(row.upper()), col - 1


def index_to_coord(r: int, c: int) -> str:
    """Convert (0, 0) -> 'A1'."""
    return f"{ROWS[r]}{c + 1}"


def _empty_grid() -> list:
    return [[WATER] * SIZE for _ in range(SIZE)]


def place_ships(grid: list) -> tuple:
    """Place all 5 ships randomly (horizontal or vertical) with no overlaps.

    Mutates and returns the grid, plus the ships list.
    """
    ships = []
    for name, length in FLEET:
        while True:
            horizontal = random.random() < 0.5
            if horizontal:
                r = random.randrange(SIZE)
                c = random.randrange(SIZE - length + 1)
                cells = [(r, c + i) for i in range(length)]
            else:
                r = random.randrange(SIZE - length + 1)
                c = random.randrange(SIZE)
                cells = [(r + i, c) for i in range(length)]
            if all(grid[rr][cc] == WATER for rr, cc in cells):
                for rr, cc in cells:
                    grid[rr][cc] = SHIP
                ships.append({"name": name, "cells": cells, "sunk": False})
                break
    return grid, ships


def new_game(game_id: int) -> GameState:
    """Fresh game: random fleets for both teams, Red fires first."""
    red_grid, red_ships = place_ships(_empty_grid())
    blue_grid, blue_ships = place_ships(_empty_grid())
    return GameState(
        game_id=game_id,
        active_team="red",
        turn_number=1,
        status="active",
        red_grid=red_grid,
        blue_grid=blue_grid,
        red_ships=red_ships,
        blue_ships=blue_ships,
    )


def fire(state: GameState, team: str, row: int, col: int) -> tuple:
    """Apply a shot by `team` at (row, col) on the opponent's grid.

    Returns (state, result) where result is 'hit', 'miss',
    'sunk:ShipName', or 'already_fired'. Mutates state in place.
    """
    if team == "red":
        grid, ships = state.blue_grid, state.blue_ships
    else:
        grid, ships = state.red_grid, state.red_ships

    cell = grid[row][col]
    if cell in (HIT, MISS):
        return state, "already_fired"

    if cell == SHIP:
        grid[row][col] = HIT
        for ship in ships:
            if (row, col) in [tuple(c) for c in ship["cells"]]:
                if all(grid[rr][cc] == HIT for rr, cc in ship["cells"]):
                    ship["sunk"] = True
                    return state, f"sunk:{ship['name']}"
                return state, "hit"
        return state, "hit"  # defensive: ship cell not in any ship record

    grid[row][col] = MISS
    return state, "miss"


def check_win(state: GameState, team: str) -> bool:
    """True if `team` has sunk all opponent ships."""
    opponent_ships = state.blue_ships if team == "red" else state.red_ships
    return all(s["sunk"] for s in opponent_ships)


def get_firing_view(grid: list, ships: list) -> list:
    """Opponent-facing view of `grid`: hits (2) and misses (3) only.

    Unhit ship cells are masked back to water so the firing team
    never sees where the remaining ships are.
    """
    return [
        [cell if cell in (HIT, MISS) else WATER for cell in row]
        for row in grid
    ]
