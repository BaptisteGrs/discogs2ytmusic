"""The Streamlit UI: a browsable/editable view over the same sqlite cache the CLI uses.

`main` renders a sidebar (`render_sidebar_nav`) that picks between several panes — the
Collection tab (`render_collection_tab`), an Other-source page (`_render_other_source_page`
— a label/wantlist/seller/other user's collection, kept strictly separate from the Collection
tab's own tracks and playlists), a playlist's detail view, or the YT Music connection
page — each a thin view over `filters.resolve_rows`/`resolve_playlist_rows` and `store`.
`_render_source_browser` is the shared renderer behind both the Collection tab and every
Other-source page. In-place edits to a rendered table are persisted as manual corrections
via `collection_edits`; Scan/Sync/Rematch/push-to-YT-Music all drive `scan_engine`/
`sync_engine`, so none of that orchestration logic lives here — this module is just the
view layer around it, plus the confirmation dialogs and error messages a UI needs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import time
from typing import Any, cast

import pandas as pd
import streamlit as st

from discogs2ytmusic import discogs_taxonomy, scan_engine, store, sync_engine, ytmusic_client
from discogs2ytmusic.collection_edits import (
    apply_artist_edits,
    apply_genre_edits,
    apply_style_edits,
    apply_video_link_edits,
)
from discogs2ytmusic.config import Config
from discogs2ytmusic.discogs import DiscogsClient, DiscogsError, is_my_wantlist_url, parse_source_url, source_url
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

# `theme.font` in `.streamlit/config.toml` sets the CSS variable Streamlit's own body/widget
# styles are supposed to read, but (as of streamlit 1.63) that variable never reaches plain
# body text when a `theme.headingFont` source is also configured — headings pick up
# `headingFont` correctly, everything else silently falls back to the browser default serif.
# Setting it here directly is the reliable fix; drop this once upstream is fixed.
st.markdown(
    """
    <style>
    html, body, [data-testid="stAppViewContainer"] {
        font-family: "Source Sans 3", sans-serif;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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

# Labels/help text for columns shared across the Collection tab and both Playlist-tab tables
# (`render_collection_tab`, `_render_playlist_detail`'s tracks table and its "Add tracks" search
# results table) — keeps the same field looking identical everywhere it's rendered. All three
# tables are read-only `st.dataframe`s with native row selection (#57/#60); per-track corrections
# are made through `_render_edit_panel`, not inline cell-editing.
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


def _source_rows(source_type: str = "collection", source_key: str = "") -> list[TrackRow]:
    with store.connect() as conn:
        return resolve_rows(conn, source_type=source_type, source_key=source_key)


def _source_display_name(conn: sqlite3.Connection, source_type: str, source_key: str) -> str:
    """Human-readable name for a source_type/source_key pair: "My Discogs Collection" for
    the implicit collection source, else the matching Other-sources page's own name."""
    if source_type == "collection":
        return "My Discogs Collection"
    other = store.get_other_source_by_key(conn, source_type, source_key)
    return other["display_name"] if other is not None else f"{source_type}:{source_key}"


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


def _rows_to_dataframe(rows: list[TrackRow]) -> pd.DataFrame:
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
    # Pass `columns=` explicitly so an empty row set still yields a dataframe with the
    # expected columns instead of a columnless one that breaks callers (e.g. `.set_index(...)`
    # in tests) expecting them to be present.
    return pd.DataFrame(records, columns=list(_TRACK_ROW_COLUMNS))


def _load_discogs_client() -> tuple[DiscogsClient, str] | None:
    """Build a Discogs client from saved credentials, or None if `auth discogs` hasn't been run yet."""
    cfg = Config.load()
    if not cfg.discogs_token or not cfg.discogs_username:
        return None
    return DiscogsClient(cfg.discogs_token), cfg.discogs_username


def _run_scan(refresh: bool, source_type: str = "collection", source_key: str = "") -> bool:
    """Fetch a Discogs source's releases (+ tracklists) into the cache — the UI equivalent
    of `scan --refresh`, generalized to any source. Shows its own progress bar and
    error/success messages; the caller decides whether/how to refresh the page afterward.

    `source_type`/`source_key` default to "my own collection". For any other source (an
    Other-source page's own type/key), picks the matching Discogs listing endpoint and
    per-item scan function — see `scan_engine.scan_release`/`scan_label_release` — and
    applies that source's persisted Style/Format/Year pre-filter (`scan_engine.ImportFilter`),
    if it has one, pruning any previously-imported release that no longer matches.

    A release that Discogs itself can't return anymore (its detail fetch 404s — common in a
    wantlist, since people wantlist releases that later get merged into a different release
    id or pulled from the database entirely; rarer but possible in a collection/label/seller
    listing too) is skipped rather than aborting the whole scan — earlier releases already
    committed to the cache would otherwise be all a failure like that left behind.

    Returns:
        True if the scan completed, False if it couldn't start (no Discogs credentials
        saved, or the Discogs API call itself failed).
    """
    creds = _load_discogs_client()
    if creds is None:
        st.error("Not authenticated with Discogs. Run `discogs2ytmusic auth discogs` first.")
        return False
    client, username = creds

    import_filter = None
    if source_type != "collection":
        with store.connect() as conn:
            source_row = store.get_other_source_by_key(conn, source_type, source_key)
        if source_row is not None:
            import_filter = scan_engine.ImportFilter.from_dict(json.loads(source_row["filter_json"]))

    try:
        with st.spinner("Fetching listing..."):
            if source_type == "collection":
                items = list(client.iter_collection_basic(username))
            elif source_type == "user_collection":
                items = list(client.iter_collection_basic(source_key))
            elif source_type == "wantlist":
                items = list(client.iter_wantlist_basic(source_key))
            elif source_type == "label":
                items = list(client.iter_label_releases(int(source_key)))
            elif source_type == "seller":
                items = list(client.iter_seller_inventory(source_key))
            else:
                raise ValueError(f"Unknown source type: {source_type}")
    except DiscogsError as e:
        st.error(f"Could not fetch from Discogs: {e}")
        return False

    total = len(items)
    progress = st.progress(0.0, text=f"Fetching tracklists... (0/{total})")
    matched_ids: set[int] = set()
    failed = 0
    with store.connect() as conn:
        for i, item in enumerate(items, start=1):
            try:
                if source_type in ("label", "seller"):
                    release_id = item["id"]
                    kept = scan_engine.scan_label_release(
                        conn,
                        client,
                        item,
                        refresh,
                        source_key=source_key,
                        source_type=source_type,
                        import_filter=import_filter,
                    )
                else:
                    release_id = item["basic_information"]["id"]
                    kept = scan_engine.scan_release(
                        conn,
                        client,
                        item,
                        refresh,
                        source_type=source_type,
                        source_key=source_key,
                        import_filter=import_filter,
                    )
            except DiscogsError:
                failed += 1
            else:
                if kept:
                    matched_ids.add(release_id)
            progress.progress(i / total if total else 1.0, text=f"Fetching tracklists... ({i}/{total})")
    progress.empty()

    if import_filter is not None and not import_filter.is_empty():
        with store.connect() as conn:
            store.prune_release_source_tags(conn, source_type, source_key, matched_ids)
            conn.commit()

    if failed:
        st.warning(f"Skipped {failed} release(s) Discogs couldn't return (removed or merged listings).")
    st.success(f"Scanned {total - failed} release(s).")
    return True


def _run_sync_matches(source_type: str = "collection", source_key: str = "") -> bool:
    """Match any unmatched tracks against YouTube/YT Music — the UI equivalent of `sync`,
    scoped to one Discogs source (defaults to "my own collection").

    Only populates the match cache; it never touches a real YT Music account (pushing a
    playlist to one has its own confirm-gated button in the Playlists tab).

    Returns:
        True if matching ran (even if it matched nothing), False if there was nothing to match.
    """
    with store.connect() as conn:
        releases_with_tracks = list(
            store.iter_releases_with_tracks(conn, source_type=source_type, source_key=source_key)
        )
        total = sum(len(store.effective_track_queries(r, t)) for r, t in releases_with_tracks)
        if total == 0:
            st.info("Nothing to match yet — scan this source first.")
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


def _render_scan_button(source_type: str = "collection", source_key: str = "") -> None:
    # A plain st.button's key never needs page-scoping (unlike a filter widget's) — its
    # "clicked" state doesn't persist across reruns, and only one page's browser renders
    # per rerun (see main()'s dispatch), so reusing this literal key across every
    # Collection/Other-source page is safe and keeps the existing CSS/tests working.
    with st.container(key="scan_pill", width="content"):
        clicked = st.button(
            "Scan",
            key="scan_button",
            type="primary",
            icon=":material/cloud_sync:",
            help="Re-fetch this source's releases and tracklists from Discogs",
        )
    if clicked and _run_scan(refresh=True, source_type=source_type, source_key=source_key):
        st.session_state.pop("collection_editor", None)
        st.rerun()


def _render_sync_matches_button(source_type: str = "collection", source_key: str = "") -> None:
    with st.container(key="sync_matches_pill", width="content"):
        clicked = st.button(
            "Sync matches",
            key="sync_matches_button",
            icon=":material/search:",
            help="Match any unmatched tracks against YouTube/YT Music (doesn't touch your YT Music account)",
        )
    if clicked and _run_sync_matches(source_type=source_type, source_key=source_key):
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


def _row_digest(rows: list[TrackRow]) -> str:
    """Short digest identifying a row set by track_id.

    Shared by every `st.dataframe` table (Collection tab, and a playlist detail view's
    tracks/search tables) that must reset its native row-selection state — rather than
    silently misapply stale state to the wrong row — whenever what's visible changes,
    whether from a filter (#19) or a different search query/playlist (#60).
    """
    ids = ",".join(str(r.track_id) for r in rows)
    return hashlib.sha1(ids.encode()).hexdigest()[:12]


def _table_key(prefix: str, rows: list[TrackRow]) -> str:
    """Derive an `st.dataframe` widget key from `prefix` and the currently visible row set.

    `st.dataframe`'s native row-selection state matches selected rows to the previous
    render by row *position*, not row identity. Reusing one static key across
    differently-shaped row sets lets a stale selection apply to the wrong row once what's
    visible changes, or point past the end of a narrower dataframe (#19, #60). Keying on
    the row set's track_ids forces a fresh widget — with no pending selection — whenever
    the visible rows change. `prefix` scopes the key to a particular table (the Collection
    tab's main table, or a specific playlist's tracks/search table) so two tables rendered
    in the same session can never collide.
    """
    return f"{prefix}_{_row_digest(rows)}"


def _render_edit_panel(selected_rows: list[TrackRow], key_prefix: str = "collection") -> None:
    """Edit artist/styles/genres/YouTube match for exactly one currently-selected track.

    Corrections moved here (out of inline cell-editing) once the main table switched to
    `st.dataframe` for real shift-click range selection (#57) — `st.dataframe` itself is
    read-only, so it can't host in-place editing the way `st.data_editor` did. Scoped to
    a single selected row: artist and YouTube-link corrections are inherently per-track,
    so there's no obviously correct bulk semantic once more than one row is selected.

    `key_prefix` namespaces the underlying widget/session-state keys so the Collection tab
    and a given playlist's detail view — either of which can render this panel in the same
    session — never collide (#60).

    Each field also gets a "Reset" button (enabled only once that field actually carries a
    correction — see `TrackRow.artist_overridden`/`styles_overridden`/`genres_overridden`/
    `video_overridden`), for reverting just that one field instead of the whole row (#66).
    Artist/Styles/Genres reset the same way blanking-and-saving the field already did — clear
    the override, fall back to Discogs. YouTube link reset is different: blanking-and-saving
    that field means "reject, this track has no match" (`apply_video_link_edits`), which is
    sticky — `sync`/`rematch` both leave a rejected match alone. Reset instead deletes the
    cached match outright and searches again immediately, so an accidentally-cleared link can
    be recovered without dropping to the CLI's `correct --clear`.
    """
    if len(selected_rows) != 1:
        if selected_rows:
            st.caption(f"{len(selected_rows)} tracks selected. Select exactly one to edit its details.")
        else:
            st.caption("Select a track above to edit its artist, styles, genres, or YouTube match.")
        return

    row = selected_rows[0]
    row_key = f"{key_prefix}_edit_{row.track_id if row.track_id is not None else f'release_{row.release_id}'}"

    # A text_input's `value=` argument only seeds its *first-ever* render for a given
    # `key` — once mounted, the widget keeps whatever the user (or a prior rerun) left in
    # it regardless of a later `value=` change, and merely clearing `st.session_state[key]`
    # doesn't reliably force a resync either (the frontend component can retain its last
    # displayed text across a rerun that doesn't touch its `key`). So each field's actual
    # widget key carries a generation counter that a reset bumps, forcing a genuinely new
    # widget instance next render — the only reliable way to make it redisplay the fresh
    # Discogs/re-searched value instead of the correction that was just discarded.
    artist_gen = st.session_state.get(f"{row_key}_artist_gen", 0)
    styles_gen = st.session_state.get(f"{row_key}_styles_gen", 0)
    genres_gen = st.session_state.get(f"{row_key}_genres_gen", 0)
    video_gen = st.session_state.get(f"{row_key}_video_gen", 0)

    with st.form(key=row_key):
        st.caption(f"Editing **{row.track_artist} — {row.track_title}**")

        artist_col, artist_reset_col = st.columns([5, 1])
        with artist_col:
            artist = st.text_input(
                "Track Artist",
                value=row.track_artist,
                key=f"{row_key}_artist_{artist_gen}",
                help="Edit to override the artist used for this track's YouTube search",
            )
        with artist_reset_col:
            st.write("")  # align with the labeled input above
            reset_artist = st.form_submit_button(
                "Reset",
                key=f"{row_key}_reset_artist",
                icon=":material/restart_alt:",
                help="Discard the artist correction and use the Discogs-sourced artist again",
                disabled=not row.artist_overridden,
            )

        styles_col, styles_reset_col = st.columns([5, 1])
        with styles_col:
            styles = st.text_input("Styles", value=", ".join(row.styles), key=f"{row_key}_styles_{styles_gen}")
        with styles_reset_col:
            st.write("")
            reset_styles = st.form_submit_button(
                "Reset",
                key=f"{row_key}_reset_styles",
                icon=":material/restart_alt:",
                help="Discard the styles correction and use the Discogs-sourced styles again",
                disabled=not row.styles_overridden,
            )

        genres_col, genres_reset_col = st.columns([5, 1])
        with genres_col:
            genres = st.text_input("Genres", value=", ".join(row.genres), key=f"{row_key}_genres_{genres_gen}")
        with genres_reset_col:
            st.write("")
            reset_genres = st.form_submit_button(
                "Reset",
                key=f"{row_key}_reset_genres",
                icon=":material/restart_alt:",
                help="Discard the genres correction and use the Discogs-sourced genres again",
                disabled=not row.genres_overridden,
            )

        video_col, video_reset_col = st.columns([5, 1])
        with video_col:
            youtube_url = st.text_input(
                "YouTube link",
                value=row.youtube_url,
                key=f"{row_key}_youtube_url_{video_gen}",
                help="Paste a YouTube/YT Music URL, or clear it to reject the current match",
            )
        with video_reset_col:
            st.write("")
            reset_video = st.form_submit_button(
                "Reset",
                key=f"{row_key}_reset_video",
                icon=":material/restart_alt:",
                help="Forget the manually-picked/rejected match and search for a new one now",
                disabled=not row.video_overridden,
            )

        saved = st.form_submit_button("Save changes", key=f"{row_key}_save")

    if reset_artist or reset_styles or reset_genres:
        with store.connect() as conn:
            if reset_artist:
                if row.track_id is not None:
                    store.set_track_search_artist(conn, row.track_id, None)
                else:
                    store.set_release_artist_override(conn, row.release_id, None)
                st.session_state[f"{row_key}_artist_gen"] = artist_gen + 1
            if reset_styles:
                if row.track_id is not None:
                    store.set_track_styles_override(conn, row.track_id, None)
                else:
                    store.set_release_styles_override(conn, row.release_id, None)
                st.session_state[f"{row_key}_styles_gen"] = styles_gen + 1
            if reset_genres:
                if row.track_id is not None:
                    store.set_track_genres_override(conn, row.track_id, None)
                else:
                    store.set_release_genres_override(conn, row.release_id, None)
                st.session_state[f"{row_key}_genres_gen"] = genres_gen + 1
        st.rerun()

    if reset_video:
        with store.connect() as conn:
            release = store.get_release(conn, row.release_id)
            assert release is not None  # the row was just built from this release
            tracks = store.get_release_tracks(conn, row.release_id)
            if row.match_id is not None:
                store.delete_match(conn, row.match_id)
            yt = ytmusic_client.get_client(authenticated=False)
            sync_engine.rematch_track(conn, yt, release, tracks, row.track_id, row.track_artist, row.track_title)
        st.session_state[f"{row_key}_video_gen"] = video_gen + 1
        st.success("Refetched the YouTube match.")
        st.rerun()

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


def _tag_group_ids(key_prefix: str) -> list[int]:
    """Stable per-group ids backing the Style filter's AND/OR group builder (#20).

    Groups are addressed by an ever-incrementing id, not list position, so removing
    one group can't shift another group's widget state (its picked tags/mode) onto
    the wrong slot. `key_prefix` scopes the backing session_state to one page (the
    Collection tab, or a specific Other-source page), so switching pages doesn't leak
    one page's group builder state into another's (#71-style key collision).
    """
    ids_key = f"{key_prefix}_tag_group_ids"
    next_id_key = f"{key_prefix}_tag_group_next_id"
    if ids_key not in st.session_state:
        st.session_state[ids_key] = [0]
        st.session_state[next_id_key] = 1
    ids: list[int] = st.session_state[ids_key]
    return ids


def _render_tag_group_filters(tag_options: list[str], key_prefix: str) -> tuple[list[TagGroup], BoolOp]:
    """Render the Style filter's AND/OR group builder and return the resulting groups
    and how they combine (see `PlaylistFilter.tag_groups`/`tag_groups_mode`).

    Each group gets its own tag multiselect + and/or "match" mode; "+ Add style group"
    appends another; a group beyond the first can be removed. Multiple groups only show
    a combinator (AND/OR between groups) once there's more than one to combine.
    """
    group_ids = _tag_group_ids(key_prefix)
    tag_groups: list[TagGroup] = []
    for gid in group_ids:
        tag_col, mode_col, remove_col = st.columns([3, 1, 1])
        with tag_col:
            selected = st.multiselect(
                "Style",
                tag_options,
                key=f"{key_prefix}_tag_group_{gid}",
                label_visibility="collapsed",
                placeholder="All styles",
            )
        with mode_col:
            mode = cast(
                BoolOp,
                st.selectbox(
                    "Match",
                    options=["or", "and"],
                    format_func=lambda m: "any of" if m == "or" else "all of",
                    key=f"{key_prefix}_tag_group_mode_{gid}",
                    label_visibility="collapsed",
                    help="How the selected styles in this group combine",
                ),
            )
        with remove_col:
            if len(group_ids) > 1 and st.button("Remove", key=f"{key_prefix}_tag_group_remove_{gid}"):
                group_ids.remove(gid)
                st.rerun()
        if selected:
            tag_groups.append(TagGroup(tags=selected, mode=mode))

    add_col, combinator_col = st.columns([1.6, 2.4])
    with add_col:
        if st.button("+ Add style group", key=f"{key_prefix}_tag_group_add"):
            next_id_key = f"{key_prefix}_tag_group_next_id"
            new_id = st.session_state[next_id_key]
            st.session_state[next_id_key] = new_id + 1
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
                    key=f"{key_prefix}_tag_groups_mode",
                    horizontal=True,
                ),
            )
    return tag_groups, tag_groups_mode


