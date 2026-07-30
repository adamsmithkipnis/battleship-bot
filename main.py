"""Orchestrator: APScheduler loop driving the crowdsourced Battleship game.

Teams alternate on a half-interval stagger: with TURN_MINUTES=10, Red acts
at 0:00, Blue at 0:05, Red at 0:10, and so on. Each tick exactly one team
fires and only that team posts, so each account posts once every
TURN_MINUTES and each side gets an equal voting window.

Every tick: read votes from the acting team's latest post, fire the
most-voted coordinate (AI fallback if no valid votes), credit the follower
who called it, and post a terse summary to the battlelog account. A weekly
leaderboard runs from battlelog.
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

logger = logging.getLogger("battleship")

# Minutes between a given team's own turns. Teams alternate on half of
# this, so each account posts every TURN_MINUTES and the two accounts are
# offset from each other by TURN_MINUTES / 2.
TURN_MINUTES = int(os.environ.get("TURN_MINUTES", "10"))
TICK_SECONDS = TURN_MINUTES * 60 // 2

HASHTAG = "#Battleship"
EMOJI = {"red": "🔴", "blue": "🔵"}
HANDLE = {"red": "@battleshipred", "blue": "@battleshipblue"}
TEAM_NAME = {"red": "Team Red", "blue": "Team Blue"}

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


def _credit_line(vote) -> str:
    """Tell followers whether the crowd or the AI picked this shot.

    Naming the caller is the point: it makes a follower's influence
    visible to everyone, and the matching reply notifies them directly.
    """
    if vote is None:
        return "🤖 No votes came in — we auto-fired."
    if vote.caller_handle:
        return (f"🎯 Called by @{vote.caller_handle} "
                f"({vote.count} of {vote.total_voters} votes)")
    return f"🎯 Crowd pick: {vote.count} of {vote.total_voters} votes"


def build_incoming_line(opponent: str, coord: str, result: str,
                        ship: str) -> str:
    """What the opponent did to us on the previous tick.

    Because only the acting team posts, this is how a team's followers
    learn about the shot they took while their account was quiet.
    """
    prefix = f"Incoming from {HANDLE[opponent]}: {coord} —"
    if result == "miss":
        return f"{prefix} Miss."
    if result.startswith("sunk:"):
        return f"{prefix} HIT, our {ship} is sunk!"
    return f"{prefix} HIT on our {ship}."


def build_turn_text(team: str, turn: int, coord: str, result: str,
                    sunk_ship: str | None, vote, incoming: str = "") -> str:
    """The acting team's post: what happened to us, then our shot."""
    if result == "miss":
        own = f"We fired {coord} — Miss."
    elif sunk_ship:
        own = f"We fired {coord} — HIT! Their {sunk_ship} is sunk!"
    else:
        own = f"We fired {coord} — HIT!"

    head = f"Turn {turn} {EMOJI[team]} "
    body = f"{head}{incoming}\n{own}" if incoming else f"{head}{own}"
    return (f"{body}\n{_credit_line(vote)}\n"
            f"Reply a coord to vote our next shot (e.g. D4). "
            f"{TURN_MINUTES} min. {HASHTAG}")


def build_log_text(firing_team: str, turn: int, coord: str, result: str,
                   sunk_ship: str | None, vote) -> str:
    """Terse neutral battlelog summary for one turn."""
    if result == "miss":
        outcome = "Miss."
    else:
        outcome = "HIT" + (f" — {sunk_ship} sunk!" if sunk_ship else "")
    if vote is None:
        credit = "No votes — AI shot."
    elif vote.caller_handle:
        credit = (f"Called by @{vote.caller_handle} "
                  f"({vote.count} of {vote.total_voters} votes)")
    else:
        credit = f"Crowd pick ({vote.count} of {vote.total_voters} votes)"
    return (f"Turn {turn} | {HANDLE[firing_team]} fires {coord} — {outcome}\n"
            f"{credit}\n"
            f"{HANDLE[_opponent(firing_team)]} to respond. {HASHTAG}")


