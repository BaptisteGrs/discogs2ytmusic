"""Match tracks against YouTube, and push/diff the results onto a real YT Music playlist.

`ensure_matches` is the shared orchestration layer between `matcher` (finds a match) and
`store` (caches it): factored out of `cli.py`'s `sync`/`rematch` so the UI's Sync/Rematch
buttons drive the same match-finding logic. `push_to_ytmusic` and friends are the other
half — diffing a playlist's matched video ids against what's actually on YT Music, adding
what's missing, removing what's stray, and recovering from a stale/deleted playlist id —
factored out of `app.py` so that logic isn't reimplemented by any future non-UI caller.
Both halves work off "this playlist's/release's tracks", never "the whole collection", so
neither needs to know anything about which Discogs source a track came from.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicError

from . import matcher, store, ytmusic_client
from .filters import TrackRow


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


def duplicate_video_groups(rows: list[TrackRow]) -> list[list[TrackRow]]:
    """Group matched rows that share the same YouTube video id — usually two different Discogs
    tracks accidentally matched to the same video, worth a second look before syncing (a shared
    video id can also make YT Music reject a whole add request if left undeduped downstream)."""
    by_video: dict[str, list[TrackRow]] = {}
    for r in rows:
        if r.video_id:
            by_video.setdefault(r.video_id, []).append(r)
    return [group for group in by_video.values() if len(group) > 1]


def stray_remote_tracks(remote_tracks: list[dict[str, Any]], video_ids: list[str]) -> list[dict[str, Any]]:
    """Remote playlist tracks whose video id isn't in the local track set — these get removed."""
    local = set(video_ids)
    return [t for t in remote_tracks if t.get("videoId") not in local]


def get_playlist_tracks_with_retry(yt: YTMusic, playlist_id: str) -> list[dict[str, Any]]:
    """`get_playlist_tracks`, retrying a couple of times on the transient "browse response missing
    expected keys" shape (see `YTMUSIC_MISSING_PLAYLIST_ERRORS`).

    A playlist that was just created via `get_or_create_playlist` isn't always immediately
    queryable — YT Music's backend appears to have a short server-side propagation delay before a
    brand-new playlist is indexed, during which `get_playlist` raises a bare KeyError/IndexError
    rather than returning real (if empty) contents. Retrying with backoff avoids treating that
    delay as a real failure; a KeyError/IndexError that persists past the retries is still raised,
    for the caller to handle as before.

    The backoff is deliberately short (well under a second total): long enough to ride out the
    propagation delay, but short enough not to itself trip Streamlit AppTest's script-run timeout
    in tests that exercise this path without mocking the delay away.
    """
    retries = 2
    for attempt in range(retries):
        try:
            return ytmusic_client.get_playlist_tracks(yt, playlist_id)
        except (KeyError, IndexError):
            time.sleep(0.25 * (attempt + 1))
    return ytmusic_client.get_playlist_tracks(yt, playlist_id)


def diff_and_sync_ytmusic(yt: YTMusic, ytmusic_id: str, video_ids: list[str]) -> list[dict[str, Any]]:
    """Push tracks missing on `ytmusic_id`, remove remote tracks no longer present locally, and
    return the removed tracks."""
    remote_tracks = get_playlist_tracks_with_retry(yt, ytmusic_id)
    remote_video_ids = {t["videoId"] for t in remote_tracks if t.get("videoId")}
    to_add = [v for v in video_ids if v not in remote_video_ids]
    to_remove = stray_remote_tracks(remote_tracks, video_ids)
    ytmusic_client.add_tracks(yt, ytmusic_id, to_add)
    ytmusic_client.remove_tracks(yt, ytmusic_id, to_remove)
    return to_remove


# ytmusicapi's `get_playlist` (used by `get_playlist_tracks`) doesn't raise a clean YTMusicError
# for a deleted/invalid playlist id — the browse response it gets back is just missing the keys
# a real playlist's response would have, and its internal `nav()` helper raises a bare KeyError
# (or IndexError, depending on the exact shape) instead. Both need to be treated the same as a
# YTMusicError for the stale-id recovery below to actually trigger on this failure mode.
YTMUSIC_MISSING_PLAYLIST_ERRORS: tuple[type[Exception], ...] = (YTMusicError, KeyError, IndexError)
YTMUSIC_PUSH_ERRORS: tuple[type[Exception], ...] = (RuntimeError, *YTMUSIC_MISSING_PLAYLIST_ERRORS)

# A bare KeyError/IndexError (see above) means "browse response missing expected keys" — that's
# also what a brand-new playlist looks like before YT Music finishes indexing it server-side (see
# `get_playlist_tracks_with_retry`), which has nothing to do with auth. Only RuntimeError/
# YTMusicError — errors ytmusicapi/`ytmusic_client` actually raise on rejection — are treated as
# evidence the saved session itself is bad; don't flip the UI to "cookie expired" on the ambiguous
# KeyError/IndexError shape alone.
YTMUSIC_AUTH_SUSPECT_ERRORS: tuple[type[Exception], ...] = (RuntimeError, YTMusicError)


def push_to_ytmusic(
    yt: YTMusic, playlist: sqlite3.Row, playlist_id: int, playlist_name: str, video_ids: list[str]
) -> list[dict[str, Any]]:
    """Create (or reuse) `playlist_name`'s linked YT Music playlist, then diff `video_ids`
    against what's actually on it — pushing what's missing and removing remote tracks no longer
    present locally — and return the removed tracks.

    If a playlist id was already saved locally but YT Music rejects requests against it — e.g. it
    was deleted on the YT Music side, or the id was never valid in the first place (cookie-based
    auth can "succeed" on `create_playlist` without a playlist actually existing server-side) —
    forget the stale id, create a fresh playlist once, and retry the diff against it, rather than
    failing on the same bad id forever. Re-raises if that retry also fails, or if there was no
    saved id to blame.

    Records `pushed_at` on every successful push — the first link and every later re-sync alike —
    since a linked playlist's `ytmusic_playlist_id` doesn't change on a re-sync and so can't be
    used on its own to tell when the playlist was last pushed.

    Takes `playlist`/`playlist_id`/`video_ids` as this one playlist's own state — nothing here
    scopes to "the collection" or any notion of an active Discogs source, so this stays reusable
    regardless of how many collection sources a cache ends up tracking.
    """
    existing_id = playlist["ytmusic_playlist_id"]
    with store.connect() as conn:
        if existing_id:
            ytmusic_id = existing_id
        else:
            ytmusic_id, _created = ytmusic_client.get_or_create_playlist(
                yt, playlist_name, description="Curated from the Discogs collection app"
            )
            store.set_playlist_ytmusic_id(conn, playlist_id, ytmusic_id)
        conn.commit()

    try:
        removed = diff_and_sync_ytmusic(yt, ytmusic_id, video_ids)
    except YTMUSIC_MISSING_PLAYLIST_ERRORS:
        if not existing_id:
            raise
        with store.connect() as conn:
            ytmusic_id, _created = ytmusic_client.get_or_create_playlist(
                yt, playlist_name, description="Curated from the Discogs collection app"
            )
            store.set_playlist_ytmusic_id(conn, playlist_id, ytmusic_id)
            conn.commit()
        removed = diff_and_sync_ytmusic(yt, ytmusic_id, video_ids)

    with store.connect() as conn:
        store.set_playlist_pushed_at(conn, playlist_id)
        conn.commit()
    return removed
