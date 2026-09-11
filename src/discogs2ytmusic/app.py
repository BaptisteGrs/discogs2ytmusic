from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Any, Literal, cast

import pandas as pd
import streamlit as st
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicError

from discogs2ytmusic import scan_engine, store, sync_engine, ytmusic_client
from discogs2ytmusic.collection_edits import (
    apply_artist_edits,
    apply_genre_edits,
    apply_style_edits,
    apply_video_link_edits,
)
from discogs2ytmusic.config import Config
from discogs2ytmusic.discogs import DiscogsClient, DiscogsError
from discogs2ytmusic.filters import (
    BoolOp,
    PlaylistFilter,
    TagGroup,
    TrackRow,
    filter_rows_by_query,
    resolve_playlist_rows,
    resolve_rows,
)

st.set_page_config(page_title="Discogs -> YT Music", layout="wide")

COLLECTION_COLUMNS = [
    "release_title",
    "track_artist",
    "position",
    "track_title",
    "labels",
    "year",
    "matched",
    "discogs_url",
    "youtube_url",
    "channel",
    "locked",
    "release_artist",
    "styles",
    "genres",
    "confidence",
    "video_title",
]

PLAYLIST_TRACK_COLUMNS = [
    "remove",
    "release_title",
    "track_artist",
    "position",
    "track_title",
    "labels",
    "year",
    "matched",
    "discogs_url",
    "youtube_url",
    "channel",
    "locked",
    "release_artist",
    "styles",
    "genres",
    "confidence",
    "video_title",
]

# Labels/help text for columns shared across the Collection tab and both Playlist-tab tables
# (`render_collection_tab`, `_render_playlist_detail`'s tracks table and its "Add tracks" search
# results table) — keeps the same field looking identical everywhere it's rendered. Each call site
# layers its own checkbox column (`select`/`remove`) and `disabled` list on top of this.
SHARED_COLUMN_CONFIG: dict[str, Any] = {
    "track_artist": st.column_config.TextColumn(
        "Track Artist", help="Edit to override the artist used for this track's YouTube search"
    ),
    "release_artist": st.column_config.TextColumn("Release Artist(s)"),
    "position": st.column_config.TextColumn("Position", help="Vinyl side/track position, e.g. A1"),
    "track_title": st.column_config.TextColumn("Track Title"),
    "release_title": st.column_config.TextColumn("Release Title"),
    "youtube_url": st.column_config.LinkColumn(
        "YouTube link",
        help="Paste a YouTube/YT Music URL, or clear it to reject the current match",
        display_text="Open",
    ),
    "discogs_url": st.column_config.LinkColumn("Discogs", display_text="Open"),
    "matched": st.column_config.CheckboxColumn("Matched"),
    "channel": st.column_config.TextColumn("Channel", help="Uploader/channel of the matched YouTube video"),
    "styles": st.column_config.TextColumn(
        "Styles", help="Comma-separated. Edit to override this track's styles from Discogs"
    ),
    "genres": st.column_config.TextColumn(
        "Genres", help="Comma-separated. Edit to override this track's genres from Discogs"
    ),
    "locked": st.column_config.CheckboxColumn(
        "Locked",
        help=(
            "A manual correction (one or more fields) protects this row from being "
            "overwritten by `scan --refresh` or `rematch`. Clear the correction "
            "(`fix-artist --clear` / `correct --clear`) to unlock it."
        ),
    ),
    "confidence": st.column_config.ProgressColumn(
        "Confidence",
        help=(
            "Fuzzy-match score between the Discogs track and the YouTube result. Blank for manually-corrected links."
        ),
        min_value=0,
        max_value=100,
        format="%d%%",
    ),
}

_NEW_PLAYLIST_SENTINEL = "+ Create new playlist"
_NO_FOLDER_SENTINEL = "No folder"
_NEW_FOLDER_SENTINEL = "+ Create new folder"

# Shared "pill" button style: small, rounded, icon+text buttons placed close together in a
# horizontal container (see `_render_playlist_detail`'s Sync/Delete row and
# `render_collection_tab`'s Scan/Sync matches/Rematch row). Keyed by `st.container(key=...)`
# rather than a generic class so each button can still get its own color override (e.g. delete).
_ACTION_PILL_CSS = """
<style>
.st-key-scan_pill button,
.st-key-sync_matches_pill button,
.st-key-rematch_pill button,
.st-key-sync_pill button,
.st-key-delete_pill button {
    border-radius: 999px !important;
    padding: 0.3rem 0.9rem !important;
    min-height: 0 !important;
    font-size: 0.85rem !important;
}
.st-key-delete_pill button {
    border-color: #E5E2D9 !important;
    color: #AF3029 !important;
}
.st-key-delete_pill button:hover {
    border-color: #E3B6AE !important;
    color: #AF3029 !important;
    background-color: #FBEEEC !important;
}
</style>
"""


def _all_rows() -> list[TrackRow]:
    with store.connect() as conn:
        return resolve_rows(conn)


def _tag_options(rows: list[TrackRow]) -> list[str]:
    tags: set[str] = set()
    for r in rows:
        tags.update(r.styles)
        tags.update(r.genres)
    return sorted(tags)


def _label_options(rows: list[TrackRow]) -> list[str]:
    labels: set[str] = set()
    for r in rows:
        labels.update(r.labels)
    return sorted(labels)


def _channel_options(rows: list[TrackRow]) -> list[str]:
    return sorted({r.channel for r in rows if r.channel})


def _year_bounds(rows: list[TrackRow]) -> tuple[int, int]:
    years = [r.year for r in rows if r.year]
    if not years:
        return (1900, 2030)
    return (min(years), max(years))


_TRACK_ROW_COLUMNS = [
    "track_id",
    "match_id",
    "release_id",
    "track_artist",
    "release_artist",
    "position",
    "track_title",
    "release_title",
    "styles",
    "genres",
    "labels",
    "year",
    "matched",
    "confidence",
    "youtube_url",
    "video_title",
    "channel",
    "locked",
    "discogs_url",
]


def _rows_to_dataframe(rows: list[TrackRow], flag_column: str | None = None) -> pd.DataFrame:
    """`flag_column`, if given, adds a leading boolean column (default False) — used for
    a transient "select"/"remove" checkbox that doesn't correspond to any stored field."""
    records = [
        {
            "track_id": r.track_id,
            "match_id": r.match_id,
            "release_id": r.release_id,
            "track_artist": r.track_artist,
            "release_artist": r.release_artist,
            "position": r.position,
            "track_title": r.track_title,
            "release_title": r.release_title,
            "styles": ", ".join(r.styles),
            "genres": ", ".join(r.genres),
            "labels": ", ".join(r.labels),
            "year": r.year,
            "matched": r.matched,
            "confidence": r.score,
            "youtube_url": r.youtube_url,
            "video_title": r.video_title,
            "channel": r.channel,
            "locked": r.locked,
            "discogs_url": r.discogs_url,
        }
        for r in rows
    ]
    columns = list(_TRACK_ROW_COLUMNS)
    if flag_column:
        for record in records:
            record[flag_column] = False
        columns.append(flag_column)
    # Pass `columns=` explicitly so an empty row set still yields a dataframe with the
    # expected columns (incl. `flag_column`) instead of a columnless one that crashes
    # any code — e.g. `_selected_track_ids` — expecting them to be present.
    return pd.DataFrame(records, columns=columns)


