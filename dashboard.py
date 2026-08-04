"""Read-only web dashboard for the Battleship bot.

    python3 dashboard.py        # then open http://127.0.0.1:8765

Touches nothing. The database is opened with PRAGMA query_only so it
physically cannot write, and the live vote tally is read from Bluesky's
public AppView with no credentials at all. Running this cannot disturb a
game in progress.

Route layout is deliberate: everything public-safe lives under /api/v1/*,
and any future destructive controls go in a separate admin blueprint that
simply isn't mounted when the app is exposed. See the SECURITY note below.

SECURITY: binds to 127.0.0.1 by default. The board images show your own
ship positions, so anyone who can load this page can see both fleets —
keep it local, or reach it over Tailscale. Do not port-forward it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template_string, request

import bluesky
import db
import game
import renderer

load_dotenv()

app = Flask(__name__)

SERVICE = "com.battleship.bot"
PUBLIC_API = "https://public.api.bsky.app/xrpc/"
TURN_MINUTES = int(os.environ.get("TURN_MINUTES", "60"))
TICK_SECONDS = TURN_MINUTES * 60 // 2

_http = httpx.Client(timeout=15)


# ---------------------------------------------------------------------------
# Bot process status
# ---------------------------------------------------------------------------

def bot_status() -> dict:
    """Ask launchd whether the job is loaded and running."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as exc:
        return {"loaded": False, "running": False, "pid": None,
                "error": str(exc)}
    for line in out.splitlines():
        if SERVICE in line:
            parts = line.split("\t")
            pid = parts[0].strip()
            return {
                "loaded": True,
                "running": pid.isdigit(),
                "pid": int(pid) if pid.isdigit() else None,
                "last_exit": parts[1].strip() if len(parts) > 1 else None,
            }
    return {"loaded": False, "running": False, "pid": None}


# ---------------------------------------------------------------------------
# Game state (read-only)
# ---------------------------------------------------------------------------