def _render_source_browser(
    source_type: str,
    source_key: str,
    header: str,
    empty_message: str,
    key_prefix: str,
    subtitle_link: str | None = None,
) -> None:
    """Render one Discogs source's browsable/filterable/editable pane: the Collection tab
    ("my own collection") or an Other-source page (a label/wantlist/seller/other user's
    collection) — same experience either way, just scoped to that source's own releases.

    Loads every track for this source via `filters.resolve_rows` (no filter), derives
    filter option lists (tags/labels/channels/year range) from those rows, then
    re-resolves with a `PlaylistFilter` built from whatever the user picked. The
    Scan/Sync buttons at the top drive `scan_engine`/`sync_engine` directly (the same
    cache-populating logic `cli.py`'s `scan`/`sync` commands use, generalized to any
    source); edits made in the table itself are persisted via `collection_edits`.
    Rematch is collection-only (not offered on Other-source pages).

    `key_prefix` scopes every widget/session_state key used here to this one page, so
    switching between the Collection tab and an Other-source page (or between two
    Other-source pages) can't leak one page's filter/selection state into another's.

    `subtitle_link`, when given (Other-source pages only — the Collection tab has no
    corresponding Discogs page of its own), renders a link back to the exact Discogs page
    this source was imported from, right under the header.
    """
    all_rows = _source_rows(source_type, source_key)

    st.markdown(_ACTION_PILL_CSS, unsafe_allow_html=True)
    st.markdown(_HEADER_CSS, unsafe_allow_html=True)
    with st.container(key="source_header"):
        st.header(header)
        if subtitle_link is not None:
            st.markdown(f"[View on Discogs ↗]({subtitle_link})")
        if all_rows:
            release_count = len({r.release_id for r in all_rows})
            matched_count = sum(1 for r in all_rows if r.matched)
            st.caption(f"{release_count} releases · {len(all_rows)} tracks · {matched_count} matched")
        with st.container(horizontal=True, gap="xxsmall"):
            _render_scan_button(source_type, source_key)
            _render_sync_matches_button(source_type, source_key)
            if source_type == "collection":
                _render_rematch_button()
        if source_type == "collection" and st.session_state.get("confirm_rematch"):
            _render_rematch_confirmation()

        if not all_rows:
            st.info(empty_message)
            return

        search = st.text_input(
            "Search by artist, track title, or release title",
            key=f"{key_prefix}_search",
            placeholder="Search by artist, track title, or release title",
            label_visibility="collapsed",
            icon=":material/search:",
        )

        tag_options = _tag_options(all_rows)
        label_options = _label_options(all_rows)
        channel_options = _channel_options(all_rows)
        year_lo, year_hi = _year_bounds(all_rows)

        tag_groups, tag_groups_mode = _render_tag_group_filters(tag_options, key_prefix)

        col1, col2, col3, col4 = st.columns([2, 2, 1.5, 1.5])
        with col1:
            labels = st.multiselect(
                "Label",
                label_options,
                key=f"{key_prefix}_labels",
                label_visibility="collapsed",
                placeholder="Label",
            )
        with col2:
            channels = st.multiselect(
                "Channel",
                channel_options,
                key=f"{key_prefix}_channels",
                label_visibility="collapsed",
                placeholder="Channel",
            )
        with col3:
            if year_lo < year_hi:
                year_range = st.slider(
                    "Year",
                    min_value=year_lo,
                    max_value=year_hi,
                    value=(year_lo, year_hi),
                    key=f"{key_prefix}_year",
                    label_visibility="collapsed",
                )
            else:
                st.write(f"Year: {year_lo}")  # a single distinct year — st.slider rejects min == max
                year_range = (year_lo, year_hi)
        with col4:
            matched_only = st.checkbox(
                "Matched", key=f"{key_prefix}_matched_only", help="Only show already-matched tracks"
            )

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
        rows = resolve_rows(conn, filt, source_type=source_type, source_key=source_key)
    rows = filter_rows_by_query(rows, search)

    st.caption(f"{len(rows)} tracks ({sum(1 for r in rows if r.matched)} matched)")
    select_all = st.checkbox(
        f"Select all {len(rows)} filtered track(s)", key=f"{key_prefix}_select_all", disabled=not rows
    )

    df = _rows_to_dataframe(rows)
    table_key = _table_key(f"{key_prefix}_table", rows)
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

    _render_edit_panel(selected_rows, key_prefix=key_prefix)

    # "Select all" overrides whatever's highlighted in the table rather than merely
    # pre-selecting it, so the table's own selection can't be used to carve out
    # exceptions from it — turn it off first to hand-pick a subset instead.
    if select_all:
        selected_ids = [r.track_id for r in rows if r.track_id is not None]
    else:
        selected_ids = [r.track_id for r in selected_rows if r.track_id is not None]
    _render_add_to_playlist(selected_ids, table_key, source_type, source_key, key_prefix)


