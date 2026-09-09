"""Fetch a Discogs collection into the sqlite cache.

Factored out of `cli.py`'s `scan` command so the same per-release cache-write logic is
reusable from the Streamlit UI (`app.py`'s Scan button) without going through the CLI —
each caller drives its own progress display (`rich.Progress` for the CLI, `st.progress`
for the UI) around `scan_release`.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import store
from .discogs import DiscogsClient


def _clean_artist_names(names: list[str]) -> str | None:
    """Join Discogs artist credits into one display string, stripping each name's own
    disambiguation suffix (e.g. "Rush (2)") before joining — joining first and stripping
    only the tail would miss any but the last name."""
    if not names:
        return None
    cleaned = [re.sub(r"\s*\(\d+\)$", "", n).strip() for n in names]
    return ", ".join(n for n in cleaned if n) or None


def scan_release(conn: sqlite3.Connection, client: DiscogsClient, item: dict[str, Any], refresh: bool) -> None:
    """Upsert one Discogs collection item (and its tracklist, if needed) into the cache.

    Args:
        conn: Open sqlite connection.
        client: An authenticated Discogs client, used to fetch tracklist detail when needed.
        item: One item as yielded by `DiscogsClient.iter_collection_basic`.
        refresh: Re-fetch and replace the tracklist even if one is already cached.

    Commits immediately, so a caller looping over many releases doesn't lose progress on
    a crash/interrupt.
    """
    info = item["basic_information"]
    release_id = info["id"]
    artist = ", ".join(a["name"] for a in info.get("artists", []))
    artist = re.sub(r"\s*\(\d+\)$", "", artist)  # strip Discogs disambiguation suffixes e.g. "Rush (2)"
    title = info.get("title", "")
    styles = info.get("styles", []) or []
    genres = info.get("genres", []) or []
    year = info.get("year") or None
    labels = [label["name"] for label in info.get("labels", []) or [] if label.get("name")]

    videos = None  # None means "don't touch whatever's already cached" (see store.upsert_release)
    if refresh or not store.has_tracks(conn, release_id):
        detail = client.get_release_detail(release_id)
        store.replace_tracks(
            conn,
            release_id,
            [(t.position, t.title, t.duration, _clean_artist_names(t.artists)) for t in detail.tracklist],
        )
        videos = [{"uri": v.uri, "title": v.title, "duration": v.duration} for v in detail.videos]

    store.upsert_release(conn, release_id, artist, title, styles, genres, year=year, labels=labels, videos=videos)
    conn.commit()
