"""SQLite persistence for game state, win/loss records, and posts."""

from __future__ import annotations

import json
import logging
import os
import sqlite3

from game import GameState

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_state (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    active_team TEXT,
    turn_number INTEGER,
    status TEXT,
    red_grid TEXT,
    blue_grid TEXT,
    red_ships TEXT,
    blue_ships TEXT,
    red_last_post_uri TEXT,
    blue_last_post_uri TEXT,
    log_last_post_uri TEXT,
    red_vote_options TEXT,
    blue_vote_options TEXT,
    last_shot_team TEXT,
    last_shot_coord TEXT,
    last_shot_result TEXT,
    last_shot_ship TEXT,
    last_volley TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_history (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    winner TEXT,
    turns INTEGER,
    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS win_loss (
    team TEXT PRIMARY KEY,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0
);

-- Every post the bot creates, so a reset can delete exactly the bot's own
-- posts instead of indiscriminately emptying the account.
CREATE TABLE IF NOT EXISTS posts (
    uri TEXT PRIMARY KEY,
    team TEXT,            -- 'red' | 'blue' | 'log'
    rkey TEXT,
    cid TEXT,
    kind TEXT,            -- 'turn' | 'log' | 'credit' | 'newgame' | 'win' | 'leaderboard'
    game_id INTEGER,
    turn_number INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_posts_team ON posts (team);
CREATE INDEX IF NOT EXISTS idx_posts_game ON posts (game_id);
"""

# Columns added after the first release; existing databases get them via
# ALTER TABLE so an in-progress game survives the upgrade.
_ADDED_COLUMNS = {
    "game_state": [
        ("last_shot_team", "TEXT"),
        ("last_shot_coord", "TEXT"),
        ("last_shot_result", "TEXT"),
        ("last_shot_ship", "TEXT"),
        ("red_vote_options", "TEXT"),
        ("blue_vote_options", "TEXT"),
        ("last_volley", "TEXT"),
    ],
}


def _db_path() -> str:
    return os.environ.get("DB_PATH", "battleship.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly() -> sqlite3.Connection:
    """A connection that physically cannot write — for the dashboard.

    Note: a `file:...?mode=ro` URI does NOT work here. In WAL mode SQLite
    needs to create a shared-memory index, which a read-only connection
    can't do, so it fails with "unable to open database file". Opening
    normally and setting query_only gets the same guarantee and works.
    """
    conn = sqlite3.connect(_db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; _SCHEMA already has the columns
        for name, coltype in columns:
            if name not in existing:
                logger.info("Migrating: adding %s.%s", table, name)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


def init_db() -> None:
    """Create tables if needed, run migrations, seed win_loss rows."""
    with _connect() as conn:
        # WAL lets the dashboard read while the bot writes, without either
        # blocking the other. It's a property of the file, set once.
        # (Not supported on network filesystems — keep the DB on local disk.)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        for team in ("red", "blue"):
            conn.execute(
                "INSERT OR IGNORE INTO win_loss (team, wins, losses) VALUES (?, 0, 0)",
                (team,),
            )


def save_state(state: GameState) -> None:
    """Upsert the current game state (single row, id=1)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO game_state (
                id, game_id, active_team, turn_number, status,
                red_grid, blue_grid, red_ships, blue_ships,
                red_last_post_uri, blue_last_post_uri, log_last_post_uri,
                red_vote_options, blue_vote_options,
                last_shot_team, last_shot_coord, last_shot_result,
                last_shot_ship, last_volley, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                game_id=excluded.game_id,
                active_team=excluded.active_team,
                turn_number=excluded.turn_number,
                status=excluded.status,
                red_grid=excluded.red_grid,
                blue_grid=excluded.blue_grid,
                red_ships=excluded.red_ships,
                blue_ships=excluded.blue_ships,
                red_last_post_uri=excluded.red_last_post_uri,
                blue_last_post_uri=excluded.blue_last_post_uri,
                log_last_post_uri=excluded.log_last_post_uri,
                red_vote_options=excluded.red_vote_options,
                blue_vote_options=excluded.blue_vote_options,
                last_shot_team=excluded.last_shot_team,
                last_shot_coord=excluded.last_shot_coord,
                last_shot_result=excluded.last_shot_result,
                last_shot_ship=excluded.last_shot_ship,
                last_volley=excluded.last_volley,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                state.game_id,
                state.active_team,
                state.turn_number,
                state.status,
                json.dumps(state.red_grid),
                json.dumps(state.blue_grid),
                json.dumps(state.red_ships),
                json.dumps(state.blue_ships),
                state.red_last_post_uri,
                state.blue_last_post_uri,
                state.log_last_post_uri,
                json.dumps(state.red_vote_options),
                json.dumps(state.blue_vote_options),
                state.last_shot_team,
                state.last_shot_coord,
                state.last_shot_result,
                state.last_shot_ship,
                json.dumps(state.last_volley),
            ),
        )


def load_state() -> GameState | None:
    """Load the most recent game state, or None (also on read failure)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM game_state ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        logger.exception("DB read failed; treating as no existing game")
        return None
    if row is None:
        return None

    def _ships(raw: str) -> list:
        ships = json.loads(raw)
        # JSON turns (row, col) tuples into lists; restore tuples so
        # membership checks against tuple coordinates keep working.
        for ship in ships:
            ship["cells"] = [tuple(c) for c in ship["cells"]]
        return ships

    def _col(name: str) -> str:
        try:
            return row[name] or ""
        except (IndexError, KeyError):
            return ""

    def _json_list(name: str) -> list:
        raw = _col(name)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    return GameState(
        game_id=row["game_id"],
        active_team=row["active_team"],
        turn_number=row["turn_number"],
        status=row["status"],
        red_grid=json.loads(row["red_grid"]),
        blue_grid=json.loads(row["blue_grid"]),
        red_ships=_ships(row["red_ships"]),
        blue_ships=_ships(row["blue_ships"]),
        red_last_post_uri=_col("red_last_post_uri"),
        blue_last_post_uri=_col("blue_last_post_uri"),
        log_last_post_uri=_col("log_last_post_uri"),
        red_vote_options=_json_list("red_vote_options"),
        blue_vote_options=_json_list("blue_vote_options"),
        last_shot_team=_col("last_shot_team"),
        last_shot_coord=_col("last_shot_coord"),
        last_shot_result=_col("last_shot_result"),
        last_shot_ship=_col("last_shot_ship"),
        last_volley=_json_list("last_volley"),
    )


def record_win(winner: str, turns: int, game_id: int) -> None:
    """Append to game_history and bump the win/loss counters."""
    loser = "blue" if winner == "red" else "red"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO game_history (game_id, winner, turns) VALUES (?, ?, ?)",
            (game_id, winner, turns),
        )
        conn.execute("UPDATE win_loss SET wins = wins + 1 WHERE team = ?", (winner,))
        conn.execute("UPDATE win_loss SET losses = losses + 1 WHERE team = ?", (loser,))


def get_record() -> dict:
    """Return {'red': {'wins': n, 'losses': n}, 'blue': {...}}."""
    with _connect() as conn:
        rows = conn.execute("SELECT team, wins, losses FROM win_loss").fetchall()
    record = {"red": {"wins": 0, "losses": 0}, "blue": {"wins": 0, "losses": 0}}
    for row in rows:
        record[row["team"]] = {"wins": row["wins"], "losses": row["losses"]}
    return record


# ---------------------------------------------------------------------------
# Post tracking (so a reset can delete precisely the bot's own posts)
# ---------------------------------------------------------------------------

def rkey_from_uri(uri: str) -> str:
    """'at://did:plc:x/app.bsky.feed.post/3abc' -> '3abc'."""
    return uri.rsplit("/", 1)[-1] if uri else ""


def record_post(team: str, uri: str, kind: str, cid: str = "",
                game_id: int | None = None,
                turn_number: int | None = None) -> None:
    """Log a post the bot just created."""
    if not uri:
        return
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO posts
               (uri, team, rkey, cid, kind, game_id, turn_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uri, team, rkey_from_uri(uri), cid, kind, game_id, turn_number),
        )


def get_posts(team: str | None = None, game_id: int | None = None) -> list:
    """Recorded posts, newest first."""
    clauses, params = [], []
    if team:
        clauses.append("team = ?")
        params.append(team)
    if game_id is not None:
        clauses.append("game_id = ?")
        params.append(game_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM posts {where} ORDER BY created_at DESC, rowid DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def count_posts() -> dict:
    """{'red': n, 'blue': n, 'log': n} of recorded posts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT team, COUNT(*) AS n FROM posts GROUP BY team").fetchall()
    return {r["team"]: r["n"] for r in rows}


def forget_posts(uris: list) -> None:
    """Drop rows for posts that have been deleted from Bluesky."""
    if not uris:
        return
    with _connect() as conn:
        conn.executemany("DELETE FROM posts WHERE uri = ?",
                         [(u,) for u in uris])


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def reset_game_state() -> None:
    """Forget the current game so the next start is from scratch.

    Leaves the all-time record, voter stats, and post log alone.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM game_state")


def reset_all(keep_record: bool = False) -> None:
    """Full wipe for a clean slate — the testing reset.

    Clears the current game, game history, and the post log.
    With `keep_record=False` (the default) the all-time W/L counters are
    zeroed too, so a first real game doesn't open with test scores.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM game_state")
        conn.execute("DELETE FROM game_history")
        conn.execute("DELETE FROM posts")
        if not keep_record:
            conn.execute("UPDATE win_loss SET wins = 0, losses = 0")