def _parse_utc(value: str):
    """SQLite CURRENT_TIMESTAMP is UTC and has no timezone marker."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _json_list(row, name: str) -> list:
    """Read a JSON list column, tolerating NULL or malformed values."""
    import json
    try:
        raw = row[name]
    except (IndexError, KeyError):
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def read_state() -> dict:
    """Current game plus record and post counts, from a read-only handle."""
    conn = db.connect_readonly()
    try:
        row = conn.execute(
            "SELECT * FROM game_state ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
        record = {"red": {"wins": 0, "losses": 0}, "blue": {"wins": 0, "losses": 0}}
        for r in conn.execute("SELECT team, wins, losses FROM win_loss"):
            record[r["team"]] = {"wins": r["wins"], "losses": r["losses"]}
        games = conn.execute(
            "SELECT COUNT(*) n FROM game_history").fetchone()["n"]
        posts = {r["team"]: r["n"] for r in conn.execute(
            "SELECT team, COUNT(*) n FROM posts GROUP BY team")}
        if row is None:
            return {"has_game": False, "record": record, "games_played": games,
                    "posts": posts}

        updated = _parse_utc(row["updated_at"])
        ships_left = {}
        for side in ("red", "blue"):
            import json
            ships = json.loads(row[f"{side}_ships"])
            ships_left[side] = sum(1 for s in ships if not s.get("sunk"))

        return {
            "has_game": True,
            "game_id": row["game_id"],
            "turn_number": row["turn_number"],
            "active_team": row["active_team"],
            "status": row["status"],
            "last_update": updated.isoformat() if updated else None,
            "next_tick": ((updated + timedelta(seconds=TICK_SECONDS)).isoformat()
                          if updated else None),
            "ships_left": ships_left,
            "last_shot": {
                "team": row["last_shot_team"], "coord": row["last_shot_coord"],
                "result": row["last_shot_result"], "ship": row["last_shot_ship"],
            },
            "vote_post_uri": (row["red_last_post_uri"]
                              if row["active_team"] == "red"
                              else row["blue_last_post_uri"]),
            "vote_options": _json_list(row, f"{row['active_team']}_vote_options"),
            "record": record,
            "games_played": games,
            "posts": posts,
        }
    finally:
        conn.close()


def load_gamestate():
    """Full GameState, for rendering boards."""
    return db.load_state()


# ---------------------------------------------------------------------------
# Live votes via the public AppView (no credentials needed)
# ---------------------------------------------------------------------------

def _as_reply_obj(post: dict):
    """Adapt public-API JSON to what bluesky.collect_votes expects.

    The REST payload uses camelCase (createdAt) while the SDK models use
    snake_case (created_at); normalise here so the vote logic is shared.
    """
    record = post.get("record", {}) or {}
    rec = SimpleNamespace(
        text=record.get("text", ""),
        created_at=record.get("createdAt", ""),
    )
    parent = (record.get("reply") or {}).get("root") or {}
    if parent:
        rec.reply = SimpleNamespace(
            root=SimpleNamespace(uri=parent.get("uri", ""),
                                 cid=parent.get("cid", "")))
    author = post.get("author", {}) or {}
    return SimpleNamespace(
        author=SimpleNamespace(did=author.get("did", ""),
                               handle=author.get("handle", "")),
        record=rec,
        uri=post.get("uri", ""),
        cid=post.get("cid", ""),
    )


def live_votes(state: dict) -> dict:
    """Running tally on the post the acting team is collecting votes on."""
    uri = state.get("vote_post_uri")
    if not uri or not state.get("has_game") or state.get("status") != "active":
        return {"available": False, "reason": "no open voting post", "votes": []}
    try:
        response = _http.get(PUBLIC_API + "app.bsky.feed.getPostThread",
                             params={"uri": uri, "depth": 1})
        response.raise_for_status()
        thread = response.json().get("thread", {})
    except Exception as exc:
        return {"available": False, "reason": _short_reason(exc), "votes": [],
                "post_uri": uri, "post_url": _bsky_url(uri)}

    replies = [_as_reply_obj(r["post"]) for r in (thread.get("replies") or [])
               if r.get("post")]
    gs = load_gamestate()
    fired = set()
    if gs is not None:
        grid = gs.blue_grid if gs.active_team == "red" else gs.red_grid
        fired = {(game.ROWS[r], c + 1)
                 for r in range(game.SIZE) for c in range(game.SIZE)
                 if grid[r][c] in (game.HIT, game.MISS)}

    # Map the offered letters to coordinates so a reply of "B" counts here
    # exactly as it will when the bot tallies at the end of the window.
    offered = state.get("vote_options") or []
    choice_map = {label: game.coord_from_string(coord)
                  for label, coord in zip("ABC", offered)}
    breakdown = bluesky.vote_breakdown(replies, fired, choice_map)

    counts = {v["coord"]: v["votes"] for v in breakdown}
    options = [{"label": label, "coord": coord, "votes": counts.get(coord, 0)}
               for label, coord in zip("ABC", offered)]
    return {
        "available": True,
        "options": options,
        "post_uri": uri,
        "post_url": _bsky_url(uri),
        "replies_seen": len(replies),
        "valid_voters": sum(v["votes"] for v in breakdown),
        "votes": breakdown,
    }


def _short_reason(exc: Exception) -> str:
    """A one-line explanation instead of a wall of exception text.

    A 400 here almost always means the stored post is gone — which is
    exactly what happens after a reset deletes posts while the bot still
    has the old URI on file.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (400, 404):
        return "voting post not found — it may have been deleted"
    if status == 429:
        return "Bluesky rate limit — will retry"
    if status:
        return f"Bluesky returned HTTP {status}"
    name = type(exc).__name__
    if "Timeout" in name:
        return "timed out reaching Bluesky"
    if "Connect" in name or "Network" in name:
        return "cannot reach Bluesky"
    return f"could not read votes ({name})"


def _bsky_url(uri: str) -> str:
    """at://did/app.bsky.feed.post/rkey -> a clickable bsky.app link."""
    try:
        _, rest = uri.split("at://", 1)
        did, _collection, rkey = rest.split("/", 2)
        return f"https://bsky.app/profile/{did}/post/{rkey}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/v1/status")
def api_status():
    state = read_state()
    return jsonify({"bot": bot_status(), "game": state,
                    "turn_minutes": TURN_MINUTES})


@app.route("/api/v1/votes")
def api_votes():
    return jsonify(live_votes(read_state()))


