"""AT Protocol wrapper for all three Bluesky accounts.

`team` is 'red', 'blue', or 'log' throughout. Also home to coordinate
parsing, vote tallying, and richtext facet building, since all three
operate on post text.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass

from atproto import Client, models

logger = logging.getLogger(__name__)

_clients = {}

# Maps a bare name (no '@', lowercased) to a DID, so mentions in post
# text become real clickable links. Populated at login with both the
# full handle ('battleshipred.bsky.social') and the short form the post
# text actually uses ('battleshipred').
_did_by_name = {}

_ENV_PREFIX = {"red": "RED", "blue": "BLUE", "log": "LOG"}

POST_LIMIT = 300  # Bluesky's per-post grapheme limit


def login_all() -> None:
    """Authenticate all three accounts and learn their DIDs."""
    for team, prefix in _ENV_PREFIX.items():
        handle = os.environ[f"{prefix}_HANDLE"]
        client = Client()
        profile = client.login(handle, os.environ[f"{prefix}_APP_PASSWORD"])
        _clients[team] = client
        did = profile.did
        _did_by_name[handle.lower()] = did
        # 'battleshipred.bsky.social' -> also register 'battleshipred',
        # which is the short form used in the post copy.
        _did_by_name[handle.split(".", 1)[0].lower()] = did
        logger.info("Logged in as %s (%s) did=%s", handle, team, did)


def login(team: str) -> None:
    """Authenticate a single account (used by tools that don't need all three)."""
    prefix = _ENV_PREFIX[team]
    handle = os.environ[f"{prefix}_HANDLE"]
    client = Client()
    profile = client.login(handle, os.environ[f"{prefix}_APP_PASSWORD"])
    _clients[team] = client
    _did_by_name[handle.lower()] = profile.did
    _did_by_name[handle.split(".", 1)[0].lower()] = profile.did
    logger.info("Logged in as %s (%s)", handle, team)


def get_did(team: str) -> str:
    return _clients[team].me.did


def logged_in(team: str) -> bool:
    return team in _clients


# ---------------------------------------------------------------------------
# Richtext facets — what makes @mentions and #hashtags clickable
# ---------------------------------------------------------------------------

# Handle-ish run after '@': starts and ends alphanumeric so trailing
# punctuation ('@battleshipred:' / '@battlelog.') is left out of the link.
_MENTION_RE = re.compile(r"@([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_TAG_RE = re.compile(r"(?<![\w#])#([A-Za-z0-9_]+)")


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def build_facets(text: str, extra_dids: dict | None = None) -> list:
    """Build mention/tag facets for `text`.

    Facet ranges are UTF-8 **byte** offsets, not character offsets — the
    post copy is full of multi-byte emoji (🔴 ⚓ 🏆), so offsets are
    computed by encoding the prefix rather than using string indices.

    `extra_dids` supplies handles beyond the three bot accounts (e.g. the
    follower being credited for a shot), as {handle: did}.
    """
    lookup = dict(_did_by_name)
    for handle, did in (extra_dids or {}).items():
        lookup[handle.lstrip("@").lower()] = did

    facets = []
    for match in _MENTION_RE.finditer(text):
        did = lookup.get(match.group(1).lower())
        if did is None:
            continue  # unknown handle: leave as plain text rather than guess
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Mention(did=did)],
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=_byte_len(text[: match.start()]),
                    byte_end=_byte_len(text[: match.end()]),
                ),
            )
        )

    for match in _TAG_RE.finditer(text):
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Tag(tag=match.group(1))],
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=_byte_len(text[: match.start()]),
                    byte_end=_byte_len(text[: match.end()]),
                ),
            )
        )
    return facets


def clamp(text: str, limit: int = POST_LIMIT) -> str:
    """Trim text to Bluesky's post limit. Facets are always built from
    the clamped text, so a mention cut in half simply loses its link
    rather than pointing past the end of the post."""
    if len(text) <= limit:
        return text
    logger.warning("Post text over %d chars; trimming", limit)
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def post_with_image(team: str, text: str, image_bytes: bytes,
                    alt: str | None = None,
                    extra_dids: dict | None = None) -> str:
    """Post text + board image; return the AT URI."""
    text = clamp(text)
    return _clients[team].send_image(
        text=text,
        image=image_bytes,
        image_alt=clamp(alt or "Battleship board.", 1000),
        facets=build_facets(text, extra_dids),
    ).uri


def post_text(team: str, text: str, extra_dids: dict | None = None) -> str:
    """Post a text-only post; return the AT URI."""
    text = clamp(text)
    return _clients[team].send_post(
        text=text, facets=build_facets(text, extra_dids)
    ).uri


def post_reply(team: str, text: str, parent_uri: str, parent_cid: str,
               root_uri: str | None = None, root_cid: str | None = None,
               extra_dids: dict | None = None) -> str:
    """Reply to an existing post — used to credit the follower whose
    coordinate won, which sends them a notification."""
    text = clamp(text)
    parent = models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid)
    if root_uri and root_cid:
        root = models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid)
    else:
        root = parent
    return _clients[team].send_post(
        text=text,
        facets=build_facets(text, extra_dids),
        reply_to=models.AppBskyFeedPost.ReplyRef(parent=parent, root=root),
    ).uri