def _selected_track_ids(edited_df: pd.DataFrame, flag_column: str) -> list[int]:
    """track_ids checked under `flag_column`, excluding rows with no real track_id
    (the no-tracklist fallback row can't be added to a playlist)."""
    selected = edited_df[edited_df[flag_column] & edited_df["track_id"].notna()]
    return [int(tid) for tid in selected["track_id"]]


def _load_discogs_client() -> tuple[DiscogsClient, str] | None:
    """Build a Discogs client from saved credentials, or None if `auth discogs` hasn't been run yet."""
    cfg = Config.load()
    if not cfg.discogs_token or not cfg.discogs_username:
        return None
    return DiscogsClient(cfg.discogs_token), cfg.discogs_username


def _run_scan(refresh: bool) -> bool:
    """Fetch the Discogs collection (+ tracklists) into the cache — the UI equivalent of
    `scan --refresh`. Shows its own progress bar and error/success messages; the caller
    decides whether/how to refresh the page afterward.

    Returns:
        True if the scan completed, False if it couldn't start (no Discogs credentials
        saved, or the Discogs API call itself failed).
    """
    creds = _load_discogs_client()
    if creds is None:
        st.error("Not authenticated with Discogs. Run `discogs2ytmusic auth discogs` first.")
        return False
    client, username = creds

    try:
        with st.spinner("Fetching collection listing..."):
            basics = list(client.iter_collection_basic(username))
    except DiscogsError as e:
        st.error(f"Could not fetch your Discogs collection: {e}")
        return False

    total = len(basics)
    progress = st.progress(0.0, text=f"Fetching tracklists... (0/{total})")
    with store.connect() as conn:
        for i, item in enumerate(basics, start=1):
            scan_engine.scan_release(conn, client, item, refresh)
            progress.progress(i / total if total else 1.0, text=f"Fetching tracklists... ({i}/{total})")
    progress.empty()

    st.success(f"Scanned {total} release(s).")
    return True


def _run_sync_matches() -> bool:
    """Match any unmatched tracks against YouTube/YT Music — the UI equivalent of `sync`.

    Only populates the match cache; it never touches a real YT Music account (pushing a
    playlist to one has its own confirm-gated button in the Playlists tab).

    Returns:
        True if matching ran (even if it matched nothing), False if there was nothing to match.
    """
    with store.connect() as conn:
        releases_with_tracks = list(store.iter_releases_with_tracks(conn))
        total = sum(len(store.effective_track_queries(r, t)) for r, t in releases_with_tracks)
        if total == 0:
            st.info("Nothing to match yet — scan your collection first.")
            return False

        yt = ytmusic_client.get_client(authenticated=False)
        progress = st.progress(0.0, text=f"Matching tracks... (0/{total})")
        done = 0

        def _on_track_done() -> None:
            nonlocal done
            done += 1
            progress.progress(done / total, text=f"Matching tracks... ({done}/{total})")

        sync_engine.ensure_matches(conn, yt, releases_with_tracks, on_track_done=_on_track_done)
        progress.empty()

    st.success(f"Matched {total} track(s).")
    return True


def _run_rematch(include_manual: bool) -> bool:
    """Clear cached matches and re-match everything from scratch — the UI equivalent of
    `rematch`. Preserves manually-corrected matches (`matches.source == 'manual'`) unless
    `include_manual` is set, per the manual-correction "locking" invariant in CLAUDE.md.

    Returns:
        True if it completed, False if the re-scan or re-match step couldn't run.
    """
    with store.connect() as conn:
        n_cleared = store.clear_all_matches(conn, include_manual=include_manual)
        conn.commit()
    st.info(f"Cleared {n_cleared} cached match(es).")

    if not _run_scan(refresh=True):
        return False
    return _run_sync_matches()


def _render_scan_button() -> None:
    with st.container(key="scan_pill", width="content"):
        clicked = st.button(
            "Scan",
            key="scan_button",
            icon=":material/cloud_sync:",
            help="Re-fetch your collection and tracklists from Discogs",
        )
    if clicked and _run_scan(refresh=True):
        st.session_state.pop("collection_editor", None)
        st.rerun()


def _render_sync_matches_button() -> None:
    with st.container(key="sync_matches_pill", width="content"):
        clicked = st.button(
            "Sync matches",
            key="sync_matches_button",
            icon=":material/search:",
            help="Match any unmatched tracks against YouTube/YT Music (doesn't touch your YT Music account)",
        )
    if clicked and _run_sync_matches():
        st.session_state.pop("collection_editor", None)
        st.rerun()


def _render_rematch_button() -> None:
    if st.session_state.get("confirm_rematch"):
        return
    with st.container(key="rematch_pill", width="content"):
        clicked = st.button(
            "Rematch",
            key="rematch_button",
            icon=":material/restart_alt:",
            help="Clear cached matches and re-match everything from scratch (slow; preserves manual corrections)",
        )
    if clicked:
        st.session_state["confirm_rematch"] = True
        st.rerun()


def _render_rematch_confirmation() -> None:
    with store.connect() as conn:
        n_matches = store.count_matches(conn)
        n_manual = store.count_manual_matches(conn)

    include_manual = st.checkbox("Also clear manually-corrected matches", key="rematch_include_manual")
    n_to_clear = n_matches if include_manual else n_matches - n_manual

    warning = (
        f"This will delete {n_to_clear} cached match(es) and re-search every track from scratch, "
        "re-fetching your Discogs collection first. This can take a while and makes a lot of "
        "YouTube/YT Music requests. It will NOT touch your real YT Music account."
    )
    if not include_manual and n_manual:
        warning += f" {n_manual} manually-corrected match(es) will be kept untouched."
    st.warning(warning)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, rematch", key="confirm_rematch_yes"):
            st.session_state["confirm_rematch"] = False
            if not _run_rematch(include_manual=include_manual):
                return
            st.session_state.pop("collection_editor", None)
            st.rerun()
    with col2:
        if st.button("Cancel", key="confirm_rematch_no"):
            st.session_state["confirm_rematch"] = False
            st.rerun()


def _collection_row_digest(rows: list[TrackRow]) -> str:
    """Short digest identifying the currently visible (filtered) row set by track_id.

    Shared by every Collection tab widget that must reset itself — rather than
    silently misapply stale state to the wrong row — whenever the active filter
    changes what's visible (#19).
    """
    ids = ",".join(str(r.track_id) for r in rows)
    return hashlib.sha1(ids.encode()).hexdigest()[:12]