def render_collection_tab() -> None:
    """Render the Collection pane: "my own Discogs collection", browsable/filterable/editable.

    A thin wrapper around `_render_source_browser` scoped to the implicit "collection"
    source — see its docstring for what's actually rendered.
    """
    _render_source_browser(
        source_type="collection",
        source_key="",
        header="My Discogs Collection",
        empty_message="No collection cached yet. Click Scan above, or run `discogs2ytmusic scan`.",
        key_prefix="collection",
    )


_OTHER_SOURCE_LABELS = {
    "user_collection": "user's collection",
    "wantlist": "wantlist",
    "label": "label catalogue",
    "seller": "seller inventory",
}
_OTHER_SOURCE_ICONS = {
    "user_collection": ":material/person:",
    "wantlist": ":material/favorite:",
    "label": ":material/sell:",
    "seller": ":material/storefront:",
}


def _render_other_source_page(source: sqlite3.Row) -> None:
    """Render one "Other sources" page: same experience as the Collection tab
    (`_render_source_browser`), scoped to this source's own releases, plus a "Remove this
    source" affordance that forgets the page (and its release tags) without touching any
    cached release/track/match data that might still be used elsewhere.
    """
    source_type, source_key, source_id = source["source_type"], source["source_key"], source["id"]
    kind_label = _OTHER_SOURCE_LABELS[source_type]
    _render_source_browser(
        source_type=source_type,
        source_key=source_key,
        header=source["display_name"],
        empty_message=f"No releases scanned yet from this {kind_label}. Click Scan above.",
        key_prefix=f"othersrc_{source_id}",
        subtitle_link=source_url(source_type, source_key),
    )

    st.divider()
    confirm_key = f"confirm_remove_othersrc_{source_id}"
    if not st.session_state.get(confirm_key):
        if st.button("Remove this source", key=f"remove_othersrc_{source_id}", icon=":material/delete:"):
            st.session_state[confirm_key] = True
            st.rerun()
        return

    st.warning(
        "This removes the page and forgets which releases came from it. Cached release/"
        "track/match data is kept — it may still be used by another source or a playlist."
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, remove", key=f"confirm_remove_othersrc_yes_{source_id}"):
            with store.connect() as conn:
                store.delete_other_source(conn, source_id)
                conn.commit()
            st.session_state["nav_kind"] = "collection"
            st.session_state.pop(confirm_key, None)
            st.rerun()
    with col2:
        if st.button("Cancel", key=f"confirm_remove_othersrc_no_{source_id}"):
            st.session_state[confirm_key] = False
            st.rerun()


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


def _render_add_to_playlist(
    selected_ids: list[int],
    table_key: str,
    source_type: str = "collection",
    source_key: str = "",
    key_prefix: str = "collection",
) -> None:
    """Selected rows in a source's main table (or every filtered track, if "select all" is
    on) -> add to an existing or new playlist, optionally filing a newly created playlist
    into a folder.

    Only playlists already built from this same `source_type`/`source_key` are offered as
    a target — and a newly-created one is tagged with it — so a playlist never ends up
    mixing tracks from more than one Discogs source (see `store.add_tracks_to_playlist`).
    """

    with store.connect() as conn:
        playlist_names = [p["name"] for p in store.list_playlists(conn, source_type=source_type, source_key=source_key)]
        folder_names, folder_by_name = _folder_picker_options(conn)

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        choice = st.selectbox(
            f"Add {len(selected_ids)} selected track(s) to",
            [_NEW_PLAYLIST_SENTINEL, *playlist_names],
            key=f"{key_prefix}_add_target",
        )
    new_name = ""
    with col2:
        if choice == _NEW_PLAYLIST_SENTINEL:
            new_name = st.text_input("New playlist name", key=f"{key_prefix}_new_playlist_name")
    folder_choice = _NO_FOLDER_SENTINEL
    new_folder_name = ""
    with col3:
        if choice == _NEW_PLAYLIST_SENTINEL:
            folder_choice = st.selectbox(
                "Folder (optional)",
                [_NO_FOLDER_SENTINEL, _NEW_FOLDER_SENTINEL, *folder_names],
                key=f"{key_prefix}_new_playlist_folder",
            )
            if folder_choice == _NEW_FOLDER_SENTINEL:
                new_folder_name = st.text_input("New folder name", key=f"{key_prefix}_new_playlist_folder_name")
    with col4:
        st.write("")
        add_clicked = st.button("Add to playlist", key=f"{key_prefix}_add_button", disabled=not selected_ids)

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
                playlist_id = store.create_playlist(conn, name, source_type=source_type, source_key=source_key)
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


def _nav_button(label: str, *, key: str, selected: bool, width: str = "stretch", **button_kwargs: object) -> bool:
    """Render a plain-text sidebar nav button, accent-colored via a scoped wrapper when selected."""
    if selected:
        with st.container(key="nav_selected"):
            return st.button(label, key=key, type="tertiary", width=width, **button_kwargs)  # type: ignore[arg-type]
    return st.button(label, key=key, type="tertiary", width=width, **button_kwargs)  # type: ignore[arg-type]


def _render_playlist_nav_button(playlist: sqlite3.Row, kind: str, selected_playlist_id: int | None) -> None:
    """Render one playlist's sidebar row, wherever it appears (ungrouped, or inside an
    expanded folder)."""
    is_selected = kind == "playlist" and playlist["id"] == selected_playlist_id
    if _nav_button(
        playlist["name"], key=f"nav_playlist_{playlist['id']}", selected=is_selected, icon=":material/music_note:"
    ):
        st.session_state["nav_kind"] = "playlist"
        st.session_state["nav_playlist_id"] = playlist["id"]
        st.rerun()


def _render_folder_nav_entry(
    folder: sqlite3.Row, playlists: list[sqlite3.Row], kind: str, selected_id: int | None
) -> None:
    """Render one playlist folder's sidebar row: a chevron that expands/collapses (like the
    Playlists section itself) to list the playlists filed under it right there in the sidebar,
    plus the folder name itself as a separate click target that opens the folder's detail page
    in the main pane (`_render_folder_detail`) — where its stats and the delete affordance live.
    """
    folder_id = folder["id"]
    expanded_key = f"nav_folder_expanded_{folder_id}"
    expanded = st.session_state.get(expanded_key, False)
    is_selected = kind == "folder" and folder_id == selected_id

    # An icon-only button (the chevron) sizes its box to just the icon, while the name
    # button's box also accounts for its label text — center-aligning the columns keeps both
    # icons on the same visual line despite that box-height difference.
    chevron_col, name_col = st.columns([1, 7], vertical_alignment="center")
    with chevron_col:
        if st.button(
            "",
            key=f"nav_folder_toggle_{folder_id}",
            type="tertiary",
            icon=":material/expand_more:" if expanded else ":material/chevron_right:",
            help="Show playlists in this folder",
        ):
            st.session_state[expanded_key] = not expanded
            st.rerun()
    with name_col:
        if _nav_button(folder["name"], key=f"nav_folder_{folder_id}", selected=is_selected, icon=":material/folder:"):
            st.session_state["nav_kind"] = "folder"
            st.session_state["nav_folder_id"] = folder_id
            st.rerun()

    if expanded:
        with st.container(key=f"nav_folder_playlists_{folder_id}"):
            if not playlists:
                st.caption("No playlists in this folder yet.")
            for p in sorted(playlists, key=lambda p: p["name"].lower()):
                _render_playlist_nav_button(p, kind, selected_id)


def _resolve_new_source_display_name(source_type: str, source_key: str) -> str:
    """Best-effort human-readable name for a newly-added Other-sources page."""
    if source_type == "label":
        creds = _load_discogs_client()
        if creds is not None:
            client, _ = creds
            try:
                info = client.get_label(int(source_key))
            except (DiscogsError, ValueError):
                info = {}
            name = info.get("name")
            if name:
                return str(name)
        return f"Label {source_key}"
    if source_type == "wantlist":
        return f"{source_key}'s wantlist"
    if source_type == "seller":
        return f"{source_key}'s inventory"
    return f"{source_key}'s collection"  # user_collection


_IMPORT_YEAR_MIN = 1960


def _import_year_max() -> int:
    """Upper bound for the Add-source page's Year pre-filter slider — always the current
    year, so a newly-released record is never out of range."""
    return datetime.date.today().year


def _render_add_source_page() -> None:
    """Dedicated page for registering a new "Other sources" entry (linked from the
    sidebar's "+ Add source" row instead of an inline form): paste a Discogs collection/
    wantlist/label/seller URL, optionally narrow what gets imported with a Style/Format/Year
    pre-filter, then create the page and jump to it.

    A URL that resolves to a source already registered shows a warning, but still renders
    the pre-filter form — prefilled with that source's current filter — so its pre-filter
    can be revised later; without this, once a source existed, there was no way to ever
    change what it was set up to import (a "Go to existing page" button is offered too,
    for jumping over there unchanged).

    The pre-filter is persisted on the source (`scan_engine.ImportFilter`, via
    `store.add_other_source`'s `filter_json`) and re-applied on every future Scan, not
    just this first import — see `_run_scan`. Style options come from Discogs' own
    genre/style taxonomy (`discogs_taxonomy`), available up front with no API call, since
    a label's own release styles aren't knowable until each one's full detail is fetched
    (which only happens once you actually decide to import). Format has no such
    ready-made picklist anywhere (no API endpoint, no dataset, unlike Style) — instead
    `store.list_known_formats` grows organically from every release any scan has ever
    actually looked at (`scan_engine.scan_release`/`scan_label_release` both record their
    format tokens regardless of any filter outcome), so it's empty until something's been
    scanned at least once.
    """
    st.header("Add a source")
    st.markdown("[Browse Discogs ↗](https://www.discogs.com)")
    st.caption("Paste a Discogs collection, wantlist, label, or seller URL.")
    url = st.text_input(
        "Discogs URL",
        key="add_source_url",
        placeholder="https://www.discogs.com/label/123-Some-Label",
        label_visibility="collapsed",
    )
    stripped = url.strip()

    if not stripped:
        return
    if is_my_wantlist_url(stripped):
        # Discogs' "my wantlist" page (`/mywantlist`) has no username in it at all — resolve
        # it to the signed-in user's own username so it becomes an ordinary wantlist source,
        # the same one `/user/<name>/wantlist` for that same name would produce.
        creds = _load_discogs_client()
        if creds is None:
            st.error("Not authenticated with Discogs. Run `discogs2ytmusic auth discogs` first.")
            return
        _, username = creds
        parsed: tuple[str, str] | None = ("wantlist", username)
    else:
        parsed = parse_source_url(stripped)
    if parsed is None:
        st.error("Paste a Discogs collection, wantlist, label, or seller URL.")
        return
    source_type, source_key = parsed

    with store.connect() as conn:
        existing = store.get_other_source_by_key(conn, source_type, source_key)
    existing_filter = None
    if existing is not None:
        st.warning(
            f"You've already added this source, as **{existing['display_name']}**. You "
            "can update its pre-filter below, or jump to the existing page unchanged."
        )
        if st.button("Go to existing page", key="add_source_go_to_existing"):
            st.session_state["nav_kind"] = "other_source"
            st.session_state["nav_other_source_id"] = existing["id"]
            st.rerun()
        existing_filter = scan_engine.ImportFilter.from_dict(json.loads(existing["filter_json"]))

    year_max = _import_year_max()

    st.divider()
    st.subheader("Pre-filter what gets imported")
    st.caption(
        "Optional — only releases matching all of these get imported, and this is "
        "re-applied every time you Scan this source again, not just the first time."
    )
    styles = st.multiselect(
        "Style",
        discogs_taxonomy.STYLE_FILTER_OPTIONS,
        default=existing_filter.styles if existing_filter else [],
        key="add_source_styles",
    )
    with store.connect() as conn:
        known_formats = store.list_known_formats(conn)
    formats = st.multiselect(
        "Format",
        known_formats,
        default=[f for f in existing_filter.formats if f in known_formats] if existing_filter else [],
        key="add_source_formats",
    )
    if not known_formats:
        st.caption("No formats seen yet — options appear here once you've scanned at least one source.")
    year_range_min, year_range_max = st.slider(
        "Year",
        min_value=_IMPORT_YEAR_MIN,
        max_value=year_max,
        value=(
            existing_filter.year_min if existing_filter and existing_filter.year_min is not None else _IMPORT_YEAR_MIN,
            existing_filter.year_max if existing_filter and existing_filter.year_max is not None else year_max,
        ),
        key="add_source_year_range",
    )

    st.divider()
    submit_label = "Update source" if existing is not None else "Add source"
    if not st.button(submit_label, key="add_source_submit", type="primary"):
        return

    filter_dict = {
        "styles": styles,
        "formats": formats,
        "year_min": int(year_range_min) if year_range_min > _IMPORT_YEAR_MIN else None,
        "year_max": int(year_range_max) if year_range_max < year_max else None,
    }
    with store.connect() as conn:
        if existing is not None:
            store.update_other_source_filter(conn, existing["id"], filter_dict)
            source_id = existing["id"]
        else:
            display_name = _resolve_new_source_display_name(source_type, source_key)
            source_id = store.add_other_source(conn, source_type, source_key, display_name, filter_dict)
        conn.commit()
    st.session_state["nav_kind"] = "other_source"
    st.session_state["nav_other_source_id"] = source_id
    st.rerun()


def _render_other_sources_nav_entries(other_sources: list[sqlite3.Row], kind: str, selected_id: int | None) -> None:
    """The "Other sources" sidebar sub-items: one nav row per registered source, plus a
    "+ Add source" link opening the dedicated add-source page (`_render_add_source_page`).
    """
    if not other_sources:
        st.caption("No other sources yet.")
    for source in other_sources:
        is_selected = kind == "other_source" and source["id"] == selected_id
        icon = _OTHER_SOURCE_ICONS[source["source_type"]]
        if _nav_button(source["display_name"], key=f"nav_othersrc_{source['id']}", selected=is_selected, icon=icon):
            st.session_state["nav_kind"] = "other_source"
            st.session_state["nav_other_source_id"] = source["id"]
            st.rerun()
    if _nav_button("+ Add source", key="nav_add_source_page", selected=kind == "add_source", icon=":material/add:"):
        st.session_state["nav_kind"] = "add_source"
        st.rerun()


def render_sidebar_nav() -> tuple[str, int | None]:
    """Render the sidebar, and report which pane `main` should show next.

    A Collection link, then a Playlists section listing playlist folders and ungrouped
    playlists together (one row per entry, alphabetically) — folders expand/collapse,
    like the Playlists section itself, to reveal the playlists filed under them right
    there in the sidebar, while the folder name is its own click target opening the
    folder's detail page in the main pane. Then an "Other sources" section, styled and
    structured just like the Playlists section (same expand/collapse header, same
    indented/lighter-weight sub-item rows — see `_SIDEBAR_NAV_CSS`'s `nav_other_sources_top`/
    `nav_other_sources` rules), listing every registered label/wantlist/seller/other-user-collection
    page plus a "+ Add source" link opening the dedicated add-source page
    (`_render_add_source_page`) rather than an inline form. Reads `playlists`/
    `playlist_folders`/`other_sources` from `store` directly (this is the one place in the
    app that queries them outside `filters.py`, since sidebar rows aren't `TrackRow`s).
    Selection state lives in `st.session_state`, set by the nav buttons here and cleared
    back to "collection" if it points at a since-deleted playlist/folder/source. Returns
    the current selection as ("collection", None), ("playlist", id), ("folder", id),
    ("other_source", id), ("add_source", None), or ("ytmusic", None).
    """
    with store.connect() as conn:
        playlists = store.list_playlists(conn)
        folders = store.list_playlist_folders(conn)
        other_sources = store.list_other_sources(conn)
    playlist_ids = {p["id"] for p in playlists}
    folder_ids = {f["id"] for f in folders}
    other_source_ids = {s["id"] for s in other_sources}

    kind = st.session_state.get("nav_kind", "collection")
    if kind == "playlist":
        selected_id = st.session_state.get("nav_playlist_id")
    elif kind == "folder":
        selected_id = st.session_state.get("nav_folder_id")
    elif kind == "other_source":
        selected_id = st.session_state.get("nav_other_source_id")
    else:
        selected_id = None
    stale = (
        (kind == "playlist" and selected_id not in playlist_ids)
        or (kind == "folder" and selected_id not in folder_ids)
        or (kind == "other_source" and selected_id not in other_source_ids)
    )
    if stale or kind not in ("collection", "playlist", "folder", "other_source", "add_source", "ytmusic"):
        kind, selected_id = "collection", None

    expanded = st.session_state.get("nav_playlists_expanded", True)
    other_sources_expanded = st.session_state.get("nav_other_sources_expanded", True)

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
                st.session_state["nav_folder_id"] = None
                st.session_state["nav_other_source_id"] = None
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
                        _render_folder_nav_entry(folder, playlists_by_folder.get(entry_id, []), kind, selected_id)
                    else:
                        playlist = next(p for p in ungrouped if p["id"] == entry_id)
                        _render_playlist_nav_button(playlist, kind, selected_id)

        st.divider()
        with st.container(key="nav_other_sources_top"):
            if st.button(
                "Other sources",
                key="nav_other_sources_toggle",
                type="tertiary",
                width="stretch",
                icon=":material/expand_more:" if other_sources_expanded else ":material/chevron_right:",
            ):
                st.session_state["nav_other_sources_expanded"] = not other_sources_expanded
                st.rerun()

        if other_sources_expanded:
            with st.container(key="nav_other_sources"):
                _render_other_sources_nav_entries(other_sources, kind, selected_id)

        st.divider()
        _render_ytmusic_nav_item(kind == "ytmusic")

    return kind, selected_id


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
            help="Pushed playlists are named '<prefix> <playlist name>'.",
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
    with store.connect() as conn:
        origin = _source_display_name(conn, playlist["source_type"], playlist["source_key"])
    st.caption(f"{pushed_caption} · Last modified {_relative_time(playlist['updated_at'])} · From: {origin}")

    _render_playlist_folder_picker(playlist)

    if rows:
        df = _rows_to_dataframe(rows)
        table_key = _table_key(f"playlist_tracks_{playlist_id}", rows)
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

        _render_edit_panel(selected_rows, key_prefix=f"playlist_{playlist_id}")

        to_remove = [r.track_id for r in selected_rows if r.track_id is not None]
        if st.button(f"Remove {len(to_remove)} selected", key=f"remove_button_{playlist_id}", disabled=not to_remove):
            with store.connect() as conn:
                store.remove_tracks_from_playlist(conn, playlist_id, to_remove)
                conn.commit()
            del st.session_state[table_key]
            st.rerun()
    else:
        st.caption("No tracks yet — search below to add some.")

    st.markdown("**Add tracks**")
    search = st.text_input(f"Search {origin} by artist or title", key=f"playlist_search_{playlist_id}")
    if search.strip():
        needle = search.strip().lower()
        already_in = {r.track_id for r in rows}
        matches = [
            r
            for r in _source_rows(playlist["source_type"], playlist["source_key"])
            if r.track_id is not None
            and r.track_id not in already_in
            and (needle in r.track_artist.lower() or needle in r.track_title.lower())
        ][:50]
        if not matches:
            st.caption("No matching tracks found.")
        else:
            search_df = _rows_to_dataframe(matches)
            search_table_key = _table_key(f"playlist_search_table_{playlist_id}", matches)
            search_event = st.dataframe(
                search_df,
                key=search_table_key,
                hide_index=True,
                width="stretch",
                column_order=["track_artist", "track_title", "release_title", "matched"],
                column_config=SHARED_COLUMN_CONFIG,
                on_select="rerun",
                selection_mode="multi-row",
            )
            selected_matches = [matches[i] for i in search_event.selection.rows]
            to_add = [r.track_id for r in selected_matches if r.track_id is not None]
            if st.button(f"Add {len(to_add)} selected", key=f"add_from_search_{playlist_id}", disabled=not to_add):
                with store.connect() as conn:
                    added = store.add_tracks_to_playlist(conn, playlist_id, to_add)
                    conn.commit()
                st.success(f"Added {added} track(s).")
                del st.session_state[search_table_key]
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


def _render_sync_confirmation(playlist: sqlite3.Row, rows: list[TrackRow]) -> None:
    playlist_id = playlist["id"]
    video_ids = [r.video_id for r in rows if r.video_id]
    confirm_key = f"confirm_sync_{playlist_id}"
    extra_confirm_key = f"confirm_sync_extra_{playlist_id}"
    playlist_name = f"{Config.load().playlist_name_prefix} {playlist['name']}"
    already_linked = bool(playlist["ytmusic_playlist_id"])

    st.warning(
        "This will create (or update) a real playlist on your YT Music account "
        f"named '{playlist_name}' with these {len(video_ids)} track(s)."
    )

    for group in sync_engine.duplicate_video_groups(rows):
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
                stray_count = len(
                    sync_engine.stray_remote_tracks(ytmusic_client.get_playlist_tracks(yt, found_id), video_ids)
                )
        except sync_engine.YTMUSIC_PUSH_ERRORS:
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
                to_remove = sync_engine.push_to_ytmusic(yt, playlist, playlist_id, playlist_name, video_ids)
            except sync_engine.YTMUSIC_PUSH_ERRORS as e:
                if isinstance(e, sync_engine.YTMUSIC_AUTH_SUSPECT_ERRORS):
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


def _render_folder_delete_button(folder: sqlite3.Row) -> None:
    folder_id = folder["id"]
    confirm_key = f"confirm_delete_folder_{folder_id}"

    if st.session_state.get(confirm_key):
        return
    with st.container(key="delete_pill", width="content"):
        if st.button(
            "Delete",
            key=f"delete_folder_button_{folder_id}",
            icon=":material/delete:",
            help="Delete this folder",
        ):
            st.session_state[confirm_key] = True
            st.rerun()


def _render_folder_delete_confirmation(folder: sqlite3.Row, playlist_count: int) -> None:
    """Confirm-then-delete for a folder, mirroring `_render_delete_confirmation`'s shape for a
    playlist. Deleting a folder unassigns (rather than deletes) any playlists inside it, per
    `store.delete_playlist_folder`'s semantics — the warning spells that out whenever the
    folder isn't empty."""
    folder_id = folder["id"]
    confirm_key = f"confirm_delete_folder_{folder_id}"

    if playlist_count:
        st.warning(
            f"Delete the folder '{folder['name']}'? Its {playlist_count} playlist(s) won't be "
            "deleted — they'll move back to ungrouped."
        )
    else:
        st.warning(f"Delete the empty folder '{folder['name']}'?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, delete", key=f"confirm_delete_folder_yes_{folder_id}"):
            with store.connect() as conn:
                store.delete_playlist_folder(conn, folder_id)
                conn.commit()
            st.session_state[confirm_key] = False
            st.session_state["nav_kind"] = "collection"
            st.session_state["nav_folder_id"] = None
            st.rerun()
    with col2:
        if st.button("Cancel", key=f"confirm_delete_folder_no_{folder_id}"):
            st.session_state[confirm_key] = False
            st.rerun()


def _render_folder_detail(folder: sqlite3.Row) -> None:
    """A folder's detail page: its contained playlists with a track count each (clicking one
    opens its own detail view — the only way to reach a grouped playlist now that folders no
    longer expand inline in the sidebar), plus the delete affordance for the folder itself."""
    folder_id = folder["id"]
    with store.connect() as conn:
        playlists = [p for p in store.list_playlists(conn) if p["folder_id"] == folder_id]
        track_counts = {p["id"]: len(store.list_playlist_track_ids(conn, p["id"])) for p in playlists}

    st.markdown(_ACTION_PILL_CSS, unsafe_allow_html=True)
    title_col, actions_col = st.columns([3, 2], vertical_alignment="center")
    with title_col:
        st.subheader(folder["name"])
    with actions_col, st.container(horizontal=True, horizontal_alignment="right", gap="xxsmall"):
        _render_folder_delete_button(folder)

    if st.session_state.get(f"confirm_delete_folder_{folder_id}"):
        _render_folder_delete_confirmation(folder, len(playlists))

    if not playlists:
        st.caption("No playlists in this folder yet.")
    st.markdown(_FOLDER_PLAYLIST_LIST_CSS, unsafe_allow_html=True)
    with st.container(key="folder_playlist_list"):
        for p in sorted(playlists, key=lambda p: p["name"].lower()):
            count = track_counts[p["id"]]
            label = f"{p['name']} - {count} track{'' if count == 1 else 's'}"
            if st.button(label, key=f"folder_playlist_{p['id']}", type="tertiary", width="stretch"):
                st.session_state["nav_kind"] = "playlist"
                st.session_state["nav_playlist_id"] = p["id"]
                st.session_state["nav_folder_id"] = None
                st.rerun()


# --- CSS ---
#
# Grouped together at the bottom of the file, out of the way of render logic, since these are
# markup/styling rather than business logic — each is `st.markdown(..., unsafe_allow_html=True)`'d
# from the render function it styles.

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
    border-color: #D9D0B0 !important;
    color: #AF3029 !important;
}
.st-key-delete_pill button:hover {
    border-color: #E3B6AE !important;
    color: #AF3029 !important;
    background-color: #FBEEEC !important;
}
</style>
"""

# A folder detail page's playlist list (`_render_folder_detail`): each row is a single
# tertiary button whose label is already "name - N tracks", so it just needs left-aligning —
# Streamlit centers button content by default.
_FOLDER_PLAYLIST_LIST_CSS = """
<style>
.st-key-folder_playlist_list button {
    justify-content: flex-start !important;
    width: 100% !important;
}
.st-key-folder_playlist_list button > div {
    justify-content: flex-start !important;
}
.st-key-folder_playlist_list button p {
    text-align: left !important;
}
</style>
"""

_SIDEBAR_NAV_CSS = """
<style>
.st-key-nav_top,
.st-key-nav_playlists,
.st-key-nav_other_sources,
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
.st-key-nav_other_sources_top button,
.st-key-nav_playlists button,
.st-key-nav_other_sources button {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.2rem 0 !important;
    min-height: 0 !important;
    width: 100% !important;
    justify-content: flex-start !important;
}
.st-key-nav_top button > div,
.st-key-nav_other_sources_top button > div,
.st-key-nav_playlists button > div,
.st-key-nav_other_sources button > div {
    justify-content: flex-start !important;
}
.st-key-nav_top button p,
.st-key-nav_other_sources_top button p,
.st-key-nav_playlists button p,
.st-key-nav_other_sources button p {
    color: #20241F;
    text-align: left !important;
}
.st-key-nav_top button p,
.st-key-nav_other_sources_top button p {
    font-weight: 600;
    font-size: 0.95rem;
}
.st-key-nav_playlists,
.st-key-nav_other_sources {
    padding-left: 0.9rem;
}
.st-key-nav_playlists .stButton,
.st-key-nav_other_sources .stButton {
    line-height: 1.3;
}
.st-key-nav_playlists button,
.st-key-nav_other_sources button {
    padding: 0.1rem 0 !important;
}
.st-key-nav_playlists button p,
.st-key-nav_other_sources button p {
    font-weight: 400;
    font-size: 0.85rem;
    line-height: 1.3;
}
.st-key-nav_top button:hover p,
.st-key-nav_other_sources_top button:hover p,
.st-key-nav_playlists button:hover p,
.st-key-nav_other_sources button:hover p {
    color: #2F5D57;
}
[class*="st-key-nav_folder_playlists_"] {
    padding-left: 0.9rem;
}
.st-key-nav_selected button p {
    color: #2F5D57 !important;
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
    color: #20241F;
    text-align: left !important;
    font-weight: 600;
    font-size: 0.95rem;
}
.st-key-nav_ytmusic button:hover p {
    color: #2F5D57;
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
    background: #FBF9EF;
    border: 1px solid #D9D0B0;
    color: #8A8570;
}
.yt-status-pill.is-off::before {
    background: transparent;
    border: 1.4px solid #8A8570;
    width: 4px;
    height: 4px;
}
.st-key-nav_top button::before,
.st-key-nav_other_sources_top button::before,
.st-key-nav_ytmusic button::before {
    content: "";
    display: inline-block;
    width: 15px;
    height: 15px;
    margin-right: 6px;
    vertical-align: -3px;
    background-color: #6E7266;
    -webkit-mask-repeat: no-repeat;
    mask-repeat: no-repeat;
    -webkit-mask-position: center;
    mask-position: center;
    -webkit-mask-size: contain;
    mask-size: contain;
}
.st-key-nav_collection button::before {
    -webkit-mask-image: url("app/static/icons/collection.svg");
    mask-image: url("app/static/icons/collection.svg");
}
.st-key-nav_playlists_toggle button::before {
    -webkit-mask-image: url("app/static/icons/playlists.svg");
    mask-image: url("app/static/icons/playlists.svg");
}
.st-key-nav_other_sources_toggle button::before {
    -webkit-mask-image: url("app/static/icons/sources.svg");
    mask-image: url("app/static/icons/sources.svg");
}
.st-key-nav_ytmusic button::before {
    -webkit-mask-image: url("app/static/icons/ytmusic.svg");
    mask-image: url("app/static/icons/ytmusic.svg");
}
.st-key-nav_top button:hover::before,
.st-key-nav_other_sources_top button:hover::before,
.st-key-nav_ytmusic button:hover::before {
    background-color: #2F5D57;
}
.st-key-nav_selected button::before {
    background-color: #2F5D57 !important;
}
</style>
"""

# Page-title styling (`st.header` calls only — see the docstring above `render_collection_tab`
# for why this is scoped to `h2` rather than every heading level: `st.subheader` is used for
# in-page section labels, not page titles, and shouldn't pick up this treatment) plus the
# boombox artwork (`static/vectorstock_23584899.png`) as a decorative watermark next to the
# title/subtitle/actions — recolored via `mask-image` (not shown at its native blue) so it
# always renders in the fixed accent blue regardless of the rest of the palette.
#
# Deliberately NOT stretched to match the filters' height below it: the source PNG is
# ~square, so `mask-size: contain` inside a box far taller than it is wide (which is what
# a `top/bottom: 0` box becomes once the filters grow) letterboxes it, leaving a large dead
# gap above and below — worse, the filters' height is unbounded (each "+ Add style group"
# click adds another row), so there's no fixed block height to size against in the first
# place. A fixed square anchored to the top, sized against the title/subtitle/actions block
# alone (whose height is stable), stays correct regardless of window width or how many
# filter rows are showing.
_HEADER_CSS = """
<style>
h2 {
    color: #1E5136 !important;
}
.st-key-source_header {
    position: relative;
    padding-right: 165px;
}
.st-key-source_header::before {
    content: "";
    position: absolute;
    top: 0;
    right: 20px;
    width: 150px;
    height: 150px;
    background-color: #84BCFC;
    -webkit-mask: url("app/static/vectorstock_23584899.png") no-repeat center / contain;
    mask: url("app/static/vectorstock_23584899.png") no-repeat center / contain;
    opacity: 0.75;
    pointer-events: none;
}
/* Tighten the header's own vertical rhythm (title/subtitle/actions/filters) so the
   filter controls read as one compact block rather than a tall stack of full-height
   widgets — labels stay at full size so nothing gets harder to read, just closer
   together. */
