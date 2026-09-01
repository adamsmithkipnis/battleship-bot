"""Orchestrator: APScheduler loop driving the crowdsourced Battleship game.

Teams alternate on a half-interval stagger: with TURN_MINUTES=10, Red acts
at 0:00, Blue at 0:05, Red at 0:10, and so on. Each tick exactly one team
fires and only that team posts, so each account posts once every
TURN_MINUTES and each side gets an equal voting window.

Every tick: read votes from the acting team's latest post, fire the
most-voted coordinate (AI fallback if no valid votes), and credit the
follower who called it. The battlelog account posts only completed game
results.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

import ai
import bluesky
import db
import game
from game import GameState, HIT, MISS
import renderer

load_dotenv()

logger = logging.getLogger("battleship")

# Minutes between a given team's own turns. Teams alternate on half of
# this, so each account posts every TURN_MINUTES and the two accounts are
# offset from each other by TURN_MINUTES / 2.
TURN_MINUTES = int(os.environ.get("TURN_MINUTES", "60"))
TICK_SECONDS = TURN_MINUTES * 60 // 2

# Volley fire: each side fires this many shots per turn, and the crowd's
# top SHOTS_PER_TURN coordinates are all taken — so a follower whose cell
# places anywhere in the top five still sees their shot fired. Held fixed
# rather than decaying with surviving ships (classic Salvo): simulation
# put fixed volleys ahead of Salvo on both game length and closeness,
# because Salvo starves the leading side exactly when it needs to finish.
SHOTS_PER_TURN = int(os.environ.get("SHOTS_PER_TURN", "5"))
# When the crowd names fewer cells than that, let the AI fill the rest.
FILL_VOLLEY = os.environ.get("FILL_VOLLEY", "1") not in ("0", "false", "no")

HASHTAG = "#Battleship"
EMOJI = {"red": "🔴", "blue": "🔵"}
HANDLE = {"red": "@battleshipred", "blue": "@battleshipblue"}
TEAM_NAME = {"red": "Team Red", "blue": "Team Blue"}
# How many suggested cells to print under each post.
SUGGESTIONS = 3

scheduler = BlockingScheduler()


def _opponent(team: str) -> str:
    return "blue" if team == "red" else "red"


# ---------------------------------------------------------------------------
# Post text builders
# ---------------------------------------------------------------------------

def _record_line() -> str:
    rec = db.get_record()
    return (f"🔴 {rec['red']['wins']}W {rec['red']['losses']}L | "
            f"🔵 {rec['blue']['wins']}W {rec['blue']['losses']}L")


def _format_options(options: list) -> str:
    """Suggested cells, shown as a hint rather than a ballot — replies are
    free coordinates so followers can bracket a target themselves."""
    return " ".join(options)


def _sunk_names(shots: list) -> list:
    return [s["ship"] for s in shots if str(s["result"]).startswith("sunk:")]


def build_incoming_line(opponent: str, volley: list) -> str:
    """Summarise the opponent's last volley.

    Their individual coordinates are visible on the board image, so this
    reports the damage rather than listing every cell — which is what has
    to fit in the post alongside our own volley.
    """
    hits = sum(1 for s in volley if s["result"] != "miss")
    sunk = _sunk_names(volley)
    line = f"Incoming from {HANDLE[opponent]}: {len(volley)} shots, {hits} hit"
    if hits != 1:
        line += "s"
    if sunk:
        line += f" — our {_join(sunk)} " + ("is" if len(sunk) == 1 else "are") + " sunk!"
    else:
        line += "."
    return line


def _join(names: list) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def build_turn_text(team: str, turn: int, shots: list, picks: list,
                    voted_count: int, incoming: str = "",
                    suggestions: list = ()) -> str:
    """The acting team's post: what hit us, our volley, and the next ask."""
    fired = " ".join(s["coord"] for s in shots)
    hits = [s["coord"] for s in shots if s["result"] != "miss"]
    sunk = _sunk_names(shots)
    own = f"We fired {fired}"
    if hits:
        own += f" — hit on {_join(hits)}!"
        if sunk:
            own += f" Their {_join(sunk)} " + ("is" if len(sunk) == 1 else "are") + " sunk!"
    else:
        own += " — all misses."

    if voted_count and picks:
        voters = picks[0].total_voters
        credit = (f"🫡 {voted_count} of {len(shots)} shots called by "
                  f"{voters} voter" + ("" if voters == 1 else "s"))
    else:
        credit = "🤖 No votes came in — we picked our own."

    head = f"Turn {turn} {EMOJI[team]} "
    body = f"{head}{incoming}\n{own}" if incoming else f"{head}{own}"
    ask = (f"Reply a coordinate — our top {SHOTS_PER_TURN} fire next volley. "
           f"{TURN_MINUTES} min. {HASHTAG}")
    if suggestions:
        ask = f"Ideas: {_format_options(suggestions)}\n" + ask
    return f"{body}\n{credit}\n{ask}"


