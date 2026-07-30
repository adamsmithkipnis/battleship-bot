"""Board image generation with Pillow.

Each post image is an 880x480 canvas with two 10x10 grids side by side:
the team's own fleet on the left, their firing view on the right.

Layout math:
  cell = 38px  ->  grid = 380px wide/tall
  left grid  x: 40..420   (40px left margin holds the row labels)
  gap        x: 420..460  (40px)
  right grid x: 460..840  (40px right margin)
  title bar  y: 8..36, column labels ~y 44, grids y: 64..444
"""

import io

from PIL import Image, ImageDraw, ImageFont

from game import SIZE, ROWS, WATER, SHIP, HIT, MISS, index_to_coord

CELL = 38
GRID_PX = CELL * SIZE           # 380
CANVAS_W, CANVAS_H = 880, 480
LEFT_X, RIGHT_X = 40, 460      # grid origins
GRID_Y = 64

BG = "#0d1117"
GRID_LINE = "#2d3748"
LABEL = "#ffffff"
CELL_COLORS = {
    WATER: "#1a5f8a",
    SHIP: "#5a6472",
    HIT: "#cc2200",
    MISS: "#334455",
}
TITLE_TINTS = {"red": "#8a1a1a", "blue": "#1a3a8a"}

# Candidate monospace fonts on macOS, in preference order.
_FONT_PATHS = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
]


def _font(size: int):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_grid(draw: ImageDraw.ImageDraw, grid: list, x0: int, y0: int,
               small_font) -> None:
    """One 10x10 grid with row/column labels at (x0, y0)."""
    # Column labels 1-10 above the grid, row labels A-J to the left.
    for c in range(SIZE):
        label = str(c + 1)
        cx = x0 + c * CELL + CELL // 2
        draw.text((cx, y0 - 12), label, fill=LABEL, font=small_font, anchor="mm")
    for r in range(SIZE):
        cy = y0 + r * CELL + CELL // 2
        draw.text((x0 - 14, cy), ROWS[r], fill=LABEL, font=small_font, anchor="mm")

    for r in range(SIZE):
        for c in range(SIZE):
            cx0, cy0 = x0 + c * CELL, y0 + r * CELL
            cx1, cy1 = cx0 + CELL, cy0 + CELL
            value = grid[r][c]
            draw.rectangle((cx0, cy0, cx1, cy1),
                           fill=CELL_COLORS[value], outline=GRID_LINE)
            center_x, center_y = cx0 + CELL // 2, cy0 + CELL // 2
            if value == HIT:
                # White X, drawn with lines (more reliable than glyphs).
                inset = 10
                draw.line((cx0 + inset, cy0 + inset, cx1 - inset, cy1 - inset),
                          fill="white", width=3)
                draw.line((cx1 - inset, cy0 + inset, cx0 + inset, cy1 - inset),
                          fill="white", width=3)
            elif value == MISS:
                rad = 5
                draw.ellipse((center_x - rad, center_y - rad,
                              center_x + rad, center_y + rad), fill="white")


def render_board(own_grid: list, firing_grid: list, team_color: str,
                 turn_number: int) -> bytes:
    """Render both grids to PNG bytes ready to upload to Bluesky."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)
    title_font = _font(18)
    small_font = _font(13)

    tint = TITLE_TINTS.get(team_color, GRID_LINE)
    for x0, title in ((LEFT_X, "OUR FLEET"), (RIGHT_X, "OUR SHOTS")):
        draw.rectangle((x0, 8, x0 + GRID_PX, 36), fill=tint)
        draw.text((x0 + GRID_PX // 2, 22), title,
                  fill="white", font=title_font, anchor="mm")
    draw.text((CANVAS_W - 8, CANVAS_H - 10), f"TURN {turn_number}",
              fill="#8b949e", font=small_font, anchor="rd")

    _draw_grid(draw, own_grid, LEFT_X, GRID_Y, small_font)
    _draw_grid(draw, firing_grid, RIGHT_X, GRID_Y, small_font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _count(grid: list, value: int) -> int:
    return sum(row.count(value) for row in grid)


def _names(ships: list, sunk: bool) -> list:
    return [s["name"] for s in ships if bool(s.get("sunk")) == sunk]


def _join(names: list) -> str:
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def build_alt_text(own_grid: list, own_ships: list, firing_grid: list,
                   opponent_ships: list, turn_number: int) -> str:
    """Describe the board for screen readers.

    The image *is* the game state, so this carries everything a follower
    needs to vote: what's afloat, where shots have landed, and which
    hits belong to a ship that hasn't sunk yet (the best cells to
    follow up on).
    """
    sunk_cells = set()
    for ship in opponent_ships:
        if ship.get("sunk"):
            sunk_cells.update(tuple(c) for c in ship["cells"])
    live_hits = [
        index_to_coord(r, c)
        for r in range(SIZE)
        for c in range(SIZE)
        if firing_grid[r][c] == HIT and (r, c) not in sunk_cells
    ]

    afloat = _names(own_ships, sunk=False)
    lost = _names(own_ships, sunk=True)
    killed = _names(opponent_ships, sunk=True)

    parts = [
        f"Turn {turn_number}. Two 10 by 10 Battleship grids, "
        f"rows A to J and columns 1 to 10.",
        f"Left grid, our fleet: {_join(afloat)} still afloat; "
        f"{_join(lost)} sunk. "
        f"The enemy has landed {_count(own_grid, HIT)} hits on us "
        f"and missed {_count(own_grid, MISS)} times.",
        f"Right grid, our shots: {_count(firing_grid, HIT)} hits and "
        f"{_count(firing_grid, MISS)} misses. "
        f"Enemy ships sunk: {_join(killed)} "
        f"({len(killed)} of {len(opponent_ships)}).",
    ]
    if live_hits:
        parts.append(
            "Confirmed hits on ships not yet sunk: "
            + ", ".join(live_hits) + "."
        )
    return " ".join(parts)