.st-key-source_header [data-testid="stVerticalBlock"] {
    gap: 0.55rem;
}
.st-key-source_header [data-testid="stWidgetLabel"] {
    margin-bottom: 0.1rem;
}
</style>
"""


def main() -> None:
    """Streamlit entry point: dispatch to a pane based on the sidebar's current selection.

    `render_sidebar_nav` both renders the sidebar and returns what it should drive — a
    specific playlist's detail view, a folder's detail view, an Other-source page, the
    YT Music connection page, or (the default) `render_collection_tab`. This is the
    module-level script Streamlit re-runs top to bottom on every interaction, so nothing
    here persists across reruns except what's explicitly stashed in `st.session_state`
    or read back from `store`.
    """
    kind, selected_id = render_sidebar_nav()
    if kind == "playlist" and selected_id is not None:
        with store.connect() as conn:
            playlist = store.get_playlist(conn, selected_id)
        assert playlist is not None  # render_sidebar_nav already dropped stale/deleted ids
        _render_playlist_detail(playlist)
    elif kind == "folder" and selected_id is not None:
        with store.connect() as conn:
            folder = store.get_playlist_folder(conn, selected_id)
        assert folder is not None  # render_sidebar_nav already dropped stale/deleted ids
        _render_folder_detail(folder)
    elif kind == "other_source" and selected_id is not None:
        with store.connect() as conn:
            source = store.get_other_source(conn, selected_id)
        assert source is not None  # render_sidebar_nav already dropped stale/deleted ids
        _render_other_source_page(source)
    elif kind == "add_source":
        _render_add_source_page()
    elif kind == "ytmusic":
        _render_ytmusic_page()
    else:
        render_collection_tab()


main()
