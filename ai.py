"""Fallback move logic when no follower votes are cast.

Classic hunt/target strategy operating on the firing view
(0 = unexplored, 2 = hit, 3 = miss):

- Target mode: if there are hit cells that don't belong to a sunk ship,
  the ship is still afloat — fire at unexplored cells orthogonally
  adjacent to those hits. If two or more active hits line up, prefer
  extending that line off either end (ships are straight).
- Hunt mode: no active hits — fire at a random unexplored cell.
"""

import random

from game import SIZE, WATER, HIT


def _in_bounds(r: int, c: int) -> bool:
    return 0 <= r < SIZE and 0 <= c < SIZE


def choose_move(firing_grid: list, opponent_ships: list) -> tuple:
    """Pick a (row_index, col_index) to fire at.

    `opponent_ships` is the opponent's ship list — only sunk-ness and
    cell membership of *sunk* ships is consulted, so no hidden
    information about unhit ships leaks into the decision.
    """
    sunk_cells = set()
    for ship in opponent_ships:
        if ship.get("sunk"):
            sunk_cells.update(tuple(c) for c in ship["cells"])

    # "Active" hits: hits on ships that are damaged but not yet sunk.
    active_hits = [
        (r, c)
        for r in range(SIZE)
        for c in range(SIZE)
        if firing_grid[r][c] == HIT and (r, c) not in sunk_cells
    ]

    if active_hits:
        candidates = _target_candidates(firing_grid, active_hits)
        if candidates:
            return random.choice(candidates)

    # Hunt mode: random unexplored cell.
    unexplored = [
        (r, c)
        for r in range(SIZE)
        for c in range(SIZE)
        if firing_grid[r][c] == WATER
    ]
    return random.choice(unexplored)


def choose_options(firing_grid: list, opponent_ships: list, count: int = 3) -> list:
    """Pick distinct candidate shots for followers to vote on.

    The first option follows the same strategy as the AI fallback. Remaining
    options add a little variety while still avoiding cells already fired at.
    """
    sunk_cells = set()
    for ship in opponent_ships:
        if ship.get("sunk"):
            sunk_cells.update(tuple(c) for c in ship["cells"])

    active_hits = [
        (r, c)
        for r in range(SIZE)
        for c in range(SIZE)
        if firing_grid[r][c] == HIT and (r, c) not in sunk_cells
    ]

    priority = []
    if active_hits:
        priority = _target_candidates(firing_grid, active_hits)

    unexplored = [
        (r, c)
        for r in range(SIZE)
        for c in range(SIZE)
        if firing_grid[r][c] == WATER
    ]
    random.shuffle(unexplored)

    out = []
    for coord in priority + unexplored:
        if coord not in out:
            out.append(coord)
        if len(out) >= count:
            break
    return out


def _target_candidates(firing_grid: list, hits: list) -> list:
    """Unexplored cells worth shooting given the active hits."""
    # If >= 2 hits share a row or column, the ship's orientation is known:
    # only the cells just past each end of the line are worth trying.
    if len(hits) >= 2:
        rows = {r for r, _ in hits}
        cols = {c for _, c in hits}
        line = []
        if len(rows) == 1:  # horizontal line
            r = hits[0][0]
            cs = sorted(c for _, c in hits)
            line = [(r, cs[0] - 1), (r, cs[-1] + 1)]
        elif len(cols) == 1:  # vertical line
            c = hits[0][1]
            rs = sorted(r for r, _ in hits)
            line = [(rs[0] - 1, c), (rs[-1] + 1, c)]
        line = [
            (r, c) for r, c in line
            if _in_bounds(r, c) and firing_grid[r][c] == WATER
        ]
        if line:
            return line

    # Otherwise (single hit, or line ends blocked): any unexplored
    # orthogonal neighbor of any active hit.
    candidates = set()
    for r, c in hits:
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if _in_bounds(nr, nc) and firing_grid[nr][nc] == WATER:
                candidates.add((nr, nc))
    return sorted(candidates)