def get_replies(post_uri: str) -> list:
    """Direct replies to a post. Items expose .author.did/.handle,
    .record.text, .uri and .cid."""
    client = _clients.get("log") or next(iter(_clients.values()))
    response = client.app.bsky.feed.get_post_thread({"uri": post_uri, "depth": 1})
    replies = getattr(response.thread, "replies", None) or []
    return [r.post for r in replies if getattr(r, "post", None) is not None]


# ---------------------------------------------------------------------------
# Enumerating and deleting posts (used by the reset tool)
# ---------------------------------------------------------------------------

POST_COLLECTION = "app.bsky.feed.post"
_DELETE_BATCH = 50          # applyWrites allows more; smaller = finer progress
_RATE_LIMIT_PAUSE = 60      # seconds to wait out a 429 before resuming


def rkey_from_uri(uri: str) -> str:
    """'at://did:plc:x/app.bsky.feed.post/3abc' -> '3abc'."""
    return uri.rsplit("/", 1)[-1] if uri else ""


def list_all_posts(team: str) -> list:
    """Every post in this account's repo, oldest first.

    Walks com.atproto.repo.listRecords with a cursor (100 per page). This
    sees the account's actual contents, including posts made before the
    bot started logging them.
    """
    client = _clients[team]
    did = client.me.did
    out, cursor = [], None
    while True:
        page = client.com.atproto.repo.list_records({
            "repo": did,
            "collection": POST_COLLECTION,
            "limit": 100,
            "cursor": cursor,
        })
        for record in page.records:
            value = record.value
            out.append({
                "uri": record.uri,
                "cid": record.cid,
                "rkey": rkey_from_uri(record.uri),
                "text": getattr(value, "text", "") or "",
                "created_at": getattr(value, "created_at", "") or "",
            })
        cursor = getattr(page, "cursor", None)
        if not cursor or not page.records:
            break
    out.sort(key=lambda p: p["created_at"])
    return out


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def delete_posts(team: str, rkeys: list, on_progress=None) -> tuple:
    """Delete posts by record key, in batches.

    Returns (deleted_rkeys, failed_rkeys). A rate limit is treated as
    normal: the call pauses and retries rather than aborting and leaving
    the account half-cleaned. `on_progress(done, total)` is called after
    each batch so a caller can show progress.
    """
    client = _clients[team]
    did = client.me.did
    deleted, failed = [], []
    total = len(rkeys)

    index = 0
    while index < total:
        batch = rkeys[index:index + _DELETE_BATCH]
        writes = [
            models.ComAtprotoRepoApplyWrites.Delete(
                collection=POST_COLLECTION, rkey=rkey)
            for rkey in batch
        ]
        try:
            client.com.atproto.repo.apply_writes({
                "repo": did, "writes": writes,
            })
            deleted.extend(batch)
            index += len(batch)
        except Exception as exc:
            if _is_rate_limited(exc):
                logger.warning("Rate limited deleting posts; pausing %ds",
                               _RATE_LIMIT_PAUSE)
                time.sleep(_RATE_LIMIT_PAUSE)
                continue  # retry the same batch
            # A single bad record (already gone, say) shouldn't stop the
            # run — fall back to deleting this batch one at a time.
            logger.warning("Batch delete failed (%s); retrying individually",
                           exc)
            for rkey in batch:
                try:
                    client.com.atproto.repo.delete_record({
                        "repo": did, "collection": POST_COLLECTION,
                        "rkey": rkey,
                    })
                    deleted.append(rkey)
                except Exception as inner:
                    if _is_rate_limited(inner):
                        time.sleep(_RATE_LIMIT_PAUSE)
                        continue
                    logger.warning("Could not delete %s: %s", rkey, inner)
                    failed.append(rkey)
            index += len(batch)
        if on_progress:
            on_progress(len(deleted) + len(failed), total)
    return deleted, failed


# ---------------------------------------------------------------------------
# Coordinate parsing and vote tallying
# ---------------------------------------------------------------------------

# Matches A5, A-5, A 5, a5, A,5 — a row letter A-J followed (with an
# optional single separator) by a column 1-10. Guards: not preceded by a
# letter (so "sea5" doesn't match) and not followed by another digit (so
# "A55" and the "A1" inside "A100" don't match).
_COORD_RE = re.compile(r"(?<![A-Za-z])([A-Ja-j])\s?[-,]?\s?(10|[1-9])(?!\d)")

