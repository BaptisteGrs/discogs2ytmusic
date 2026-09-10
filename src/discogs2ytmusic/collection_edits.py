from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from . import store, ytmusic_client


def _int_or_none(value: Any) -> int | None:
    return None if pd.isna(value) else int(value)


def _str(value: Any) -> str:
    """Cast a DataFrame cell to str — columns we read here are always string dtype."""
    return str(value)


def apply_artist_edits(conn: sqlite3.Connection, original: pd.DataFrame, edited: pd.DataFrame) -> int:
    """Diff the `track_artist` column and persist changes as artist overrides.

    A track-backed row gets a per-track `search_artist` override; a release
    with no tracklist on file (track_id is None) falls back to the
    release-level override instead. Clearing the cell reverts to the
    Discogs-sourced (or heuristically-split) artist. Returns the number of
    rows updated.
    """
    count = 0
    for idx in original.index:
        old_val, new_val = _str(original.at[idx, "track_artist"]), _str(edited.at[idx, "track_artist"])
        if new_val == old_val:
            continue
        override = new_val.strip() or None
        track_id = _int_or_none(original.at[idx, "track_id"])
        if track_id is not None:
            store.set_track_search_artist(conn, track_id, override)
        else:
            release_id = _int_or_none(original.at[idx, "release_id"])
            assert release_id is not None  # every row has a release_id
            store.set_release_artist_override(conn, release_id, override)
        count += 1
    return count


def _split_comma_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def apply_style_edits(conn: sqlite3.Connection, original: pd.DataFrame, edited: pd.DataFrame) -> int:
    """Diff the `styles` column and persist changes as style overrides.

    A track-backed row gets a per-track override, since Discogs only reports styles
    per-release and a multi-style release otherwise has no way to say which track is
    which (e.g. "Tech House, Downtempo, Breaks" across 4 tracks). A release with no
    tracklist on file (track_id is None) falls back to the release-level override
    instead. Clearing the cell reverts to the inherited (release, or Discogs') styles.
    Returns the number of rows updated.
    """
    count = 0
    for idx in original.index:
        old_val, new_val = _str(original.at[idx, "styles"]), _str(edited.at[idx, "styles"])
        if new_val == old_val:
            continue
        override = _split_comma_list(new_val) or None
        track_id = _int_or_none(original.at[idx, "track_id"])
        if track_id is not None:
            store.set_track_styles_override(conn, track_id, override)
        else:
            release_id = _int_or_none(original.at[idx, "release_id"])
            assert release_id is not None  # every row has a release_id
            store.set_release_styles_override(conn, release_id, override)
        count += 1
    return count


def apply_genre_edits(conn: sqlite3.Connection, original: pd.DataFrame, edited: pd.DataFrame) -> int:
    """Diff the `genres` column and persist changes as genre overrides.

    Same per-track/release-fallback split as `apply_style_edits` — see its docstring.
    Returns the number of rows updated.
    """
    count = 0
    for idx in original.index:
        old_val, new_val = _str(original.at[idx, "genres"]), _str(edited.at[idx, "genres"])
        if new_val == old_val:
            continue
        override = _split_comma_list(new_val) or None
        track_id = _int_or_none(original.at[idx, "track_id"])
        if track_id is not None:
            store.set_track_genres_override(conn, track_id, override)
        else:
            release_id = _int_or_none(original.at[idx, "release_id"])
            assert release_id is not None  # every row has a release_id
            store.set_release_genres_override(conn, release_id, override)
        count += 1
    return count


def apply_video_link_edits(
    conn: sqlite3.Connection, original: pd.DataFrame, edited: pd.DataFrame
) -> tuple[int, list[str]]:
    """Diff the `youtube_url` column and persist changes as manual match corrections.

    Uses the *edited* artist/title (so a simultaneous artist correction on the
    same row is respected) when creating a brand-new match. Returns (rows
    updated, error messages for URLs that couldn't be parsed).
    """
    count = 0
    errors: list[str] = []
    for idx in original.index:
        old_val, new_val = _str(original.at[idx, "youtube_url"]), _str(edited.at[idx, "youtube_url"])
        if new_val == old_val:
            continue
        new_val = new_val.strip()
        match_id = _int_or_none(original.at[idx, "match_id"])

        if not new_val:
            if match_id is not None:
                store.update_match(conn, match_id, video_id=None, video_title=None, source="manual")
                count += 1
            continue

        try:
            video_id = ytmusic_client.parse_video_id(new_val)
        except ValueError as e:
            errors.append(str(e))
            continue

        if match_id is not None:
            store.update_match(conn, match_id, video_id=video_id, video_title=None, source="manual")
        else:
            artist, title = _str(edited.at[idx, "track_artist"]), _str(edited.at[idx, "track_title"])
            store.save_match(conn, artist, title, video_id, None, "manual", None)
        count += 1
    return count, errors