@app.route("/api/v1/log")
def api_log():
    lines = min(int(request.args.get("lines", "40")), 500)
    path = os.environ.get("LOG_PATH", "")
    if not path or not os.path.exists(path):
        return jsonify({"lines": [], "error": "log file not found"})
    try:
        with open(path, "rb") as handle:
            # Read only the tail; the log grows without bound.
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64_000))
            text = handle.read().decode("utf-8", "replace")
        return jsonify({"lines": text.splitlines()[-lines:]})
    except Exception as exc:
        return jsonify({"lines": [], "error": str(exc)})


@app.route("/board/<team>.png")
def board_png(team):
    if team not in ("red", "blue"):
        return Response("unknown team", status=404)
    state = load_gamestate()
    if state is None:
        return Response("no game", status=404)
    if team == "red":
        own, firing = state.red_grid, game.get_firing_view(state.blue_grid,
                                                           state.blue_ships)
    else:
        own, firing = state.blue_grid, game.get_firing_view(state.red_grid,
                                                            state.red_ships)
    png = renderer.render_board(own, firing, team, state.turn_number)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE = """
<!-- rendered inside the Flask template; kept in one file for portability -->
<title>Battleship Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { background:#0d1117; color:#c9d1d9; margin:0;
         font:14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { padding:14px 20px; border-bottom:1px solid #2d3748;
           display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; letter-spacing:.08em; }
  .wrap { padding:20px; display:grid; gap:20px;
          grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); }
  .card { border:1px solid #2d3748; border-radius:8px; overflow:hidden; }
  .card h2 { font-size:12px; margin:0; padding:8px 12px; background:#161b22;
             border-bottom:1px solid #2d3748; letter-spacing:.1em;
             text-transform:uppercase; color:#8b949e; }
  .card .body { padding:12px; }
  img { width:100%; height:auto; display:block; }
  .pill { padding:2px 8px; border-radius:10px; font-size:12px; }
  .ok { background:#12341f; color:#4ade80; } .bad { background:#3a1212; color:#f87171; }
  .red { color:#f87171; } .blue { color:#60a5fa; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-weight:normal; font-size:12px; }
  .lead { background:#1a2f1a; }
  pre { margin:0; max-height:280px; overflow:auto; font-size:12px;
        color:#8b949e; white-space:pre-wrap; }
  .big { font-size:22px; }
  .muted { color:#6e7681; } a { color:#60a5fa; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
</style>
<header>
  <h1>⚓ BATTLESHIP BOT</h1>
  <span id="botstate" class="pill">…</span>
  <span id="gameinfo" class="muted"></span>
  <span id="countdown" class="muted" style="margin-left:auto"></span>
</header>
<div class="wrap">
  <div class="card"><h2>🔴 Team Red</h2><img id="rboard" alt="Red board"></div>
  <div class="card"><h2>🔵 Team Blue</h2><img id="bboard" alt="Blue board"></div>

  <div class="card"><h2>Live votes — A/B/C ballot</h2><div class="body" id="votes">…</div></div>

  <div class="card"><h2>Standings</h2><div class="body">
    <div class="grid2">
      <div><div class="muted">Record</div><div id="record"></div></div>
      <div><div class="muted">Ships left</div><div id="ships"></div></div>
    </div>
  </div></div>

  <div class="card" style="grid-column:1/-1"><h2>Log</h2>
    <div class="body"><pre id="log">…</pre></div></div>
</div>
<script>
const $ = id => document.getElementById(id);
let nextTick = null;

async function j(u) { const r = await fetch(u); return r.json(); }

function fmtRecord(rec) {
  return `<span class="red">🔴 ${rec.red.wins}W ${rec.red.losses}L</span> &nbsp; ` +
         `<span class="blue">🔵 ${rec.blue.wins}W ${rec.blue.losses}L</span>`;
}

async function refresh() {
  try {
    const s = await j('/api/v1/status');
    const bot = s.bot, g = s.game;
    $('botstate').className = 'pill ' + (bot.running ? 'ok' : 'bad');
    $('botstate').textContent = bot.running ? `running · pid ${bot.pid}`
      : (bot.loaded ? 'stopped' : 'not loaded');
    if (g.has_game) {
      const who = g.active_team === 'red' ? '<span class="red">🔴 Red</span>'
                                          : '<span class="blue">🔵 Blue</span>';
      $('gameinfo').innerHTML = `game ${g.game_id} · turn ${g.turn_number} · ` +
        `${who} to fire · <span class="muted">${g.status}</span>`;
      $('record').innerHTML = fmtRecord(g.record) +
        ` <span class="muted">(${g.games_played} finished)</span>`;
      $('ships').innerHTML =
        `<span class="red">🔴 ${g.ships_left.red}/5</span> &nbsp; ` +
        `<span class="blue">🔵 ${g.ships_left.blue}/5</span>`;
      nextTick = g.next_tick ? new Date(g.next_tick) : null;
      const bust = '?t=' + Date.now();
      $('rboard').src = '/board/red.png' + bust;
      $('bboard').src = '/board/blue.png' + bust;
    } else {
      $('gameinfo').textContent = 'no game yet';
      $('record').innerHTML = fmtRecord(g.record);
    }

    const v = await j('/api/v1/votes');
    if (!v.available) {
      $('votes').innerHTML = `<span class="muted">${v.reason}</span>` +
        (v.post_url ? ` <a href="${v.post_url}" target="_blank">open post ↗</a>` : '');
    } else {
      const opts = v.options || [];
      const most = Math.max(0, ...opts.map(o => o.votes));
      let h = '';
      if (opts.length) {
        h += '<table><tr><th></th><th>coord</th><th>votes</th></tr>';
        opts.forEach(o => {
          const lead = (most > 0 && o.votes === most) ? 'lead' : '';
          h += `<tr class="${lead}"><td class="big">${o.label}</td>` +
               `<td class="big">${o.coord}</td><td>${o.votes}</td></tr>`;
        });
        h += '</table>';
      }
      // Write-in coordinates still count; show any that aren't on the ballot.
      const ballot = new Set(opts.map(o => o.coord));
      const writeIns = (v.votes || []).filter(x => !ballot.has(x.coord));
      if (writeIns.length) {
        h += '<div class="muted" style="margin-top:10px">write-ins</div><table>';
        writeIns.forEach(x => {
          h += `<tr><td class="big">${x.coord}</td><td>${x.votes}</td>` +
               `<td class="muted">@${x.first_caller}</td></tr>`;
        });
        h += '</table>';
      }
      if (!opts.length && !writeIns.length) {
        h = `<span class="muted">no votes yet — ` +
            `${v.replies_seen} repl${v.replies_seen === 1 ? 'y' : 'ies'} seen. ` +
            `Option A fires if none arrive.</span>`;
      }
      h += `<div class="muted" style="margin-top:8px">${v.valid_voters} voter(s), ` +
           `${v.replies_seen} repl(ies)` +
           (v.post_url ? ` · <a href="${v.post_url}" target="_blank">open post ↗</a>` : '') +
           `</div>`;
      $('votes').innerHTML = h;
    }

    const lg = await j('/api/v1/log?lines=40');
    $('log').textContent = lg.error ? lg.error : lg.lines.join('\\n');
    $('log').scrollTop = $('log').scrollHeight;
  } catch (e) {
    $('botstate').className = 'pill bad';
    $('botstate').textContent = 'dashboard error: ' + e.message;
  }
}

function tickCountdown() {
  if (!nextTick) { $('countdown').textContent = ''; return; }
  const ms = nextTick - new Date();
  if (ms <= 0) { $('countdown').textContent = 'next shot due now'; return; }
  const m = Math.floor(ms / 60000), s = Math.floor((ms % 60000) / 1000);
  $('countdown').textContent =
    `next shot in ${m}:${String(s).padStart(2, '0')} — voting closes`;
}

refresh();
setInterval(refresh, 15000);
setInterval(tickCountdown, 1000);
</script>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


def main() -> int:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8765"))
    if not os.environ.get("DB_PATH"):
        print("DB_PATH is not set — is .env present in this directory?",
              file=sys.stderr)
    db_path = db._db_path()
    if not os.path.exists(db_path):
        print(f"Warning: no database at {db_path} yet. "
              f"Start the bot once and reload.", file=sys.stderr)
    if host not in ("127.0.0.1", "localhost"):
        print(f"NOTE: binding to {host} — this exposes both fleets' ship "
              f"positions to anyone who can reach it. Prefer Tailscale over "
              f"port forwarding.", file=sys.stderr)
    print(f"Battleship dashboard → http://{host}:{port}")
    # load_dotenv=False: we already loaded .env explicitly at import, and
    # Flask's own scan walks up from the cwd, which is a surprise waiting
    # to happen when launchd starts this from elsewhere.
    app.run(host=host, port=port, debug=False, threaded=True,
            load_dotenv=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
