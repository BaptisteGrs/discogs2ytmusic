from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from . import store
from .discogs import release_url

BoolOp = Literal["and", "or"]


@dataclass
class TagGroup:
    """One AND/OR group of style/genre tags, as used by `PlaylistFilter.tag_groups`.

    `tags` are matched against a release's styles OR genres (case-insensitive), same
    as a flat tag list always was. `mode` says whether a release must carry ALL of
    `tags` ("and") or ANY of them ("or") to satisfy this group. An empty `tags` list
    contributes nothing (it's dropped before evaluation — see `release_matches`).
    """

    tags: list[str] = field(default_factory=list)
    mode: BoolOp = "or"

    def matches(self, release_tags: set[str]) -> bool:
        """Whether `release_tags` (already lower-cased) satisfies this group."""
        wanted = _norm(self.tags)
        return wanted <= release_tags if self.mode == "and" else bool(wanted & release_tags)


@dataclass
class PlaylistFilter:
    """A saved playlist's selection criteria.

    `tag_groups` matches a release's styles/genres with explicit AND/OR chaining: each
    group is internally AND'd or OR'd per its own `mode`, and the groups themselves are
    combined by `tag_groups_mode` — e.g. two OR-mode groups combined with "and" expresses
    `("Electro" OR "Tech House") AND ("Vinyl Only" OR "Reissue")`. A single OR-mode group
    reproduces the old flat-list-of-tags behavior. `labels` matches release label names
    (OR'd together, case-insensitive). `year_min`/`year_max` bound the release year
    (inclusive); either side may be omitted. `channels` matches a track's matched
    YouTube uploader/channel (OR'd together); like `matched_only`, it's a per-track
    concern with no single value on a release, so it's applied in `resolve_rows` rather
    than `release_matches`. All given criteria are AND'd together; an empty/omitted
    criterion is skipped.
    """

    tag_groups: list[TagGroup] = field(default_factory=list)
    tag_groups_mode: BoolOp = "or"
    labels: list[str] = field(default_factory=list)
    year_min: int | None = None
    year_max: int | None = None
    matched_only: bool = False
    channels: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialize to the JSON stored in `playlist_defs.filter_json`."""
        return json.dumps(
            {
                "tag_groups": [{"tags": g.tags, "mode": g.mode} for g in self.tag_groups],
                "tag_groups_mode": self.tag_groups_mode,
                "labels": self.labels,
                "year_min": self.year_min,
                "year_max": self.year_max,
                "matched_only": self.matched_only,
                "channels": self.channels,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> PlaylistFilter:
        """Deserialize a `PlaylistFilter` from `playlist_defs.filter_json`.

        Accepts both the current `tag_groups` shape and the flat `tags: [...]` list
        used before #20, which is treated as a single OR group for filter_json rows
        saved before that change.
        """
        data = json.loads(raw)
        if "tag_groups" in data:
            tag_groups = [
                TagGroup(tags=g.get("tags") or [], mode=g.get("mode") or "or") for g in data.get("tag_groups") or []
            ]
        else:
            legacy_tags = data.get("tags") or []
            tag_groups = [TagGroup(tags=legacy_tags, mode="or")] if legacy_tags else []
        return cls(
            tag_groups=tag_groups,
            tag_groups_mode=data.get("tag_groups_mode") or "or",
            labels=data.get("labels") or [],
            year_min=data.get("year_min"),
            year_max=data.get("year_max"),
            matched_only=data.get("matched_only", False),
            # Absent for filter_json rows saved before #28 added this field.
            channels=data.get("channels") or [],
        )


def _norm(values: Iterable[str]) -> set[str]:
    return {v.strip().lower() for v in values}


def release_matches(release: sqlite3.Row, filt: PlaylistFilter) -> bool:
    """Whether a release passes a filter's styles/genres/labels/year criteria.

    `matched_only` is a per-track concern (a release has no single matched
    state) so it's applied separately in `resolve_rows`, not here.
    """
    active_groups = [g for g in filt.tag_groups if g.tags]
    if active_groups:
        release_tags = _norm(json.loads(release["styles"]) or []) | _norm(json.loads(release["genres"]) or [])
        group_results = [g.matches(release_tags) for g in active_groups]
        combined = all(group_results) if filt.tag_groups_mode == "and" else any(group_results)
        if not combined:
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
    # A manual correction (artist/title/style/genre override and/or picked video)
    # protects this row from scan/sync overwrites.
    locked: bool


def _build_track_row(
    conn: sqlite3.Connection,
    release: sqlite3.Row,
    track: sqlite3.Row | None,
    artist: str,
    title: str,
) -> TrackRow:
    track_id = track["id"] if track is not None else None
    position = track["position"] if track is not None else None
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
        styles=store.effective_track_styles(track, release),
        genres=store.effective_track_genres(track, release),
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
        locked=(track is not None and bool(track["search_artist"]))
        or source == "manual"
        or bool(release["artist_override"])
        or bool(release["title_override"])
        or bool(release["styles_override"])
        or bool(release["genres_override"])
        or (track is not None and bool(track["styles_override"]))
        or (track is not None and bool(track["genres_override"])),
    )


def resolve_rows(conn: sqlite3.Connection, filt: PlaylistFilter | None = None) -> list[TrackRow]:
    """One row per track across the whole collection, optionally narrowed by a filter.

    Pass `filt=None` to browse everything (the Collection tab's default view).
    """
    rows: list[TrackRow] = []
    for release, tracks in store.iter_releases_with_tracks(conn):
        if filt is not None and not release_matches(release, filt):
            continue
        tracks_by_id = {t["id"]: t for t in tracks}
        for track_id, artist, title in store.effective_track_queries(release, tracks):
            track = tracks_by_id.get(track_id) if track_id is not None else None
            row = _build_track_row(conn, release, track, artist, title)
            if filt is not None and filt.matched_only and not row.video_id:
                continue
            if filt is not None and filt.channels and row.channel not in filt.channels:
                continue
            rows.append(row)
    rows.sort(key=lambda r: (r.track_artist, r.track_title))
    return rows


def filter_rows_by_query(rows: list[TrackRow], query: str) -> list[TrackRow]:
    """Narrow `rows` to those whose track artist, track title, or release title contains
    `query` as a case-insensitive substring; returns `rows` unchanged if `query` is blank.

    Applied client-side to an already-resolved row list (the Collection tab's free-text
    search box) rather than folded into `PlaylistFilter`/`release_matches` — collection
    size is small and rows are already materialized for the table by the time this runs.
    """
    needle = query.strip().lower()
    if not needle:
        return rows
    return [
        r
        for r in rows
        if needle in r.track_artist.lower() or needle in r.track_title.lower() or needle in r.release_title.lower()
    ]


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
        rows.append(_build_track_row(conn, release, track, artist, title))
    return rows
