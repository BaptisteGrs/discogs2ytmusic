"""Make sure every track across a set of releases has a cached YouTube match.

`ensure_matches` is the shared orchestration layer between `matcher` (finds a match)
and `store` (caches it): factored out of `cli.py`'s `sync`/`rematch` so the UI's Sync
button and Rematch button drive the same match-finding logic. It only searches and
writes to the match cache — pushing matched tracks to a real YT Music playlist is a
separate concern, handled in `app.py`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence

from ytmusicapi import YTMusic

from . import matcher, store


def ensure_matches(
    conn: sqlite3.Connection,
    yt: YTMusic,
    releases_with_tracks: Iterable[tuple[sqlite3.Row, Sequence[sqlite3.Row]]],
    on_track_done: Callable[[], None] | None = None,
) -> None:
    """Make sure every track across the given (release, tracks) pairs has a cached match.

    Resolution order per track:
    1. an already-cached match — no-op
    2. a confident hit among the release's own Discogs-embedded videos
       (`matcher.match_against_discogs_videos`), with a best-effort channel lookup
    3. a YouTube/YT Music search (`matcher.find_match`)

    Commits after each track that actually gets (re)searched, so a crash/interrupt
    doesn't lose earlier progress. `on_track_done` — if given — is called exactly once
    per (track_id, artist, title) query yielded across all the given releases, whether
    or not it needed a fresh lookup; a caller can use it to drive a progress bar sized
    to the same count.
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
            if on_track_done:
                on_track_done()