# A/B/C choice matching, tried in this order. The whole difficulty is that
# "a" is an ordinary English word — "a good shot" must not read as a vote for
# option A — while "b" and "c" essentially never are. So a bare lowercase "a"
# only counts with a stronger signal (alone, punctuated, or after a voting
# word), whereas "b"/"c" are trusted at the start of a reply.
_OPTION_PATTERNS = (
    # 1. the whole reply is the letter: "a", "B", "c!", "b)"
    re.compile(r"^\s*([ABCabc])\s*[^A-Za-z0-9]*$"),
    # 2. the letter opens the reply and is punctuated: "a) yes", "b. go"
    re.compile(r"^\s*([ABCabc])\s*[).:;,!—-]"),
    # 3. after a voting word: "I vote b", "option C", "let's go with a"
    re.compile(
        r"\b(?:vote|votes|voting|option|choice|choose|pick|go|going)\b"
        r"(?:\s+(?:for|with|on|is))?"
        r"[^A-Za-z0-9]{0,4}([ABCabc])(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
    # 4. b/c opening a sentence: "b please" (deliberately excludes "a")
    re.compile(r"^\s*([BCbc])(?![A-Za-z0-9])"),
    # 5. a standalone CAPITAL letter anywhere: "I think B", never "a shot"
    re.compile(r"(?<![A-Za-z0-9])([ABC])(?![A-Za-z0-9])"),
)


def parse_coordinate(text: str) -> tuple | None:
    """Extract the first coordinate from reply text: 'b 7!' -> ('B', 7)."""
    match = _COORD_RE.search(text or "")
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def parse_option(text: str) -> str | None:
    """Find an A/B/C choice in reply text, or None. Returns 'A', 'B' or 'C'."""
    for pattern in _OPTION_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1).upper()
    return None


def parse_vote_text(text: str, options: dict | None = None) -> tuple | None:
    """Extract a vote as a coordinate.

    Direct coordinates still work, but when a post offers A/B/C choices a
    reply of just "B" maps to that option's coordinate.
    """
    coord = parse_coordinate(text)
    if coord is not None:
        return coord
    if not options:
        return None
    label = parse_option(text)
    return options.get(label) if label else None


@dataclass
class VoteResult:
    coord: tuple            # ('B', 7)
    count: int              # votes for the winning coordinate
    total_voters: int       # distinct followers who cast a valid vote
    caller_did: str = ""    # first follower to call the winning coordinate
    caller_handle: str = ""
    caller_uri: str = ""    # their reply, so the bot can reply to it
    caller_cid: str = ""
    root_uri: str = ""      # thread root, for a well-formed reply
    root_cid: str = ""
    choice_label: str = ""  # A/B/C when the winning vote used a choice


def _created_at(reply) -> str:
    return getattr(getattr(reply, "record", None), "created_at", "") or ""


def collect_votes(replies: list, already_fired: set,
                  options: dict | None = None) -> tuple:
    """Reduce replies to valid votes: (votes_by_did, first_reply_by_coord).

    `already_fired` holds ('A', 5)-style tuples. Replies are processed
    oldest first, so a voter's earliest reply is their vote and the
    follower recorded against a coordinate is whoever called it first.
    """
    votes, first, labels = {}, {}, {}
    for reply in sorted(replies, key=_created_at):
        try:
            did = reply.author.did
            text = reply.record.text
        except AttributeError:
            continue
        if did in votes:
            continue
        coord = parse_vote_text(text, options)
        if coord is None or coord in already_fired:
            continue
        votes[did] = coord
        first.setdefault(coord, reply)
        for label, option_coord in (options or {}).items():
            if coord == option_coord:
                labels.setdefault(coord, label)
                break
    return votes, first, labels


def vote_breakdown(replies: list, already_fired: set,
                   options: dict | None = None) -> list:
    """Every coordinate currently voted for, most votes first.

    Same reduction as tally_votes, exposed for the dashboard's live view
    so the two can never disagree about the standings.
    """
    votes, first, labels = collect_votes(replies, already_fired, options)
    out = []
    for coord, count in Counter(votes.values()).most_common():
        caller = first.get(coord)
        out.append({
            "coord": f"{coord[0]}{coord[1]}",
            "votes": count,
            "choice": labels.get(coord, ""),
            "first_caller": (getattr(getattr(caller, "author", None), "handle", "")
                             if caller else ""),
        })
    return out


def tally_votes(replies: list, already_fired: set,
                options: dict | None = None) -> VoteResult | None:
    """The winning coordinate, or None when no valid votes were cast."""
    votes, first, labels = collect_votes(replies, already_fired, options)
    if not votes:
        return None

    coord, count = Counter(votes.values()).most_common(1)[0]
    result = VoteResult(coord=coord, count=count, total_voters=len(votes))
    result.choice_label = labels.get(coord, "")

    caller = first.get(coord)
    if caller is not None:
        result.caller_did = getattr(caller.author, "did", "") or ""
        result.caller_handle = getattr(caller.author, "handle", "") or ""
        result.caller_uri = getattr(caller, "uri", "") or ""
        result.caller_cid = getattr(caller, "cid", "") or ""
        # Prefer the reply's own thread root so the credit reply threads
        # correctly; fall back to the reply itself if it isn't exposed.
        root = getattr(getattr(caller.record, "reply", None), "root", None)
        result.root_uri = getattr(root, "uri", "") or result.caller_uri
        result.root_cid = getattr(root, "cid", "") or result.caller_cid
    return result