def build_credit_reply(coord: str, result: str, sunk_ship: str | None,
                       turn: int, vote) -> str:
    """Reply sent to the follower whose coordinate was fired."""
    if result == "miss":
        outcome = "a miss"
    elif sunk_ship:
        outcome = f"a HIT — you sank their {sunk_ship}!"
    else:
        outcome = "a HIT!"
    return (f"🎯 Your call. We fired {coord} on turn {turn} — {outcome}\n\n"
            f"({vote.count} of {vote.total_voters} votes) {HASHTAG}")


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


def build_leaderboard_text(entries: list, days: int = 7) -> str:
    """Weekly top-callers roundup, assembled to fit the post limit."""
    header = f"📊 TOP GUNNERS — last {days} days\n"
    footer = (f"\nReply a coordinate on @battleshipred or @battleshipblue "
              f"to get on the board. {HASHTAG}")
    def plural(n: int, word: str) -> str:
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    lines = []
    budget = bluesky.POST_LIMIT - len(header) - len(footer)
    for i, entry in enumerate(entries, 1):
        bits = [f"{entry['sinks']} sunk"] if entry["sinks"] else []
        bits.append(plural(entry["hits"], "hit"))
        bits.append(plural(entry["calls"], "shot"))
        line = f"{i}. @{entry['handle']} — {', '.join(bits)}"
        if len("\n".join(lines + [line])) > budget:
            break
        lines.append(line)
    return header + "\n".join(lines) + footer


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

    # 1. Collect and tally votes from the acting team's latest post — the
    #    post that has been up for TURN_MINUTES asking for exactly this.
    vote = bluesky.tally_votes(_fetch_replies(vote_uri),
                               _already_fired(state, team))

    if vote is not None:
        row_letter, col = vote.coord
        r, c = game.coord_to_index(row_letter, col)
        logger.info("Team %s move by vote: %s%d (%d of %d votes, called by %s)",
                    team, row_letter, col, vote.count, vote.total_voters,
                    vote.caller_handle or "unknown")
    else:
        _, _, firing_view, opp_ships = _views(state, team)
        r, c = ai.choose_move(firing_view, opp_ships)
        logger.info("Team %s move by AI fallback: %s", team,
                    game.index_to_coord(r, c))

    coord = game.index_to_coord(r, c)
    turn = state.turn_number

    # The opponent's shot from the previous tick, reported in this post
    # before it gets overwritten below.
    incoming = ""
    if state.last_shot_team == opponent and state.last_shot_coord:
        incoming = build_incoming_line(opponent, state.last_shot_coord,
                                       state.last_shot_result,
                                       state.last_shot_ship)

    # 2. Apply the shot.
    state, result = game.fire(state, team, r, c)
    if result == "already_fired":
        # Shouldn't happen (votes and AI both skip fired cells), but if it
        # does, skip this tick rather than posting a bogus update.
        logger.error("Duplicate shot at %s by team %s; skipping tick", coord, team)
        return

    sunk_ship = result.split(":", 1)[1] if result.startswith("sunk:") else None
    is_hit = result != "miss"
    hit_ship = None
    if is_hit:
        target_ships = state.blue_ships if team == "red" else state.red_ships
        for ship in target_ships:
            if (r, c) in [tuple(cell) for cell in ship["cells"]]:
                hit_ship = ship["name"]
                break

    # 3. Win check.
    won = is_hit and game.check_win(state, team)
    if won:
        state.status = f"{team}_won"
        db.record_win(team, turn, state.game_id)
        logger.info("Team %s wins game %s in %d turns", team, state.game_id, turn)

    # 4. Post. If posting fails, state is NOT saved — the next tick re-reads
    #    the same post's replies and retries the turn.
    #    Normally only the acting team posts, which is what staggers the two
    #    accounts. A win is the exception: both accounts announce the result.
    extra = {}
    if vote is not None and vote.caller_handle and vote.caller_did:
        # The credited handle needs its DID to become a clickable mention.
        extra[vote.caller_handle] = vote.caller_did

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
        else:
            img, alt = _render_for(state, team)
            text = build_turn_text(team, turn, coord, result, sunk_ship,
                                   vote, incoming)
            uri = _post_image(team, text, img, alt, "turn", state.game_id,
                              turn, extra_dids=extra)
            if team == "red":
                state.red_last_post_uri = uri
            else:
                state.blue_last_post_uri = uri
            log_text = build_log_text(team, turn, coord, result, sunk_ship, vote)

        state.log_last_post_uri = _post_text("log", log_text, "log",
                                             state.game_id, turn,
                                             extra_dids=extra)
    except Exception:
        logger.exception("Posting failed; skipping turn (state not saved)")
        return

    state.last_shot_team = team
    state.last_shot_coord = coord
    state.last_shot_result = result
    state.last_shot_ship = hit_ship or ""

    # 5. Credit the caller and log their stats. Both are non-essential, so a
    #    failure here must not roll back a turn that has already posted.
    if vote is not None and vote.caller_did:
        try:
            db.record_call(vote.caller_did, vote.caller_handle, team, coord,
                           result, vote.count, state.game_id, turn)
        except Exception:
            logger.exception("Failed to record voter stats")
        if vote.caller_uri and vote.caller_cid:
            try:
                reply_uri = bluesky.post_reply(
                    team,
                    build_credit_reply(coord, result, sunk_ship, turn, vote),
                    parent_uri=vote.caller_uri, parent_cid=vote.caller_cid,
                    root_uri=vote.root_uri, root_cid=vote.root_cid,
                )
                _remember(team, reply_uri, "credit", state.game_id, turn)
            except Exception:
                logger.exception("Failed to post credit reply")

    # 6. Persist and hand the turn over.
    if not won:
        state.active_team = opponent
        state.turn_number = turn + 1
    db.save_state(state)

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

    # Both accounts open the game: each needs a post of its own for its
    # followers to reply to. From here on they alternate.
    def opening_text(team: str) -> str:
        first = ("We fire first." if team == "red"
                 else f"{HANDLE['red']} fires first.")
        return (f"⚓ NEW GAME — @battleshipred vs @battleshipblue!\n\n"
                f"Ships are placed. {first}\n\n"
                f"All-time: {record_line}\n\n"
                f"Reply a coordinate (e.g. B7) to vote for our shots. "
                f"{HASHTAG}")

    log_text = (f"⚓ GAME {game_id} UNDERWAY\n"
                f"@battleshipred vs @battleshipblue\n"
                f"@battleshipred fires first.\n\n"
                f"All-time: {record_line} {HASHTAG}")

    try:
        red_img, red_alt = _render_for(state, "red")
        blue_img, blue_alt = _render_for(state, "blue")
        state.red_last_post_uri = _post_image(
            "red", opening_text("red"), red_img, red_alt, "newgame", game_id, 1)
        state.blue_last_post_uri = _post_image(
            "blue", opening_text("blue"), blue_img, blue_alt, "newgame",
            game_id, 1)
        state.log_last_post_uri = _post_text("log", log_text, "newgame",
                                             game_id, 1)
    except Exception:
        logger.exception("Failed to post new-game announcements")
        # Save anyway so the game exists; the next tick can still fire
        # (the AI fallback runs when there are no posts to read votes from).

    db.save_state(state)


def post_leaderboard() -> None:
    """Weekly top-callers roundup from the battlelog account."""
    try:
        entries = db.get_leaderboard(days=7, limit=5)
        if not entries:
            logger.info("No voter calls in the last 7 days; skipping leaderboard")
            return
        text = build_leaderboard_text(entries, days=7)
        # Pass every DID so each handle on the board is clickable.
        extra = {e["handle"]: e["did"] for e in entries if e.get("handle")}
        state = db.load_state()
        _post_text("log", text, "leaderboard",
                   state.game_id if state else 0, extra_dids=extra)
        logger.info("Posted leaderboard with %d entries", len(entries))
    except Exception:
        logger.exception("post_leaderboard failed")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    log_path = os.environ.get("LOG_PATH")
    if log_path:
        handlers.append(logging.FileHandler(log_path))
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
    scheduler.add_job(post_leaderboard, "cron", day_of_week="sun", hour=18,
                      minute=0, id="leaderboard", coalesce=True)
    logger.info("Scheduler started; one team acts every %d seconds "
                "(each team every %d min), leaderboard Sundays at 18:00",
                TICK_SECONDS, TURN_MINUTES)
    scheduler.start()


if __name__ == "__main__":
    main()
