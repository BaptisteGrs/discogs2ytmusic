from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from . import store
from .discogs import release_url


@dataclass
class PlaylistFilter:
    """A saved playlist's selection criteria.

    `tags` matches a release's styles OR genres (OR'd together, case-insensitive) —
    e.g. tags=["Electro", "Tech House"] catches a release tagged with either, on
    either field. `labels` matches release label names the same way. `year_min`/
    `year_max` bound the release year (inclusive); either side may be omitted.
    All given criteria are AND'd together; an empty/omitted criterion is skipped.
    """

    tags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    year_min: int | None = None
    year_max: int | None = None
    matched_only: bool = False

    def to_json(self) -> str:
        """Serialize to the JSON stored in `playlist_defs.filter_json`."""
        return json.dumps(
            {
                "tags": self.tags,
                "labels": self.labels,
                "year_min": self.year_min,
                "year_max": self.year_max,
                "matched_only": self.matched_only,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> PlaylistFilter:
        """Deserialize a `PlaylistFilter` from `playlist_defs.filter_json`."""
        data = json.loads(raw)
        return cls(
            tags=data.get("tags") or [],
            labels=data.get("labels") or [],
            year_min=data.get("year_min"),
            year_max=data.get("year_max"),
            matched_only=data.get("matched_only", False),
        )


def _norm(values: Iterable[str]) -> set[str]:
    return {v.strip().lower() for v in values}


def release_matches(release: sqlite3.Row, filt: PlaylistFilter) -> bool:
    """Whether a release passes a filter's styles/genres/labels/year criteria.

    `matched_only` is a per-track concern (a release has no single matched
    state) so it's applied separately in `resolve_rows`, not here.
    """
    if filt.tags:
        release_tags = _norm(json.loads(release["styles"]) or []) | _norm(json.loads(release["genres"]) or [])
        if not release_tags & _norm(filt.tags):
            return False
    if filt.labels:
        release_labels = _norm(json.loads(release["labels"]) or [])
        if not release_labels & _norm(filt.labels):
            return False
    year = release["year"]
    if filt.year_min is not None and (year is None or year < filt.year_min):
        return False
    return not (filt.year_max is not None and (year is None or year > filt.year_max))


@dataclass
class TrackRow:
    """One browsable row per track — the shape the Collection/Playlists tabs work with.

    Unlike `views.MatchRow` (one row per style tag, for the legacy per-style
    export/sync), this is always exactly one row per track regardless of how
    many tags/criteria it happens to match.
    """

    track_id: int | None
    release_id: int
    track_artist: str
    release_artist: str
    track_title: str  # clean title used for YouTube search / match lookup — never prefix this
    position: str | None  # vinyl side/position tag (e.g. "A1"); display-only, None for the no-tracklist fallback row
    release_title: str
    styles: list[str]
    genres: list[str]
    labels: list[str]
    year: int | None
    discogs_url: str
    match_id: int | None
    matched: bool
    video_id: str | None
    youtube_url: str
    video_title: str
    source: str
    score: float | None
    channel: str | None
    searched_at: str | None
    # A manual correction (artist override and/or picked video) protects this row from
    # scan/sync overwrites.
    locked: bool


def _build_track_row(
    conn: sqlite3.Connection,
    release: sqlite3.Row,
    track_id: int | None,
    artist: str,
    title: str,
    position: str | None,
    artist_overridden: bool,
) -> TrackRow:
    match = store.get_match(conn, artist, title)
    video_id = match["video_id"] if match else None
    searched_at = None
    if match is not None:
        searched_at = datetime.fromtimestamp(match["searched_at"]).isoformat(timespec="seconds")
    source = (match["source"] if match else None) or ""
    return TrackRow(
        track_id=track_id,
        release_id=release["release_id"],
        track_artist=artist,
        release_artist=store.effective_release_artist(release),
        track_title=title,
        position=position,
        release_title=store.effective_release_title(release),
        styles=json.loads(release["styles"]) or [],
        genres=json.loads(release["genres"]) or [],
        labels=json.loads(release["labels"]) or [],
        year=release["year"],
        discogs_url=release_url(release["release_id"]),
        match_id=match["id"] if match is not None else None,
        matched=bool(video_id),
        video_id=video_id,
        youtube_url=f"https://music.youtube.com/watch?v={video_id}" if video_id else "",
        video_title=(match["video_title"] if match else None) or "",
        source=source,
        score=match["score"] if match is not None else None,
        channel=(match["channel"] if match is not None else None) or "",
        searched_at=searched_at,
        locked=artist_overridden or source == "manual",
    )


def resolve_rows(conn: sqlite3.Connection, filt: PlaylistFilter | None = None) -> list[TrackRow]:
    """One row per track across the whole collection, optionally narrowed by a filter.

    Pass `filt=None` to browse everything (the Collection tab's default view).
    """
    rows: list[TrackRow] = []
    for release, tracks in store.iter_releases_with_tracks(conn):
        if filt is not None and not release_matches(release, filt):
            continue
        positions = {t["id"]: t["position"] for t in tracks}
        artist_overridden = {t["id"]: bool(t["search_artist"]) for t in tracks}
        for track_id, artist, title in store.effective_track_queries(release, tracks):
            row = _build_track_row(
                conn,
                release,
                track_id,
                artist,
                title,
                positions.get(track_id) if track_id is not None else None,
                artist_overridden.get(track_id, False),
            )
            if filt is not None and filt.matched_only and not row.video_id:
                continue
            rows.append(row)
    rows.sort(key=lambda r: (r.track_artist, r.track_title))
    return rows


def resolve_playlist_rows(conn: sqlite3.Connection, playlist_id: int) -> list[TrackRow]:
    """TrackRows for one curated playlist's tracks, in playlist order (unlike `resolve_rows`,
    this is not re-sorted — playlist order is meaningful)."""
    rows: list[TrackRow] = []
    for track_id in store.list_playlist_track_ids(conn, playlist_id):
        track = store.get_track(conn, track_id)
        if track is None:
            continue  # stale reference — shouldn't happen, but don't let it crash the page
        release = store.get_release(conn, track["release_id"])
        if release is None:
            continue
        [(_, artist, title)] = store.effective_track_queries(release, [track])
        rows.append(
            _build_track_row(conn, release, track_id, artist, title, track["position"], bool(track["search_artist"]))
        )
    return rows
