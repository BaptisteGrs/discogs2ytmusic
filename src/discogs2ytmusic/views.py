from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import store
from .discogs import release_url


@dataclass
class MatchRow:
    """One (style, track) pairing for the legacy per-style export/sync flow.

    A track that carries N style tags appears as N separate rows, one per style.
    """

    match_id: int | None  # None if this track has never been searched (no cached matches row)
    track_id: int | None  # None for the release-title fallback used when a release has no tracklist
    style: str
    artist: str  # the artist actually used for the search (release artist, or a fix-artist override)
    title: str
    discogs_url: str
    matched: bool
    video_id: str | None
    youtube_url: str
    video_title: str
    source: str
    score: float | None
    searched_at: str | None


def build_match_rows(conn: sqlite3.Connection, styles: list[str] | None = None) -> list[MatchRow]:
    """One row per (style, track) — a track under N styles appears N times, same as `export`."""
    rows: list[MatchRow] = []
    for release, tracks in store.iter_releases_with_tracks(conn):
        release_styles = json.loads(release["styles"]) or []
        if styles:
            release_styles = [s for s in release_styles if s in styles]
        if not release_styles:
            continue
        for s in release_styles:
            for track_id, artist, track_title in store.effective_track_queries(release, tracks):
                match = store.get_match(conn, artist, track_title)
                video_id = match["video_id"] if match else None
                searched_at = None
                if match is not None:
                    searched_at = datetime.fromtimestamp(match["searched_at"]).isoformat(timespec="seconds")
                rows.append(
                    MatchRow(
                        match_id=match["id"] if match is not None else None,
                        track_id=track_id,
                        style=s,
                        artist=artist,
                        title=track_title,
                        discogs_url=release_url(release["release_id"]),
                        matched=bool(video_id),
                        video_id=video_id,
                        youtube_url=f"https://music.youtube.com/watch?v={video_id}" if video_id else "",
                        video_title=(match["video_title"] if match else None) or "",
                        source=(match["source"] if match else None) or "",
                        score=match["score"] if match is not None else None,
                        searched_at=searched_at,
                    )
                )
    rows.sort(key=lambda r: (r.style, r.artist, r.title))
    return rows