def build_credit_reply(coord: str, result: str, sunk_ship: str | None,
                       turn: int, vote) -> str:
    """Reply sent to the follower whose coordinate was fired."""
    if result == "miss":
        outcome = "a miss"
    elif sunk_ship and str(sunk_ship).strip():
        outcome = f"a HIT — you sank their {sunk_ship}!"
    else:
        outcome = "a HIT!"
    return (f"🎯 Your call. We fired {coord} in our turn {turn} volley — "
            f"{outcome}\n\n({vote.count} of {vote.total_voters} votes) {HASHTAG}")


def build_win_text(winner: str, turns: int) -> str:
    rec = db.get_record()
    return (f"🏆 GAME OVER — {TEAM_NAME[winner]} wins in {turns} turns!\n\n"
            f"All-time record:\n"
            f"🔴 @battleshipred: {rec['red']['wins']}W {rec['red']['losses']}L\n"
            f"🔵 @battleshipblue: {rec['blue']['wins']}W {rec['blue']['losses']}L\n\n"
            f"New game starts in 1 hour. {HASHTAG}")


def build_log_win_text(winner: str, turns: int, game_id: int) -> str:
    return (f"🏆 GAME {game_id} OVER — {turns} turns\n"
            f"{HANDLE[winner]} wins.\n\n"
            f"All-time: {_record_line()}\n\n"
            f"Next game in 1 hour. {HASHTAG}")


# ---------------------------------------------------------------------------
# Rendering / state helpers
# ---------------------------------------------------------------------------

def _views(state: GameState, team: str) -> tuple:
    """(own grid, own ships, firing view, opponent ships) for `team`."""
    if team == "red":
        return (state.red_grid, state.red_ships,
                game.get_firing_view(state.blue_grid, state.blue_ships),
                state.blue_ships)
    return (state.blue_grid, state.blue_ships,
            game.get_firing_view(state.red_grid, state.red_ships),
            state.red_ships)


def _render_for(state: GameState, team: str) -> tuple:
    """(png bytes, alt text) from `team`'s perspective."""
    own, own_ships, firing, opp_ships = _views(state, team)
    png = renderer.render_board(own, firing, team, state.turn_number)
    alt = renderer.build_alt_text(own, own_ships, firing, opp_ships,
                                  state.turn_number)
    return png, alt


def _shot_options(state: GameState, team: str) -> list:
    """A few legal cells to suggest under the post."""
    _, _, firing, opp_ships = _views(state, team)
    return [
        game.index_to_coord(r, c)
        for r, c in ai.choose_volley(firing, opp_ships, SUGGESTIONS)
    ]


def _vote_options_for(state: GameState, team: str) -> list:
    return state.red_vote_options if team == "red" else state.blue_vote_options


def _set_vote_options(state: GameState, team: str, options: list) -> None:
    if team == "red":
        state.red_vote_options = options
    else:
        state.blue_vote_options = options


def _already_fired(state: GameState, team: str) -> set:
    """('A', 5)-style coords `team` has already fired at the opponent."""
    grid = state.blue_grid if team == "red" else state.red_grid
    return {
        (game.ROWS[r], c + 1)
        for r in range(game.SIZE)
        for c in range(game.SIZE)
        if grid[r][c] in (HIT, MISS)
    }


def _post_image(team: str, text: str, image: bytes, alt: str, kind: str,
                game_id: int, turn: int | None = None,
                extra_dids: dict | None = None) -> str:
    """Post an image and log the URI so a reset can find it later."""
    uri = bluesky.post_with_image(team, text, image, alt=alt,
                                 extra_dids=extra_dids)
    _remember(team, uri, kind, game_id, turn)
    return uri


def _post_text(team: str, text: str, kind: str, game_id: int,
               turn: int | None = None, extra_dids: dict | None = None) -> str:
    uri = bluesky.post_text(team, text, extra_dids=extra_dids)
    _remember(team, uri, kind, game_id, turn)
    return uri


def _remember(team: str, uri: str, kind: str, game_id: int,
              turn: int | None) -> None:
    """Post tracking must never break posting, so failures only log."""
    try:
        db.record_post(team, uri, kind, game_id=game_id, turn_number=turn)
    except Exception:
        logger.exception("Failed to record post %s", uri)


def _fetch_replies(uri: str) -> list:
    if not uri:
        return []
    try:
        return bluesky.get_replies(uri)
    except Exception:
        logger.exception("Failed to fetch replies from %s", uri)
        return []


# ---------------------------------------------------------------------------
# Game loop
# ---------------------------------------------------------------------------

