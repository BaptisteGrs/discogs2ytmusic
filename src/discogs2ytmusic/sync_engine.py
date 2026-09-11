from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence

from ytmusicapi import YTMusic

from . import matcher, store


def rematch_track(
    conn: sqlite3.Connection,
    yt: YTMusic,
    release: sqlite3.Row,
    tracks: Sequence[sqlite3.Row],
    track_id: int | None,
    artist: str,
    title: str,
    discogs_matches: dict[int, matcher.MatchResult] | None = None,
) -> None:
    """Search for and cache a fresh match for one (artist, title) query belonging to `release`.

    Resolution order: a confident hit among `release`'s own Discogs-embedded videos
    (`matcher.match_against_discogs_videos`), with a best-effort channel lookup, else a
    YouTube/YT Music search (`matcher.find_match`). Overwrites whatever's cached for this
    query now — callers that want to preserve an existing match should check for one first
    (`ensure_matches` does; a single-row "refetch" action is expected to have already deleted
    the stale one via `store.delete_match`).

    Pass `discogs_matches` — the release-wide result of `matcher.match_against_discogs_videos`
    — when a caller already scored the whole release's tracks against its embedded videos
    (as `ensure_matches` does once per release); omit it to have it computed fresh here, over
    all of `tracks`, so the same greedy cross-track video assignment (each video used by at
    most one track) still holds for a single-track call.
    """
    if discogs_matches is None:
        queries = store.effective_track_queries(release, tracks)
        videos = json.loads(release["videos"] or "[]")
        discogs_matches = matcher.match_against_discogs_videos(videos, queries)

    if track_id is not None and track_id in discogs_matches:
        result = discogs_matches[track_id]
        channel = matcher.resolve_channel(result.video_id) if result.video_id else None
    else:
        result = matcher.find_match(yt, artist, title)
        channel = result.channel

    store.save_match(
        conn, artist, title, result.video_id, result.video_title, result.source, result.score, channel=channel
    )
    conn.commit()


def ensure_matches(
    conn: sqlite3.Connection,
    yt: YTMusic,
    releases_with_tracks: Iterable[tuple[sqlite3.Row, Sequence[sqlite3.Row]]],
    on_track_done: Callable[[], None] | None = None,
) -> None:
    """Make sure every track across the given (release, tracks) pairs has a cached match.

    Skips any query that already has a cached match; a fresh lookup for the rest is
    delegated to `rematch_track` (see its docstring for the resolution order). Commits
    after each track that actually gets (re)searched, so a crash/interrupt doesn't lose
    earlier progress. `on_track_done` — if given — is called exactly once per (track_id,
    artist, title) query yielded across all the given releases, whether or not it needed a
    fresh lookup; a caller can use it to drive a progress bar sized to the same count.
    """
    for release, tracks in releases_with_tracks:
        queries = store.effective_track_queries(release, tracks)
        videos = json.loads(release["videos"] or "[]")
        discogs_matches = matcher.match_against_discogs_videos(videos, queries)

        seen: set[tuple[str, str]] = set()
        for track_id, artist, title in queries:
            if (artist, title) in seen:
                if on_track_done:
                    on_track_done()
                continue
            seen.add((artist, title))

            if store.get_match(conn, artist, title) is not None:
                if on_track_done:
                    on_track_done()
                continue

            rematch_track(conn, yt, release, tracks, track_id, artist, title, discogs_matches=discogs_matches)
            if on_track_done:
                on_track_done()