def _collection_table_key(rows: list[TrackRow]) -> str:
    """Derive the Collection tab's main `st.dataframe` key from the currently visible row set.

    `st.dataframe`'s native row-selection state matches selected rows to the previous
    render by row *position*, not row identity. Reusing one static key across
    differently-filtered row sets lets a stale selection apply to the wrong row once the
    filter changes what's visible, or point past the end of a narrower dataframe (#19).
    Keying on the row set's track_ids forces a fresh widget — with no pending selection —
    whenever the visible rows change.
    """
    return f"collection_table_{_collection_row_digest(rows)}"


def _render_edit_panel(selected_rows: list[TrackRow]) -> None:
    """Edit artist/styles/genres/YouTube match for exactly one currently-selected track.

    Corrections moved here (out of inline cell-editing) once the main table switched to
    `st.dataframe` for real shift-click range selection (#57) — `st.dataframe` itself is
    read-only, so it can't host in-place editing the way `st.data_editor` did. Scoped to
    a single selected row: artist and YouTube-link corrections are inherently per-track,
    so there's no obviously correct bulk semantic once more than one row is selected.
    """
    if len(selected_rows) != 1:
        if selected_rows:
            st.caption(f"{len(selected_rows)} tracks selected. Select exactly one to edit its details.")
        else:
            st.caption("Select a track above to edit its artist, styles, genres, or YouTube match.")
        return

    row = selected_rows[0]
    row_key = f"collection_edit_{row.track_id if row.track_id is not None else f'release_{row.release_id}'}"
    with st.form(key=row_key):
        st.caption(f"Editing **{row.track_artist} — {row.track_title}**")
        artist = st.text_input(
            "Track Artist",
            value=row.track_artist,
            key=f"{row_key}_artist",
            help="Edit to override the artist used for this track's YouTube search",
        )
        styles = st.text_input("Styles", value=", ".join(row.styles), key=f"{row_key}_styles")
        genres = st.text_input("Genres", value=", ".join(row.genres), key=f"{row_key}_genres")
        youtube_url = st.text_input(
            "YouTube link",
            value=row.youtube_url,
            key=f"{row_key}_youtube_url",
            help="Paste a YouTube/YT Music URL, or clear it to reject the current match",
        )
        saved = st.form_submit_button("Save changes", key=f"{row_key}_save")

    if not saved:
        return

    original = _rows_to_dataframe([row])
    edited = original.copy()
    edited.loc[0, ["track_artist", "styles", "genres", "youtube_url"]] = [artist, styles, genres, youtube_url]

    with store.connect() as conn:
        n_artist = apply_artist_edits(conn, original, edited)
        n_style = apply_style_edits(conn, original, edited)
        n_genre = apply_genre_edits(conn, original, edited)
        n_video, errors = apply_video_link_edits(conn, original, edited)

    for message in errors:
        st.error(message)
    n_corrections = n_artist + n_style + n_genre + n_video
    if n_corrections:
        st.success(f"Saved {n_corrections} correction(s).")
        st.rerun()


def _tag_group_ids() -> list[int]:
    """Stable per-group ids backing the Style filter's AND/OR group builder (#20).

    Groups are addressed by an ever-incrementing id, not list position, so removing
    one group can't shift another group's widget state (its picked tags/mode) onto
    the wrong slot.
    """
    if "collection_tag_group_ids" not in st.session_state:
        st.session_state["collection_tag_group_ids"] = [0]
        st.session_state["collection_tag_group_next_id"] = 1
    ids: list[int] = st.session_state["collection_tag_group_ids"]
    return ids


def _render_tag_group_filters(tag_options: list[str]) -> tuple[list[TagGroup], BoolOp]:
    """Render the Style filter's AND/OR group builder and return the resulting groups
    and how they combine (see `PlaylistFilter.tag_groups`/`tag_groups_mode`).

    Each group gets its own tag multiselect + and/or "match" mode; "+ Add style group"
    appends another; a group beyond the first can be removed. Multiple groups only show
    a combinator (AND/OR between groups) once there's more than one to combine.
    """
    group_ids = _tag_group_ids()
    tag_groups: list[TagGroup] = []
    for i, gid in enumerate(group_ids):
        label_visibility: Literal["visible", "collapsed"] = "visible" if i == 0 else "collapsed"
        tag_col, mode_col, remove_col = st.columns([3, 1, 1])
        with tag_col:
            selected = st.multiselect(
                "Style", tag_options, key=f"collection_tag_group_{gid}", label_visibility=label_visibility
            )
        with mode_col:
            mode = cast(
                BoolOp,
                st.selectbox(
                    "Match",
                    options=["or", "and"],
                    format_func=lambda m: "any of" if m == "or" else "all of",
                    key=f"collection_tag_group_mode_{gid}",
                    label_visibility=label_visibility,
                ),
            )
        with remove_col:
            if i == 0:
                st.write("")  # align with the labeled widgets in this row
            if len(group_ids) > 1 and st.button("Remove", key=f"collection_tag_group_remove_{gid}"):
                group_ids.remove(gid)
                st.rerun()
        if selected:
            tag_groups.append(TagGroup(tags=selected, mode=mode))

    add_col, combinator_col = st.columns([1, 3])
    with add_col:
        if st.button("+ Add style group", key="collection_tag_group_add"):
            new_id = st.session_state["collection_tag_group_next_id"]
            st.session_state["collection_tag_group_next_id"] = new_id + 1
            group_ids.append(new_id)
            st.rerun()
    tag_groups_mode: BoolOp = "or"
    if len(group_ids) > 1:
        with combinator_col:
            tag_groups_mode = cast(
                BoolOp,
                st.radio(
                    "Combine style groups with",
                    options=["or", "and"],
                    format_func=lambda m: "Match ANY group (OR)" if m == "or" else "Match ALL groups (AND)",
                    key="collection_tag_groups_mode",
                    horizontal=True,
                ),
            )
    return tag_groups, tag_groups_mode