def game_tick() -> None:
    try:
        _game_tick()
    except Exception:
        # Never let an unexpected error kill the scheduler.
        logger.exception("game_tick failed")


def _game_tick() -> None:
    state = db.load_state()
    if state is None:
        logger.warning("No game state found on tick; starting a new game")
        start_new_game()
        return
    if state.status != "active":
        logger.info("Game %s finished (%s); waiting for restart timer",
                    state.game_id, state.status)
        return

    team = state.active_team
    opponent = _opponent(team)
    vote_uri = (state.red_last_post_uri if team == "red"
                else state.blue_last_post_uri)
    vote_options = _vote_options_for(state, team)

    # 1. Collect votes from the acting team's latest post — the post that
    #    has been up for TURN_MINUTES asking for exactly this volley.
    picks = bluesky.top_votes(_fetch_replies(vote_uri),
                              _already_fired(state, team),
                              SHOTS_PER_TURN)

    _, _, firing_view, opp_ships = _views(state, team)
    cells = [game.coord_to_index(*p.coord) for p in picks]
    voted_count = len(cells)

    # The crowd's choices fire first; the AI fills any remaining shots so a
    # quiet hour still advances the game. Set FILL_VOLLEY=0 to fire only
    # what was actually voted for.
    if FILL_VOLLEY and len(cells) < SHOTS_PER_TURN:
        cells += ai.choose_volley(firing_view, opp_ships,
                                  SHOTS_PER_TURN - len(cells), exclude=cells)
    if not cells:
        cells = ai.choose_volley(firing_view, opp_ships, 1)

    turn = state.turn_number
    logger.info("Team %s volley: %d shot(s), %d from %d voter(s)", team,
                len(cells), voted_count,
                picks[0].total_voters if picks else 0)

    # The opponent's volley from the previous tick, reported in this post
    # before it gets overwritten below.
    incoming = ""
    if state.last_shot_team == opponent and state.last_volley:
        incoming = build_incoming_line(opponent, state.last_volley)

    # 2. Fire the volley, stopping early if the fleet is already sunk.
    shots, won = [], False
    for (r, c) in cells:
        state, result = game.fire(state, team, r, c)
        if result == "already_fired":
            continue
        ship_name = ""
        if result != "miss":
            target = state.blue_ships if team == "red" else state.red_ships
            for ship in target:
                if (r, c) in [tuple(cell) for cell in ship["cells"]]:
                    ship_name = ship["name"]
                    break
        shots.append({"coord": game.index_to_coord(r, c),
                      "result": result, "ship": ship_name})
        if game.check_win(state, team):
            won = True
            break

    if not shots:
        logger.error("Team %s volley hit no new cells; skipping tick", team)
        return

    # 3. Win check.
    if won:
        state.status = f"{team}_won"
        db.record_win(team, turn, state.game_id)
        logger.info("Team %s wins game %s in %d turns", team, state.game_id, turn)

    # 4. Post. If posting fails, state is NOT saved — the next tick re-reads
    #    the same post's replies and retries the turn.
    #    Normally only the acting team posts, which is what staggers the two
    #    accounts. A win is the exception: both accounts announce the result.
    # Every credited handle needs its DID to become a clickable mention.
    extra = {p.caller_handle: p.caller_did for p in picks
             if p.caller_handle and p.caller_did}

    try:
        if won:
            win_text = build_win_text(team, turn)
            for side in ("red", "blue"):
                img, alt = _render_for(state, side)
                uri = _post_image(side, win_text, img, alt, "win",
                                  state.game_id, turn)
                if side == "red":
                    state.red_last_post_uri = uri
                else:
                    state.blue_last_post_uri = uri
            log_text = build_log_win_text(team, turn, state.game_id)
            state.log_last_post_uri = _post_text("log", log_text, "win",
                                                 state.game_id, turn,
                                                 extra_dids=extra)
        else:
            next_options = _shot_options(state, team)
            _set_vote_options(state, team, next_options)
            img, alt = _render_for(state, team)
            text = build_turn_text(team, turn, shots, picks, voted_count,
                                   incoming, next_options)
            uri = _post_image(team, text, img, alt, "turn", state.game_id,
                              turn, extra_dids=extra)
            if team == "red":
                state.red_last_post_uri = uri
            else:
                state.blue_last_post_uri = uri
    except Exception:
        logger.exception("Posting failed; skipping turn (state not saved)")
        return

    state.last_shot_team = team
    state.last_volley = shots
    # Kept in step for anything still reading the single-shot fields.
    last = shots[-1]
    state.last_shot_coord = last["coord"]
    state.last_shot_result = last["result"]
    state.last_shot_ship = last["ship"]

    # 5. Persist immediately. The public post has already gone out, so if
    #    anything below dies the turn must NOT be replayed — that would
    #    post the same turn twice. A missed credit reply is far cheaper.
    if not won:
        state.active_team = opponent
        state.turn_number = turn + 1
    db.save_state(state)

    # 6. Credit every follower whose cell was fired, so each gets a
    #    notification. Strictly best-effort, and deliberately after the
    #    save above.
    by_coord = {s["coord"]: s for s in shots}
    for pick in picks:
        coord = f"{pick.coord[0]}{pick.coord[1]}"
        shot = by_coord.get(coord)
        if shot is None or not (pick.caller_uri and pick.caller_cid):
            continue
        try:
            reply_uri = bluesky.post_reply(
                team,
                build_credit_reply(coord, shot["result"], shot["ship"] or None,
                                   turn, pick),
                parent_uri=pick.caller_uri, parent_cid=pick.caller_cid,
                root_uri=pick.root_uri, root_cid=pick.root_cid,
            )
            _remember(team, reply_uri, "credit", state.game_id, turn)
        except Exception:
            logger.exception("Failed to post credit reply to %s",
                             pick.caller_handle)

    if won:
        delay = int(os.environ.get("RESTART_DELAY_SECONDS", "3600"))
        run_date = datetime.now() + timedelta(seconds=delay)
        scheduler.add_job(start_new_game, "date", run_date=run_date,
                          id=f"restart_game_{state.game_id}", replace_existing=True)
        logger.info("New game scheduled for %s", run_date)


