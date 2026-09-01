"""Move selection: what to fire at, and which cells are provably empty.

Two ideas do the work here, both suggested by a player:

1. Target mode — once a ship is wounded, chase it along its axis.
2. Rule out cells that cannot hold a ship. If a cell has no room for a
   run of L consecutive open cells (L = the smallest ship still afloat),
   nothing can be there and firing at it wastes a shot. That generalises
   the "four misses in a diamond around an empty centre" rule: with L=2,
   a centre whose four orthogonal neighbours are all misses has no run of
   two through it, so it is dead.

On top of that, hunting only considers cells where (row + col) % L == 0.
Any L-length ship spans L consecutive diagonals, so exactly one of its
cells satisfies that — searching the rest is wasted effort.
"""

import random

from game import SIZE, WATER, HIT, MISS


def _in_bounds(r: int, c: int) -> bool:
    return 0 <= r < SIZE and 0 <= c < SIZE


def sunk_cells(opponent_ships: list) -> set:
    """Cells belonging to ships already sunk — known empty water now."""
    cells = set()
    for ship in opponent_ships:
        if ship.get("sunk"):
            cells.update(tuple(c) for c in ship["cells"])
    return cells


def min_afloat_length(opponent_ships: list) -> int:
    """Length of the smallest opponent ship still afloat."""
    lengths = [len(s["cells"]) for s in opponent_ships if not s.get("sunk")]
    return min(lengths) if lengths else 2


def can_hold_ship(firing_grid: list, r: int, c: int, length: int,
                  dead: set) -> bool:
    """Could a ship of `length` still occupy (r, c)?

    True when some horizontal or vertical run of `length` cells through
    (r, c) contains no confirmed miss and no cell of an already-sunk ship.
    """
    for dr, dc in ((0, 1), (1, 0)):
        for offset in range(-length + 1, 1):
            run = [(r + dr * (offset + i), c + dc * (offset + i))
                   for i in range(length)]
            if any(not _in_bounds(rr, cc) for rr, cc in run):
                continue
            if all(firing_grid[rr][cc] != MISS and (rr, cc) not in dead
                   for rr, cc in run):
                return True
    return False


def _active_hits(firing_grid: list, dead: set) -> list:
    """Hits belonging to ships that are damaged but not yet sunk."""
    return [(r, c)
            for r in range(SIZE) for c in range(SIZE)
            if firing_grid[r][c] == HIT and (r, c) not in dead]


def _target_candidates(firing_grid: list, hits: list) -> list:
    """Unexplored cells worth shooting given the active hits."""
    # If >= 2 hits share a row or column the orientation is known, so only
    # the cells just past each end of the line are worth trying.
    if len(hits) >= 2:
        rows = {r for r, _ in hits}
        cols = {c for _, c in hits}
        line = []
        if len(rows) == 1:
            r = hits[0][0]
            cs = sorted(c for _, c in hits)
            line = [(r, cs[0] - 1), (r, cs[-1] + 1)]
        elif len(cols) == 1:
            c = hits[0][1]
            rs = sorted(r for r, _ in hits)
            line = [(rs[0] - 1, c), (rs[-1] + 1, c)]
        line = [(r, c) for r, c in line
                if _in_bounds(r, c) and firing_grid[r][c] == WATER]
        if line:
            return line

    candidates = set()
    for r, c in hits:
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if _in_bounds(nr, nc) and firing_grid[nr][nc] == WATER:
                candidates.add((nr, nc))
    return sorted(candidates)


def hunt_pool(firing_grid: list, opponent_ships: list) -> list:
    """Unexplored cells that could still hold a ship, parity-filtered.

    Falls back to the wider set if filtering leaves nothing, so this can
    never return empty while any unexplored cell remains.
    """
    dead = sunk_cells(opponent_ships)
    length = min_afloat_length(opponent_ships)
    unexplored = [(r, c) for r in range(SIZE) for c in range(SIZE)
                  if firing_grid[r][c] == WATER]
    viable = [(r, c) for r, c in unexplored
              if can_hold_ship(firing_grid, r, c, length, dead)]
    parity = [(r, c) for r, c in viable if (r + c) % length == 0]
    return parity or viable or unexplored


def choose_move(firing_grid: list, opponent_ships: list) -> tuple:
    """Pick a single (row, col) to fire at."""
    picks = choose_volley(firing_grid, opponent_ships, 1)
    return picks[0]


def choose_volley(firing_grid: list, opponent_ships: list, count: int,
                  exclude=()) -> list:
    """Pick up to `count` distinct cells for one volley.

    Wounded ships are finished first, then hunting fills the rest.
    `exclude` holds cells already claimed this turn (by voters, say) so
    the same cell is never fired at twice in one volley.
    """
    chosen, taken = [], set(exclude)
    dead = sunk_cells(opponent_ships)

    for cell in _target_candidates(firing_grid, _active_hits(firing_grid, dead)):
        if len(chosen) >= count:
            return chosen
        if cell not in taken:
            chosen.append(cell)
            taken.add(cell)

    pool = [c for c in hunt_pool(firing_grid, opponent_ships) if c not in taken]
    random.shuffle(pool)
    for cell in pool:
        if len(chosen) >= count:
            break
        chosen.append(cell)
        taken.add(cell)
    return chosen


def choose_options(firing_grid: list, opponent_ships: list,
                   count: int = 3) -> list:
    """Distinct candidate shots, kept for the A/B/C ballot path."""
    return choose_volley(firing_grid, opponent_ships, count)