def render_collection_tab() -> None:
    """Render the browsable/editable table of every cached track and its YouTube match."""
    st.markdown(_ACTION_PILL_CSS, unsafe_allow_html=True)
    title_col, actions_col = st.columns([1, 1], vertical_alignment="center")
    with title_col:
        st.header("My Discogs Collection")
    with actions_col, st.container(horizontal=True, horizontal_alignment="right", gap="xxsmall"):
        _render_scan_button()
        _render_sync_matches_button()
        _render_rematch_button()
    if st.session_state.get("confirm_rematch"):
        _render_rematch_confirmation()

    all_rows = _all_rows()
    if not all_rows:
        st.info("No collection cached yet. Click Scan above, or run `discogs2ytmusic scan`.")
        return

    search = st.text_input(
        "Search by artist, track title, or release title", key="collection_search", placeholder="e.g. daft punk"
    )

    tag_options = _tag_options(all_rows)
    label_options = _label_options(all_rows)
    channel_options = _channel_options(all_rows)
    year_lo, year_hi = _year_bounds(all_rows)

    tag_groups, tag_groups_mode = _render_tag_group_filters(tag_options)

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        labels = st.multiselect("Label", label_options, key="collection_labels")
    with col2:
        channels = st.multiselect("Channel", channel_options, key="collection_channels")
    with col3:
        if year_lo < year_hi:
            year_range = st.slider(
                "Year", min_value=year_lo, max_value=year_hi, value=(year_lo, year_hi), key="collection_year"
            )
        else:
            st.write(f"Year: {year_lo}")  # a single distinct year — st.slider rejects min == max
            year_range = (year_lo, year_hi)
    with col4:
        st.write("")  # vertical alignment with the widgets above
        matched_only = st.checkbox("Matched only", key="collection_matched_only")

    narrowed = bool(tag_groups or labels or channels or matched_only or year_range != (year_lo, year_hi))
    filt = (
        PlaylistFilter(
            tag_groups=tag_groups,
            tag_groups_mode=tag_groups_mode,
            labels=labels,
            year_min=year_range[0] if year_range[0] > year_lo else None,
            year_max=year_range[1] if year_range[1] < year_hi else None,
            matched_only=matched_only,
            channels=channels,
        )
        if narrowed
        else None
    )

    with store.connect() as conn:
        rows = resolve_rows(conn, filt)
    rows = filter_rows_by_query(rows, search)

    st.caption(f"{len(rows)} tracks ({sum(1 for r in rows if r.matched)} matched)")
    select_all = st.checkbox(
        f"Select all {len(rows)} filtered track(s)", key="collection_select_all", disabled=not rows
    )

    df = _rows_to_dataframe(rows)
    table_key = _collection_table_key(rows)
    event = st.dataframe(
        df,
        key=table_key,
        hide_index=True,
        width="stretch",
        column_order=COLLECTION_COLUMNS,
        column_config=SHARED_COLUMN_CONFIG,
        on_select="rerun",
        selection_mode="multi-row",
    )
    selected_rows = [rows[i] for i in event.selection.rows]

    _render_edit_panel(selected_rows)

    # "Select all" overrides whatever's highlighted in the table rather than merely
    # pre-selecting it, so the table's own selection can't be used to carve out
    # exceptions from it — turn it off first to hand-pick a subset instead.
    if select_all:
        selected_ids = [r.track_id for r in rows if r.track_id is not None]
    else:
        selected_ids = [r.track_id for r in selected_rows if r.track_id is not None]
    _render_add_to_playlist(selected_ids, table_key)


def _folder_picker_options(conn: sqlite3.Connection) -> tuple[list[str], dict[str, int]]:
    folders = store.list_playlist_folders(conn)
    return [f["name"] for f in folders], {f["name"]: f["id"] for f in folders}


def _resolve_new_folder_choice(
    conn: sqlite3.Connection, choice: str, new_folder_name: str, folder_by_name: dict[str, int]
) -> tuple[int | None, str | None]:
    """Turn a folder-picker choice into a folder_id (or None for ungrouped), creating a new
    folder on the fly for the `_NEW_FOLDER_SENTINEL` choice.

    Returns:
        A (folder_id, error_message) pair — error_message is set (and folder_id is None)
        only when the "+ Create new folder" choice was made without a name.
    """
    if choice == _NO_FOLDER_SENTINEL:
        return None, None
    if choice == _NEW_FOLDER_SENTINEL:
        name = new_folder_name.strip()
        if not name:
            return None, "Enter a name for the new folder, or choose 'No folder'."
        existing = store.list_playlist_folders(conn)
        match = next((f for f in existing if f["name"] == name), None)
        folder_id = int(match["id"]) if match is not None else store.create_playlist_folder(conn, name)
        return folder_id, None
    return folder_by_name.get(choice), None


def _render_add_to_playlist(selected_ids: list[int], table_key: str) -> None:
    """Selected rows in the Collection tab's main table (or every filtered track, if
    "select all" is on) -> add to an existing or new playlist, optionally filing a newly
    created playlist into a folder."""

    with store.connect() as conn:
        playlist_names = [p["name"] for p in store.list_playlists(conn)]
        folder_names, folder_by_name = _folder_picker_options(conn)

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        choice = st.selectbox(
            f"Add {len(selected_ids)} selected track(s) to",
            [_NEW_PLAYLIST_SENTINEL, *playlist_names],
            key="collection_add_target",
        )
    new_name = ""
    with col2:
        if choice == _NEW_PLAYLIST_SENTINEL:
            new_name = st.text_input("New playlist name", key="collection_new_playlist_name")
    folder_choice = _NO_FOLDER_SENTINEL
    new_folder_name = ""
    with col3:
        if choice == _NEW_PLAYLIST_SENTINEL:
            folder_choice = st.selectbox(
                "Folder (optional)",
                [_NO_FOLDER_SENTINEL, _NEW_FOLDER_SENTINEL, *folder_names],
                key="collection_new_playlist_folder",
            )
            if folder_choice == _NEW_FOLDER_SENTINEL:
                new_folder_name = st.text_input("New folder name", key="collection_new_playlist_folder_name")
    with col4:
        st.write("")
        add_clicked = st.button("Add to playlist", key="collection_add_button", disabled=not selected_ids)

    if not add_clicked:
        return
    if not selected_ids:
        st.warning("No tracks selected.")
        return

    with store.connect() as conn:
        if choice == _NEW_PLAYLIST_SENTINEL:
            name = new_name.strip()
            if not name:
                st.error("Enter a name for the new playlist.")
                return
            folder_id, folder_error = _resolve_new_folder_choice(conn, folder_choice, new_folder_name, folder_by_name)
            if folder_error:
                st.error(folder_error)
                return
            try:
                playlist_id = store.create_playlist(conn, name)
            except sqlite3.IntegrityError:
                st.error(f"A playlist named '{name}' already exists.")
                return
            if folder_id is not None:
                store.set_playlist_folder(conn, playlist_id, folder_id)
        else:
            playlist_row = store.get_playlist_by_name(conn, choice)
            assert playlist_row is not None  # choice comes from the same list_playlists() call above
            playlist_id = playlist_row["id"]
            name = choice
        added = store.add_tracks_to_playlist(conn, playlist_id, selected_ids)
        conn.commit()

    st.success(f"Added {added} track(s) to '{name}'.")
    del st.session_state[table_key]
    st.rerun()


def _playlist_dataframe(rows: list[TrackRow]) -> pd.DataFrame:
    return _rows_to_dataframe(rows, flag_column="remove")


