# Battleship Bluesky Bot

A game of Battleship played across three Bluesky accounts, where the shots are
chosen by followers voting in the replies.

- [@battleshipred](https://bsky.app/profile/battleshipred.bsky.social) — Team Red
- [@battleshipblue](https://bsky.app/profile/battleshipblue.bsky.social) — Team Blue
- [@battlelog](https://bsky.app/profile/battlelog.bsky.social) — neutral play-by-play

Teams alternate on a stagger: with `TURN_MINUTES=10`, Red fires at 0:00, Blue at
0:05, Red at 0:10. Each tick, the acting team reads the replies to its own most
recent post, fires the most-voted coordinate, and posts an updated board. If no
valid votes arrive, a hunt/target AI takes the shot and the post says so.

The follower who called the winning coordinate is named in the post and gets a
reply crediting them. First to sink all five enemy ships wins; the bot announces
the result with the all-time record, waits an hour, and starts a fresh game.

## Layout

| File | Purpose |
| --- | --- |
| `main.py` | Orchestrator and APScheduler loop |
| `game.py` | Battleship rules, ship placement, win detection (pure logic) |
| `ai.py` | Fallback move logic (hunt/target) |
| `renderer.py` | Board PNG generation and screen-reader alt text |
| `bluesky.py` | AT Protocol wrapper, richtext facets, vote tallying, post deletion |
| `db.py` | SQLite schema, game state, voter stats, reset helpers |
| `dashboard.py` | Read-only local web dashboard |
| `reset.py` | Wipe posts and/or local data for a clean slate |
| `com.battleship.bot.plist` | launchd config for the bot |
| `com.battleship.dashboard.plist` | launchd config for the dashboard |

Both plists ship with `/ABSOLUTE/PATH/TO/` placeholders you must edit.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in handles, app passwords, and paths
python3 main.py           # runs in the foreground; Ctrl+C to stop
```

Use Bluesky **app passwords** (Settings → Privacy and Security → App Passwords),
never the accounts' real passwords. `.env` is gitignored and must stay that way.

### Run as a daemon (macOS)

Edit the paths in `com.battleship.bot.plist`, then:

```bash
cp com.battleship.bot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.battleship.bot.plist
launchctl list | grep battleship          # a PID means it's running
launchctl kickstart -k gui/$(id -u)/com.battleship.bot   # restart after a code change
```

A LaunchAgent only runs while you're logged in, so for unattended operation
enable automatic login, disable sleep, and turn on "start up automatically after
a power failure."

## Dashboard

```bash
python3 dashboard.py      # http://127.0.0.1:8765
```

Shows both boards, the live vote tally with a countdown to when voting closes,
whose turn it is, ships remaining, the win/loss record, the voter leaderboard,
and a log tail.

It is strictly read-only: the database is opened with `PRAGMA query_only`, and
the live tally is read from Bluesky's public AppView, so **it needs no
credentials**. Running it cannot disturb a game in progress.

The board images reveal both fleets' ship positions, so keep it on localhost or
reach it over Tailscale — do not port-forward it. `DASHBOARD_HOST` and
`DASHBOARD_PORT` override the defaults.

## Resetting

```bash
python3 reset.py --dry-run        # report only, changes nothing
python3 reset.py --all --restart  # delete all posts, wipe local data, restart
python3 reset.py --db             # local data only, keep the posts
python3 reset.py --all --keep-record   # ...but preserve the W/L counters
```

Deleting posts is irreversible, and requires typing `delete` to confirm. Two
things to know:

- Followers' replies live in their own repos and cannot be deleted here. They
  remain on their profiles as replies to a missing post.
- Stop the bot first. Deleting the post it is waiting on makes it silently fall
  back to AI shots, since a failed reply fetch reads as "no votes."

`--all` zeroes the all-time W/L record by default, so a first real game doesn't
open with scores left over from testing.

## Configuration

| Variable | Meaning |
| --- | --- |
| `RED_HANDLE` / `RED_APP_PASSWORD` | Team Red credentials (same for `BLUE_`, `LOG_`) |
| `DB_PATH` | Absolute path to the SQLite database |
| `LOG_PATH` | Absolute path to the log file |
| `TURN_MINUTES` | Minutes between a team's own turns (default 10); teams alternate on half of this |
| `RESTART_DELAY_SECONDS` | Pause between games (default 3600) |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | Dashboard bind address (default `127.0.0.1:8765`) |

## Notes on the implementation

**Mentions and hashtags need explicit facets.** `send_post` only generates
richtext facets when handed a `TextBuilder`; a plain string produces none, which
renders `@handle` as inert text with no link and no notification. Every post
routes through `bluesky.build_facets()`, which computes UTF-8 **byte** offsets —
the post copy is full of emoji, so character indices would be wrong.

**Turn state survives restarts.** Everything lives in SQLite, so a crash,
reboot, or power cut resumes the same game. Logins retry with backoff so booting
before the network is up waits instead of crash-looping.

**A failed post does not advance the game.** State is saved only after posting
succeeds, so a network blip replays the same turn from the same reply thread on
the next tick.

**WAL mode** is enabled on the database so the dashboard can read while the bot
writes. Note that a `file:...?mode=ro` connection *fails* against a WAL database
("unable to open database file") because it cannot create the shared-memory
index — `PRAGMA query_only=1` is the working equivalent. Keep the database on
local disk; WAL does not work over network filesystems.

**Voter stats are an append-only event log** (`voter_calls`), so weekly,
all-time, per-game, and per-player views are all derivable without schema
changes. The weekly leaderboard posts from `@battlelog` on Sundays at 18:00.
