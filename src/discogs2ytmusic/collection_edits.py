from __future__ import annotations

import pandas as pd

from . import store, ytmusic_client


def _int_or_none(value) -> int | None:
    return None if pd.isna(value) else int(value)


def apply_artist_edits(conn, original: pd.DataFrame, edited: pd.DataFrame) -> int:
    """Diff the `artist` column and persist changes as artist overrides.

    A track-backed row gets a per-track `search_artist` override; a release
    with no tracklist on file (track_id is None) falls back to the
    release-level override instead. Clearing the cell reverts to the
    Discogs-sourced artist. Returns the number of rows updated.
    """
    count = 0
    for idx in original.index:
        old_val, new_val = original.at[idx, "artist"], edited.at[idx, "artist"]
        if new_val == old_val:
            continue
        override = new_val.strip() or None
        track_id = _int_or_none(original.at[idx, "track_id"])
        if track_id is not None:
            store.set_track_search_artist(conn, track_id, override)
        else:
            store.set_release_artist_override(conn, int(original.at[idx, "release_id"]), override)
        count += 1
    return count


def apply_video_link_edits(conn, original: pd.DataFrame, edited: pd.DataFrame) -> tuple[int, list[str]]:
    """Diff the `youtube_url` column and persist changes as manual match corrections.

    Uses the *edited* artist/title (so a simultaneous artist correction on the
    same row is respected) when creating a brand-new match. Returns (rows
    updated, error messages for URLs that couldn't be parsed).
    """
    count = 0
    errors: list[str] = []
    for idx in original.index:
        old_val, new_val = original.at[idx, "youtube_url"], edited.at[idx, "youtube_url"]
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
            artist, title = edited.at[idx, "artist"], edited.at[idx, "title"]
            store.save_match(conn, artist, title, video_id, None, "manual", None)
        count += 1
    return count, errors