_SIDEBAR_NAV_CSS = """
<style>
.st-key-nav_top,
.st-key-nav_playlists,
[class*="st-key-nav_folder_playlists_"] {
    gap: 0.15rem !important;
}
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {
    gap: 0.4rem !important;
}
.st-key-nav_top hr {
    margin: 0.5rem 0 !important;
}
.st-key-nav_top [data-testid="stMarkdownContainer"]:has(hr) {
    margin-bottom: 0 !important;
}
.st-key-nav_top button,
.st-key-nav_playlists button {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.2rem 0 !important;
    min-height: 0 !important;
    width: 100% !important;
    justify-content: flex-start !important;
}
.st-key-nav_top button > div,
.st-key-nav_playlists button > div {
    justify-content: flex-start !important;
}
.st-key-nav_top button p,
.st-key-nav_playlists button p {
    color: #1F1E1D;
    text-align: left !important;
}
.st-key-nav_top button p {
    font-weight: 600;
    font-size: 0.95rem;
}
.st-key-nav_playlists {
    padding-left: 0.9rem;
}
.st-key-nav_playlists .stButton {
    line-height: 1.3;
}
.st-key-nav_playlists button {
    padding: 0.1rem 0 !important;
}
.st-key-nav_playlists button p {
    font-weight: 400;
    font-size: 0.85rem;
    line-height: 1.3;
}
.st-key-nav_top button:hover p,
.st-key-nav_playlists button:hover p {
    color: #CC785C;
}
[class*="st-key-nav_folder_playlists_"] {
    padding-left: 0.9rem;
}
.st-key-nav_selected button p {
    color: #CC785C !important;
    font-weight: 500 !important;
}
.st-key-nav_ytmusic {
    align-items: center !important;
}
.st-key-nav_ytmusic button {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.2rem 0 !important;
    min-height: 0 !important;
    justify-content: flex-start !important;
}
.st-key-nav_ytmusic button > div {
    justify-content: flex-start !important;
}
.st-key-nav_ytmusic button p {
    color: #1F1E1D;
    text-align: left !important;
    font-weight: 600;
    font-size: 0.95rem;
}
.st-key-nav_ytmusic button:hover p {
    color: #CC785C;
}
.yt-status-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.68rem;
    font-weight: 600;
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    white-space: nowrap;
}
.yt-status-pill::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 50%;
}
.yt-status-pill.is-connected {
    background: #E9F3EC;
    border: 1px solid #BFDCC7;
    color: #2F6D42;
}
.yt-status-pill.is-connected::before {
    background: #3A8451;
}
.yt-status-pill.is-off {
    background: #FFFFFF;
    border: 1px solid #E5E2D9;
    color: #87837A;
}
.yt-status-pill.is-off::before {
    background: transparent;
    border: 1.4px solid #87837A;
    width: 4px;
    height: 4px;
}
</style>
"""


def _nav_button(label: str, *, key: str, selected: bool, width: str = "stretch", **button_kwargs: object) -> bool:
    """Render a plain-text sidebar nav button, accent-colored via a scoped wrapper when selected."""
    if selected:
        with st.container(key="nav_selected"):
            return st.button(label, key=key, type="tertiary", width=width, **button_kwargs)  # type: ignore[arg-type]
    return st.button(label, key=key, type="tertiary", width=width, **button_kwargs)  # type: ignore[arg-type]


def _render_playlist_nav_button(playlist: sqlite3.Row, kind: str, selected_playlist_id: int | None) -> None:
    """Render one playlist's sidebar row, wherever it appears (ungrouped, or inside a folder)."""
    is_selected = kind == "playlist" and playlist["id"] == selected_playlist_id
    if _nav_button(playlist["name"], key=f"nav_playlist_{playlist['id']}", selected=is_selected):
        st.session_state["nav_kind"] = "playlist"
        st.session_state["nav_playlist_id"] = playlist["id"]
        st.rerun()


def _render_folder_nav_entry(
    folder: sqlite3.Row, playlists: list[sqlite3.Row], kind: str, selected_playlist_id: int | None
) -> None:
    """Render one playlist folder's sidebar row: same row style as a playlist entry, expanding
    and collapsing — via the same mechanism as the Playlists section itself — to reveal the
    playlists filed under it.
    """
    expanded_key = f"nav_folder_expanded_{folder['id']}"
    expanded = st.session_state.get(expanded_key, False)
    if st.button(
        folder["name"],
        key=f"nav_folder_{folder['id']}",
        type="tertiary",
        width="stretch",
        icon=":material/expand_more:" if expanded else ":material/chevron_right:",
    ):
        st.session_state[expanded_key] = not expanded
        st.rerun()

    if expanded:
        with st.container(key=f"nav_folder_playlists_{folder['id']}"):
            if not playlists:
                st.caption("No playlists in this folder yet.")
            for p in sorted(playlists, key=lambda p: p["name"].lower()):
                _render_playlist_nav_button(p, kind, selected_playlist_id)


def render_sidebar_nav() -> tuple[str, int | None]:
    """Render the sidebar: a Collection link, then a Playlists section listing playlist
    folders and ungrouped playlists together (one row per entry, alphabetically) — folders
    expand/collapse, like the Playlists section itself, to reveal the playlists filed under
    them. Returns the current selection as ("collection", None), ("playlist", id), or
    ("ytmusic", None).
    """
    with store.connect() as conn:
        playlists = store.list_playlists(conn)
        folders = store.list_playlist_folders(conn)
    playlist_ids = {p["id"] for p in playlists}

    kind = st.session_state.get("nav_kind", "collection")
    playlist_id = st.session_state.get("nav_playlist_id")
    stale_playlist = kind == "playlist" and playlist_id not in playlist_ids
    if stale_playlist or kind not in ("collection", "playlist", "ytmusic"):
        kind, playlist_id = "collection", None

    expanded = st.session_state.get("nav_playlists_expanded", True)

    ungrouped = [p for p in playlists if p["folder_id"] is None]
    playlists_by_folder: dict[int, list[sqlite3.Row]] = {}
    for p in playlists:
        if p["folder_id"] is not None:
            playlists_by_folder.setdefault(p["folder_id"], []).append(p)

    with st.sidebar:
        st.markdown(_SIDEBAR_NAV_CSS, unsafe_allow_html=True)

        with st.container(key="nav_top"):
            if _nav_button("My Discogs Collection", key="nav_collection", selected=kind == "collection"):
                st.session_state["nav_kind"] = "collection"
                st.session_state["nav_playlist_id"] = None
                st.rerun()

            st.divider()

            if st.button(
                "Playlists",
                key="nav_playlists_toggle",
                type="tertiary",
                width="stretch",
                icon=":material/expand_more:" if expanded else ":material/chevron_right:",
            ):
                st.session_state["nav_playlists_expanded"] = not expanded
                st.rerun()

        if expanded:
            with st.container(key="nav_playlists"):
                if not playlists and not folders:
                    st.caption("No playlists yet — create one from the Collection view.")
                entries: list[tuple[str, int, str]] = sorted(
                    [(f["name"], f["id"], "folder") for f in folders]
                    + [(p["name"], p["id"], "playlist") for p in ungrouped],
                    key=lambda entry: entry[0].lower(),
                )
                for _name, entry_id, entry_kind in entries:
                    if entry_kind == "folder":
                        folder = next(f for f in folders if f["id"] == entry_id)
                        _render_folder_nav_entry(folder, playlists_by_folder.get(entry_id, []), kind, playlist_id)
                    else:
                        playlist = next(p for p in ungrouped if p["id"] == entry_id)
                        _render_playlist_nav_button(playlist, kind, playlist_id)

        st.divider()
        _render_ytmusic_nav_item(kind == "ytmusic")

    return kind, playlist_id


