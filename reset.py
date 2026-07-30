"""Wipe the game back to a clean slate — for testing.

Deletes the bot's posts from Bluesky and/or clears the local database, so
the accounts look untouched to anyone browsing them.

    python3 reset.py --dry-run          # show what would happen, change nothing
    python3 reset.py --db               # clear local data only, keep posts
    python3 reset.py --posts            # delete Bluesky posts only
    python3 reset.py --all              # both (the usual testing reset)
    python3 reset.py --all --keep-record   # ...but preserve the W/L record

Deleting posts is irreversible. Two things worth knowing:
  * Followers' replies live in their own repos and cannot be deleted here.
    They'll remain on their profiles as replies to a missing post.
  * Stop the bot first. If it posts mid-wipe you'll be left with orphans,
    and deleting the post it is waiting on makes it fall back to AI shots.

The delete logic lives in bluesky.delete_posts / db.reset_*, so the
dashboard can offer the same operations behind a button later.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from dotenv import load_dotenv

import bluesky
import db

TEAMS = ("red", "blue", "log")
SERVICE = "com.battleship.bot"


def bot_is_running() -> bool:
    """True if the launchd job reports a live PID."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if SERVICE in line:
            pid = line.split("\t")[0].strip()
            return pid.isdigit()
    return False


def stop_bot() -> bool:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{SERVICE}"],
        capture_output=True, text=True)
    return result.returncode == 0


def start_bot() -> bool:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{SERVICE}"],
        capture_output=True, text=True)
    return result.returncode == 0


def survey() -> dict:
    """What's out there right now, per account."""
    found = {}
    for team in TEAMS:
        bluesky.login(team)
        posts = bluesky.list_all_posts(team)
        found[team] = posts
        print(f"  {team:>5}: {len(posts)} post(s) in the repo")
        if posts:
            first, last = posts[0], posts[-1]
            print(f"         oldest {first['created_at'][:19]} "
                  f"{first['text'][:40]!r}")
            print(f"         newest {last['created_at'][:19]} "
                  f"{last['text'][:40]!r}")
    return found


def confirm(prompt: str, expected: str) -> bool:
    print(f"\n{prompt}")
    try:
        typed = input(f"Type {expected!r} to continue (anything else aborts): ")
    except (EOFError, KeyboardInterrupt):
        return False
    return typed.strip() == expected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset the Battleship bot to a clean slate.")
    parser.add_argument("--posts", action="store_true",
                        help="delete the accounts' posts on Bluesky")
    parser.add_argument("--db", action="store_true",
                        help="clear local game data (state, history, stats)")
    parser.add_argument("--all", action="store_true",
                        help="both --posts and --db")
    parser.add_argument("--keep-record", action="store_true",
                        help="preserve the all-time W/L counters")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; change nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation (use with care)")
    parser.add_argument("--restart", action="store_true",
                        help="start the bot again when finished")
    args = parser.parse_args()

    do_posts = args.posts or args.all
    do_db = args.db or args.all
    if not (do_posts or do_db or args.dry_run):
        parser.print_help()
        return 1

    load_dotenv()
    db.init_db()

    print("Battleship reset\n" + "=" * 40)
    running = bot_is_running()
    print(f"Bot currently running: {'YES' if running else 'no'}")

    tracked = db.count_posts()
    print(f"Posts the bot has logged locally: "
          f"{sum(tracked.values())} {tracked or ''}")

    remote = {}
    if do_posts or args.dry_run:
        print("\nChecking the accounts on Bluesky...")
        try:
            remote = survey()
        except Exception as exc:
            print(f"\nCould not read the accounts: {exc}")
            print("Check the credentials in .env, then try again.")
            return 1

    total_remote = sum(len(v) for v in remote.values())

    if args.dry_run:
        print("\n--- DRY RUN, nothing will change ---")
        print(f"Would delete {total_remote} post(s) across "
              f"{len([t for t, v in remote.items() if v])} account(s).")
        print("Would clear: game state, game history, voter stats, post log.")
        print(f"Would {'KEEP' if args.keep_record else 'ZERO'} the W/L record.")
        return 0

    if running:
        print("\nThe bot is running. It must be stopped first, or it will "
              "post again mid-wipe.")
        if not args.yes and not confirm("Stop the bot now?", "stop"):
            print("Aborted; nothing changed.")
            return 1
        print("Stopping the bot..." if stop_bot() else "Could not stop the bot.")

    if do_posts and total_remote:
        print(f"\nAbout to permanently delete {total_remote} post(s).")
        print("This cannot be undone. Replies from followers will remain on "
              "their own profiles.")
        if not args.yes and not confirm(
                "Confirm deletion of all posts on all three accounts.",
                "delete"):
            print("Aborted; nothing changed.")
            return 1

        for team, posts in remote.items():
            if not posts:
                continue
            rkeys = [p["rkey"] for p in posts]

            def progress(done, total, team=team):
                print(f"\r  {team}: {done}/{total} deleted", end="", flush=True)

            deleted, failed = bluesky.delete_posts(team, rkeys,
                                                   on_progress=progress)
            print(f"\r  {team}: {len(deleted)} deleted"
                  + (f", {len(failed)} failed" if failed else ""))
            db.forget_posts([p["uri"] for p in posts
                             if p["rkey"] in set(deleted)])

    if do_db:
        db.reset_all(keep_record=args.keep_record)
        print("\nLocal data cleared"
              + (" (W/L record preserved)." if args.keep_record
                 else " (W/L record zeroed)."))

    if args.restart:
        print("Restarting the bot..." if start_bot()
              else "Could not restart the bot.")
        print("A fresh game will begin and post within a minute.")
    else:
        print("\nDone. Start the bot when you're ready:")
        print("  launchctl kickstart -k gui/$(id -u)/com.battleship.bot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