def start_new_game() -> None:
    try:
        _start_new_game()
    except Exception:
        logger.exception("start_new_game failed")


def _start_new_game() -> None:
    previous = db.load_state()
    game_id = (previous.game_id + 1) if previous else 1
    state = game.new_game(game_id)
    logger.info("Starting game %d", game_id)

    record_line = _record_line()
    state.red_vote_options = _shot_options(state, "red")
    state.blue_vote_options = _shot_options(state, "blue")

    # Both accounts open the game: each needs a post of its own for its
    # followers to reply to. From here on they alternate.
    def opening_text(team: str) -> str:
        first = ("We fire first." if team == "red"
                 else f"{HANDLE['red']} fires first.")
        options = _vote_options_for(state, team)
        return (f"⚓ NEW GAME — @battleshipred vs @battleshipblue!\n\n"
                f"Ships are placed. {first}\n\n"
                f"All-time: {record_line}\n\n"
                f"Reply a coordinate (e.g. B7) — our top {SHOTS_PER_TURN} "
                f"fire as one volley each turn.\n"
                f"Ideas: {_format_options(options)} {HASHTAG}")

    try:
        red_img, red_alt = _render_for(state, "red")
        blue_img, blue_alt = _render_for(state, "blue")
        state.red_last_post_uri = _post_image(
            "red", opening_text("red"), red_img, red_alt, "newgame", game_id, 1)
        state.blue_last_post_uri = _post_image(
            "blue", opening_text("blue"), blue_img, blue_alt, "newgame",
            game_id, 1)
    except Exception:
        logger.exception("Failed to post new-game announcements")
        # Save anyway so the game exists; the next tick can still fire
        # (the AI fallback runs when there are no posts to read votes from).

    db.save_state(state)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Log to LOG_PATH, and additionally to the screen when interactive.

    launchd redirects stdout into the same file named by LOG_PATH, so
    adding a stdout handler on top of the file handler writes every line
    twice. stdout is a TTY only when running in the foreground, which is
    exactly when the screen echo is wanted.
    """
    handlers = []
    log_path = os.environ.get("LOG_PATH")
    if log_path:
        handlers.append(logging.FileHandler(log_path))
    if not handlers or sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def login_with_retry() -> None:
    """Keep retrying Bluesky logins so a boot with no network (router
    still coming up, ISP outage) waits instead of crash-looping."""
    delay = 15
    while True:
        try:
            bluesky.login_all()
            return
        except Exception:
            logger.exception("Login failed (network down?); retrying in %ds", delay)
            time.sleep(delay)
            delay = min(delay * 2, 600)  # back off up to 10 minutes


def main() -> None:
    load_dotenv()
    setup_logging()
    logger.info("Battleship bot starting")

    db.init_db()
    login_with_retry()

    state = db.load_state()
    if state is None or state.status != "active":
        start_new_game()

    scheduler.add_job(game_tick, "interval", seconds=TICK_SECONDS,
                      id="game_tick", coalesce=True, max_instances=1)
    logger.info("Scheduler started; one team acts every %d seconds "
                "(each team every %d min); battlelog posts game results only",
                TICK_SECONDS, TURN_MINUTES)
    scheduler.start()


if __name__ == "__main__":
    main()