def _ytmusic_connected() -> bool:
    """Whether YT Music should be shown as connected: auth headers are saved, and no YT Music
    call this session has failed in a way that suggests that saved session has gone stale
    (see `_mark_ytmusic_auth_suspect`)."""
    return ytmusic_client.is_authenticated() and not st.session_state.get("ytmusic_auth_suspect", False)


def _mark_ytmusic_auth_suspect() -> None:
    """Flag the saved YT Music session as suspect after a failed request, so the sidebar pill
    and YT Music page drop to "Not connected" until the user re-saves fresh headers — a failure
    this shape (once past the auth-file-exists check) usually means the cookie/x-goog-authuser
    session has expired or rotated, not a one-off network blip.

    The sidebar itself has already rendered earlier in this same script run, so the pill picks
    this up starting the *next* rerun rather than instantly — deliberately not forcing an
    st.rerun() here, since that would blow away the st.error() explaining what just failed.
    """
    st.session_state["ytmusic_auth_suspect"] = True


def _render_ytmusic_nav_item(selected: bool) -> None:
    """Sidebar nav row linking to the dedicated YT Music page, with a status pill showing
    whether an account is currently connected."""
    authenticated = _ytmusic_connected()
    with st.container(key="nav_ytmusic", horizontal=True, gap="small"):
        if _nav_button("YT Music", key="nav_ytmusic_btn", selected=selected, width="content"):
            st.session_state["nav_kind"] = "ytmusic"
            st.session_state["nav_playlist_id"] = None
            st.rerun()
        pill_class = "is-connected" if authenticated else "is-off"
        pill_text = "Connected" if authenticated else "Not connected"
        st.markdown(f'<span class="yt-status-pill {pill_class}">{pill_text}</span>', unsafe_allow_html=True)


def _render_ytmusic_page() -> None:
    """Dedicated page to connect (or re-connect) a YT Music account.

    A two-column guide: the DevTools steps on the left, connection status and the
    paste-headers form on the right — so nothing scrolls out of view while copying values
    back and forth between this page and the browser's DevTools panel. Submits through the
    same `ytmusic_client.save_auth_headers` the CLI's `auth ytmusic` command uses, so both
    write `YTMUSIC_AUTH_FILE` identically — this page is additive for the UI-only workflow,
    not a replacement for the CLI command.

    Also hosts a minimal text input for `Config.playlist_name_prefix` — a deliberately
    unstyled placement until a real Settings page exists (tracked separately in #46).
    """
    st.subheader("YT Music")
    st.caption("Connect your account so matched tracks can sync to real playlists.")

    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown("**How to get your header values**")
        st.markdown("\n".join(f"{i}. {step}" for i, step in enumerate(ytmusic_client.SETUP_STEPS, 1)))

    with right:
        if _ytmusic_connected():
            st.success("Connected", icon=":material/check_circle:")
        else:
            st.info("Not connected", icon=":material/link_off:")

        cookie = st.text_input(
            "cookie",
            key="ytmusic_auth_cookie_input",
            placeholder="__Secure-3PAPISID=…; SID=…",
        )
        authuser = st.text_input(
            "x-goog-authuser",
            key="ytmusic_auth_authuser_input",
            placeholder="0",
        )
        if st.button("Save", key="ytmusic_auth_save"):
            try:
                ytmusic_client.save_auth_headers(cookie, authuser)
            except ValueError as e:
                st.error(str(e))
            except ytmusic_client.YTMusicAuthError as e:
                st.error(f"Could not authenticate: {e}")
            else:
                st.session_state["ytmusic_auth_suspect"] = False
                st.success("YT Music connected.")
                st.rerun()

        st.divider()
        st.markdown("**Pushed playlist name**")
        # Minimal, unstyled placement for now — a proper Settings page (and any visual
        # redesign around it) is tracked separately in #46, not part of this feature.
        cfg = Config.load()
        prefix = st.text_input(
            "Prefix",
            value=cfg.playlist_name_prefix,
            key="playlist_name_prefix_input",
            help="Pushed playlists are named '<prefix> - <playlist name>'.",
        )
        if st.button("Save prefix", key="playlist_name_prefix_save"):
            cfg.playlist_name_prefix = prefix
            cfg.save()
            st.success("Playlist name prefix saved.")


def _relative_time(timestamp: float) -> str:
    """Format a unix timestamp as a coarse "N unit(s) ago" string, relative to now."""
    seconds = max(0.0, time.time() - timestamp)
    for unit_seconds, unit_name in (
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
    ):
        count = int(seconds // unit_seconds)
        if count >= 1:
            return f"{count} {unit_name}{'s' if count != 1 else ''} ago"
    return "just now"


def _render_playlist_folder_picker(playlist: sqlite3.Row) -> None:
    """Let the user file a curated playlist into a folder (existing or new), or move it back
    to ungrouped, from its detail view."""
    playlist_id = playlist["id"]
    with store.connect() as conn:
        folder_names, folder_by_name = _folder_picker_options(conn)
    folder_by_id = {folder_id: name for name, folder_id in folder_by_name.items()}
    current_choice = folder_by_id.get(playlist["folder_id"], _NO_FOLDER_SENTINEL)
    options = [_NO_FOLDER_SENTINEL, _NEW_FOLDER_SENTINEL, *folder_names]

    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        choice = st.selectbox(
            "Folder",
            options,
            index=options.index(current_choice) if current_choice in options else 0,
            key=f"playlist_folder_choice_{playlist_id}",
        )
    new_folder_name = ""
    with col2:
        if choice == _NEW_FOLDER_SENTINEL:
            new_folder_name = st.text_input("New folder name", key=f"playlist_folder_new_name_{playlist_id}")
    with col3:
        st.write("")
        move_clicked = st.button("Move", key=f"playlist_folder_move_{playlist_id}")

    if not move_clicked:
        return
    with store.connect() as conn:
        folder_id, error = _resolve_new_folder_choice(conn, choice, new_folder_name, folder_by_name)
        if error:
            st.error(error)
            return
        store.set_playlist_folder(conn, playlist_id, folder_id)
        conn.commit()
    st.rerun()


def _render_playlist_detail(playlist: sqlite3.Row) -> None:
    playlist_id = playlist["id"]
    with store.connect() as conn:
        rows = resolve_playlist_rows(conn, playlist_id)

    matched = sum(1 for r in rows if r.matched)

    st.markdown(_ACTION_PILL_CSS, unsafe_allow_html=True)
    title_col, actions_col = st.columns([3, 2], vertical_alignment="center")
    with title_col:
        st.subheader(playlist["name"])
    with actions_col, st.container(horizontal=True, horizontal_alignment="right", gap="xxsmall"):
        _render_sync_button(playlist, rows)
        _render_delete_button(playlist)

    if st.session_state.get(f"confirm_sync_{playlist_id}"):
        _render_sync_confirmation(playlist, rows)
    if st.session_state.get(f"confirm_delete_{playlist_id}"):
        _render_delete_confirmation(playlist)

    track_summary = f"{len(rows)} track(s), {matched} matched"
    ytmusic_playlist_id = playlist["ytmusic_playlist_id"]
    if ytmusic_playlist_id:
        playlist_url = f"https://music.youtube.com/playlist?list={ytmusic_playlist_id}"
        st.markdown(
            f'<div style="font-size: 0.875rem; color: rgba(49, 51, 63, 0.6); margin-bottom: 0.5rem;">'
            f"{track_summary} · "
            f'<a href="{playlist_url}" target="_blank" rel="noopener" style="font-weight: 700;">'
            f"Linked to YT Music playlist</a></div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption(track_summary)

    pushed_at = playlist["pushed_at"]
    pushed_caption = f"Pushed to YT Music {_relative_time(pushed_at)}" if pushed_at else "Not yet pushed to YT Music"
    st.caption(f"{pushed_caption} · Last modified {_relative_time(playlist['updated_at'])}")

    _render_playlist_folder_picker(playlist)

    if rows:
        df = _playlist_dataframe(rows)
        editor_key = f"playlist_editor_{playlist_id}"
        edited_df = st.data_editor(
            df,
            key=editor_key,
            hide_index=True,
            width="stretch",
            column_order=PLAYLIST_TRACK_COLUMNS,
            disabled=[c for c in PLAYLIST_TRACK_COLUMNS if c not in ("remove", "styles", "genres")],
            column_config={
                **SHARED_COLUMN_CONFIG,
                "remove": st.column_config.CheckboxColumn("", help="Select tracks to remove from this playlist"),
            },
        )

        with store.connect() as conn:
            n_style = apply_style_edits(conn, df, edited_df)
            n_genre = apply_genre_edits(conn, df, edited_df)
        n_corrections = n_style + n_genre
        if n_corrections:
            st.success(f"Saved {n_corrections} correction(s).")
            del st.session_state[editor_key]
            st.rerun()

        to_remove = _selected_track_ids(edited_df, "remove")
        if st.button(f"Remove {len(to_remove)} selected", key=f"remove_button_{playlist_id}", disabled=not to_remove):
            with store.connect() as conn:
                store.remove_tracks_from_playlist(conn, playlist_id, to_remove)
                conn.commit()
            del st.session_state[editor_key]
            st.rerun()
    else:
        st.caption("No tracks yet — search below to add some.")

    st.markdown("**Add tracks**")
    search = st.text_input("Search your collection by artist or title", key=f"playlist_search_{playlist_id}")
    if search.strip():
        needle = search.strip().lower()
        already_in = {r.track_id for r in rows}
        matches = [
            r
            for r in _all_rows()
            if r.track_id is not None
            and r.track_id not in already_in
            and (needle in r.track_artist.lower() or needle in r.track_title.lower())
        ][:50]
        if not matches:
            st.caption("No matching tracks found.")
        else:
            search_df = _rows_to_dataframe(matches, flag_column="select")
            edited_search_df = st.data_editor(
                search_df,
                key=f"playlist_search_editor_{playlist_id}",
                hide_index=True,
                width="stretch",
                column_order=["select", "track_artist", "track_title", "release_title", "matched"],
                disabled=["track_artist", "track_title", "release_title", "matched"],
                column_config={**SHARED_COLUMN_CONFIG, "select": st.column_config.CheckboxColumn("")},
            )
            to_add = _selected_track_ids(edited_search_df, "select")
            if st.button(f"Add {len(to_add)} selected", key=f"add_from_search_{playlist_id}", disabled=not to_add):
                with store.connect() as conn:
                    added = store.add_tracks_to_playlist(conn, playlist_id, to_add)
                    conn.commit()
                st.success(f"Added {added} track(s).")
                del st.session_state[f"playlist_search_editor_{playlist_id}"]
                st.rerun()


def _render_sync_button(playlist: sqlite3.Row, rows: list[TrackRow]) -> None:
    playlist_id = playlist["id"]
    video_ids = [r.video_id for r in rows if r.video_id]

    if not video_ids:
        st.caption("No matched tracks to push yet.")
        return

    confirm_key = f"confirm_sync_{playlist_id}"
    if st.session_state.get(confirm_key):
        return
    with st.container(key="sync_pill", width="content"):
        if st.button(
            "Sync",
            key=f"sync_button_{playlist_id}",
            type="primary",
            icon=":material/sync:",
            help=f"Push {len(video_ids)} track(s) to YT Music",
        ):
            st.session_state[confirm_key] = True
            st.rerun()


def _duplicate_video_groups(rows: list[TrackRow]) -> list[list[TrackRow]]:
    """Group matched rows that share the same YouTube video id — usually two different Discogs
    tracks accidentally matched to the same video, worth a second look before syncing (a shared
    video id can also make YT Music reject a whole add request if left undeduped downstream)."""
    by_video: dict[str, list[TrackRow]] = {}
    for r in rows:
        if r.video_id:
            by_video.setdefault(r.video_id, []).append(r)
    return [group for group in by_video.values() if len(group) > 1]


def _stray_remote_tracks(remote_tracks: list[dict[str, Any]], video_ids: list[str]) -> list[dict[str, Any]]:
    """Remote playlist tracks whose video id isn't in the local track set — these get removed."""
    local = set(video_ids)
    return [t for t in remote_tracks if t.get("videoId") not in local]


def _get_playlist_tracks_with_retry(yt: YTMusic, playlist_id: str) -> list[dict[str, Any]]:
    """`get_playlist_tracks`, retrying a couple of times on the transient "browse response missing
    expected keys" shape (see `_YTMUSIC_MISSING_PLAYLIST_ERRORS`).

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


def _diff_and_sync_ytmusic(yt: YTMusic, ytmusic_id: str, video_ids: list[str]) -> list[dict[str, Any]]:
    """Push tracks missing on `ytmusic_id`, remove remote tracks no longer present locally, and
    return the removed tracks."""
    remote_tracks = _get_playlist_tracks_with_retry(yt, ytmusic_id)
    remote_video_ids = {t["videoId"] for t in remote_tracks if t.get("videoId")}
    to_add = [v for v in video_ids if v not in remote_video_ids]
    to_remove = _stray_remote_tracks(remote_tracks, video_ids)
    ytmusic_client.add_tracks(yt, ytmusic_id, to_add)
    ytmusic_client.remove_tracks(yt, ytmusic_id, to_remove)
    return to_remove


# ytmusicapi's `get_playlist` (used by `get_playlist_tracks`) doesn't raise a clean YTMusicError
# for a deleted/invalid playlist id — the browse response it gets back is just missing the keys
# a real playlist's response would have, and its internal `nav()` helper raises a bare KeyError
# (or IndexError, depending on the exact shape) instead. Both need to be treated the same as a
# YTMusicError for the stale-id recovery below to actually trigger on this failure mode.
_YTMUSIC_MISSING_PLAYLIST_ERRORS: tuple[type[Exception], ...] = (YTMusicError, KeyError, IndexError)
_YTMUSIC_PUSH_ERRORS: tuple[type[Exception], ...] = (RuntimeError, *_YTMUSIC_MISSING_PLAYLIST_ERRORS)

# A bare KeyError/IndexError (see above) means "browse response missing expected keys" — that's
# also what a brand-new playlist looks like before YT Music finishes indexing it server-side (see
# `_get_playlist_tracks_with_retry`), which has nothing to do with auth. Only RuntimeError/
# YTMusicError — errors ytmusicapi/`ytmusic_client` actually raise on rejection — are treated as
# evidence the saved session itself is bad; don't flip the UI to "cookie expired" on the ambiguous
# KeyError/IndexError shape alone.
_YTMUSIC_AUTH_SUSPECT_ERRORS: tuple[type[Exception], ...] = (RuntimeError, YTMusicError)


def _push_to_ytmusic(
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
        removed = _diff_and_sync_ytmusic(yt, ytmusic_id, video_ids)
    except _YTMUSIC_MISSING_PLAYLIST_ERRORS:
        if not existing_id:
            raise
        with store.connect() as conn:
            ytmusic_id, _created = ytmusic_client.get_or_create_playlist(
                yt, playlist_name, description="Curated from the Discogs collection app"
            )
            store.set_playlist_ytmusic_id(conn, playlist_id, ytmusic_id)
            conn.commit()
        removed = _diff_and_sync_ytmusic(yt, ytmusic_id, video_ids)

    with store.connect() as conn:
        store.set_playlist_pushed_at(conn, playlist_id)
        conn.commit()
    return removed


def _render_sync_confirmation(playlist: sqlite3.Row, rows: list[TrackRow]) -> None:
    playlist_id = playlist["id"]
    video_ids = [r.video_id for r in rows if r.video_id]
    confirm_key = f"confirm_sync_{playlist_id}"
    extra_confirm_key = f"confirm_sync_extra_{playlist_id}"
    playlist_name = f"{Config.load().playlist_name_prefix} - {playlist['name']}"
    already_linked = bool(playlist["ytmusic_playlist_id"])

    st.warning(
        "This will create (or update) a real playlist on your YT Music account "
        f"named '{playlist_name}' with these {len(video_ids)} track(s)."
    )

    for group in _duplicate_video_groups(rows):
        names = ", ".join(f"{r.track_artist} - {r.track_title}" for r in group)
        st.warning(f"Same YouTube video matched to {len(group)} tracks: {names}.")

    # Only the *first* link to a playlist can silently attach to a pre-existing YT Music
    # playlist that happens to share the generated name — once ytmusic_playlist_id is saved,
    # later syncs know exactly which remote playlist they're diffing against, so no need to
    # re-check every render.
    stray_count = 0
    if not already_linked and _ytmusic_connected():
        try:
            yt = ytmusic_client.get_client(authenticated=True)
            found_id = ytmusic_client.find_playlist(yt, playlist_name)
            if found_id is not None:
                stray_count = len(_stray_remote_tracks(ytmusic_client.get_playlist_tracks(yt, found_id), video_ids))
        except _YTMUSIC_PUSH_ERRORS:
            pass  # surfaced again, more clearly, if the user proceeds and it still fails

    needs_extra_confirm = stray_count > 0
    if needs_extra_confirm:
        st.warning(
            f"Found an existing YT Music playlist '{playlist_name}' with {stray_count} track(s) "
            "not in your local playlist — syncing will remove them."
        )

    col1, col2 = st.columns(2)
    with col1:
        awaiting_extra_confirm = needs_extra_confirm and not st.session_state.get(extra_confirm_key, False)
        label = "Yes, remove them and push" if awaiting_extra_confirm else "Yes, push to YT Music"
        if st.button(label, key=f"confirm_sync_yes_{playlist_id}"):
            if awaiting_extra_confirm:
                st.session_state[extra_confirm_key] = True
                st.rerun()
            st.session_state[confirm_key] = False
            st.session_state.pop(extra_confirm_key, None)
            if not _ytmusic_connected():
                st.error("Not authenticated with YT Music. Use the YT Music page (in the sidebar) to connect.")
                return
            try:
                yt = ytmusic_client.get_client(authenticated=True)
                to_remove = _push_to_ytmusic(yt, playlist, playlist_id, playlist_name, video_ids)
            except _YTMUSIC_PUSH_ERRORS as e:
                if isinstance(e, _YTMUSIC_AUTH_SUSPECT_ERRORS):
                    _mark_ytmusic_auth_suspect()
                    st.error(
                        f"YT Music rejected the request: {e}\n\n"
                        "This usually means the saved cookie/x-goog-authuser session has expired or "
                        "rotated. Re-connect on the YT Music page (in the sidebar) with a fresh copy "
                        "of those two header values and try again."
                    )
                else:
                    st.error(
                        f"YT Music returned an unexpected response: {e}\n\n"
                        "This can happen right after creating a new playlist, before YT Music has "
                        "finished indexing it server-side. Wait a few seconds and try again."
                    )
                return
            summary = f"Synced {len(video_ids)} track(s) to '{playlist_name}'"
            if to_remove:
                summary += f", removed {len(to_remove)} track(s) no longer in the playlist"
            st.success(summary + ".")
            st.rerun()
    with col2:
        if st.button("Cancel", key=f"confirm_sync_no_{playlist_id}"):
            st.session_state[confirm_key] = False
            st.session_state.pop(extra_confirm_key, None)
            st.rerun()


def _render_delete_button(playlist: sqlite3.Row) -> None:
    playlist_id = playlist["id"]
    confirm_key = f"confirm_delete_{playlist_id}"

    if st.session_state.get(confirm_key):
        return
    with st.container(key="delete_pill", width="content"):
        if st.button(
            "Delete",
            key=f"delete_button_{playlist_id}",
            icon=":material/delete:",
            help="Delete this playlist",
        ):
            st.session_state[confirm_key] = True
            st.rerun()


def _render_delete_confirmation(playlist: sqlite3.Row) -> None:
    playlist_id = playlist["id"]
    confirm_key = f"confirm_delete_{playlist_id}"

    st.warning(
        "This removes the playlist from this app only — it does NOT delete the linked "
        "YT Music playlist, if any. Continue?"
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, delete", key=f"confirm_delete_yes_{playlist_id}"):
            with store.connect() as conn:
                store.delete_playlist(conn, playlist_id)
                conn.commit()
            st.session_state[confirm_key] = False
            st.session_state["nav_kind"] = "collection"
            st.session_state["nav_playlist_id"] = None
            st.rerun()
    with col2:
        if st.button("Cancel", key=f"confirm_delete_no_{playlist_id}"):
            st.session_state[confirm_key] = False
            st.rerun()


def main() -> None:
    """Streamlit entry point — a sidebar (Collection + Playlists) driving the main content pane."""
    kind, playlist_id = render_sidebar_nav()
    if kind == "playlist" and playlist_id is not None:
        with store.connect() as conn:
            playlist = store.get_playlist(conn, playlist_id)
        assert playlist is not None  # render_sidebar_nav already dropped stale/deleted ids
        _render_playlist_detail(playlist)
    elif kind == "ytmusic":
        _render_ytmusic_page()
    else:
        render_collection_tab()


main()
