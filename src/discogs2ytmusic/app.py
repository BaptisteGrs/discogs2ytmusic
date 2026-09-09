from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd
import streamlit as st

from discogs2ytmusic import scan_engine, store, sync_engine, ytmusic_client
from discogs2ytmusic.collection_edits import apply_artist_edits, apply_video_link_edits
from discogs2ytmusic.config import Config
from discogs2ytmusic.discogs import DiscogsClient, DiscogsError
from discogs2ytmusic.filters import PlaylistFilter, TrackRow, resolve_playlist_rows, resolve_rows

st.set_page_config(page_title="Discogs -> YT Music", layout="wide")

COLLECTION_COLUMNS = [
    "select",
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
    "locked": st.column_config.CheckboxColumn(
        "Locked",
        help=(
            "A manual correction (artist or YouTube link) protects this row from being "
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


def _year_bounds(rows: list[TrackRow]) -> tuple[int, int]:
    years = [r.year for r in rows if r.year]
    if not years:
        return (1900, 2030)
    return (min(years), max(years))


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
    if flag_column:
        for record in records:
            record[flag_column] = False
    return pd.DataFrame(records)


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
    clicked = st.button("Scan", key="scan_button", help="Re-fetch your collection and tracklists from Discogs")
    if clicked and _run_scan(refresh=True):
        st.session_state.pop("collection_editor", None)
        st.rerun()


def _render_sync_matches_button() -> None:
    clicked = st.button(
        "Sync matches",
        key="sync_matches_button",
        help="Match any unmatched tracks against YouTube/YT Music (doesn't touch your YT Music account)",
    )
    if clicked and _run_sync_matches():
        st.session_state.pop("collection_editor", None)
        st.rerun()


def _render_rematch_button() -> None:
    if st.session_state.get("confirm_rematch"):
        return
    if st.button(
        "Rematch",
        key="rematch_button",
        help="Clear cached matches and re-match everything from scratch (slow; preserves manual corrections)",
    ):
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


def render_collection_tab() -> None:
    """Render the browsable/editable table of every cached track and its YouTube match."""
    st.header("My Discogs Collection")

    action_col1, action_col2, action_col3 = st.columns(3)
    with action_col1:
        _render_scan_button()
    with action_col2:
        _render_sync_matches_button()
    with action_col3:
        _render_rematch_button()
    if st.session_state.get("confirm_rematch"):
        _render_rematch_confirmation()

    all_rows = _all_rows()
    if not all_rows:
        st.info("No collection cached yet. Click Scan above, or run `discogs2ytmusic scan`.")
        return

    tag_options = _tag_options(all_rows)
    label_options = _label_options(all_rows)
    year_lo, year_hi = _year_bounds(all_rows)

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        tags = st.multiselect("Style", tag_options, key="collection_tags")
    with col2:
        labels = st.multiselect("Label", label_options, key="collection_labels")
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

    narrowed = bool(tags or labels or matched_only or year_range != (year_lo, year_hi))
    filt = (
        PlaylistFilter(
            tags=tags,
            labels=labels,
            year_min=year_range[0] if year_range[0] > year_lo else None,
            year_max=year_range[1] if year_range[1] < year_hi else None,
            matched_only=matched_only,
        )
        if narrowed
        else None
    )

    with store.connect() as conn:
        rows = resolve_rows(conn, filt)

    st.caption(f"{len(rows)} tracks ({sum(1 for r in rows if r.matched)} matched)")

    df = _rows_to_dataframe(rows, flag_column="select")
    edited_df = st.data_editor(
        df,
        key="collection_editor",
        hide_index=True,
        width="stretch",
        column_order=COLLECTION_COLUMNS,
        disabled=[
            "release_artist",
            "position",
            "track_title",
            "release_title",
            "discogs_url",
            "styles",
            "genres",
            "labels",
            "year",
            "matched",
            "confidence",
            "video_title",
            "channel",
            "locked",
        ],
        column_config={
            **SHARED_COLUMN_CONFIG,
            "select": st.column_config.CheckboxColumn("", help="Select tracks to add to a playlist"),
        },
    )

    with store.connect() as conn:
        n_artist = apply_artist_edits(conn, df, edited_df)
        n_video, errors = apply_video_link_edits(conn, df, edited_df)

    for message in errors:
        st.error(message)
    if n_artist or n_video:
        st.success(f"Saved {n_artist + n_video} correction(s).")
        del st.session_state["collection_editor"]
        st.rerun()

    _render_add_to_playlist(edited_df)


def _render_add_to_playlist(edited_df: pd.DataFrame) -> None:
    """Checked rows in the Collection tab's "select" column -> add to an existing or new playlist."""
    selected_ids = _selected_track_ids(edited_df, "select")

    with store.connect() as conn:
        playlist_names = [p["name"] for p in store.list_playlists(conn)]

    col1, col2, col3 = st.columns([2, 2, 1])
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
    with col3:
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
            try:
                playlist_id = store.create_playlist(conn, name)
            except sqlite3.IntegrityError:
                st.error(f"A playlist named '{name}' already exists.")
                return
        else:
            playlist_row = store.get_playlist_by_name(conn, choice)
            assert playlist_row is not None  # choice comes from the same list_playlists() call above
            playlist_id = playlist_row["id"]
            name = choice
        added = store.add_tracks_to_playlist(conn, playlist_id, selected_ids)
        conn.commit()

    st.success(f"Added {added} track(s) to '{name}'.")
    del st.session_state["collection_editor"]
    st.rerun()


def _playlist_dataframe(rows: list[TrackRow]) -> pd.DataFrame:
    return _rows_to_dataframe(rows, flag_column="remove")


_SIDEBAR_NAV_CSS = """
<style>
.st-key-nav_top,
.st-key-nav_playlists {
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


def render_sidebar_nav() -> tuple[str, int | None]:
    """Render the sidebar: a Collection link, then a Playlists section with one button per
    curated playlist. Returns the current selection as ("collection", None) or ("playlist", id).
    """
    with store.connect() as conn:
        playlists = store.list_playlists(conn)
    playlist_ids = {p["id"] for p in playlists}

    kind = st.session_state.get("nav_kind", "collection")
    playlist_id = st.session_state.get("nav_playlist_id")
    stale_playlist = kind == "playlist" and playlist_id not in playlist_ids
    if stale_playlist or kind not in ("collection", "playlist", "ytmusic"):
        kind, playlist_id = "collection", None

    expanded = st.session_state.get("nav_playlists_expanded", True)

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
                if not playlists:
                    st.caption("No playlists yet — create one from the Collection view.")
                for p in playlists:
                    is_selected = kind == "playlist" and p["id"] == playlist_id
                    if _nav_button(p["name"], key=f"nav_playlist_{p['id']}", selected=is_selected):
                        st.session_state["nav_kind"] = "playlist"
                        st.session_state["nav_playlist_id"] = p["id"]
                        st.rerun()

        st.divider()
        _render_ytmusic_nav_item(kind == "ytmusic")

    return kind, playlist_id


def _render_ytmusic_nav_item(selected: bool) -> None:
    """Sidebar nav row linking to the dedicated YT Music page, with a status pill showing
    whether an account is currently connected."""
    authenticated = ytmusic_client.is_authenticated()
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
    """
    st.subheader("YT Music")
    st.caption("Connect your account so matched tracks can sync to real playlists.")

    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown("**How to get your header values**")
        st.markdown("\n".join(f"{i}. {step}" for i, step in enumerate(ytmusic_client.SETUP_STEPS, 1)))

    with right:
        if ytmusic_client.is_authenticated():
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
                st.success("YT Music connected.")
                st.rerun()


def _render_playlist_detail(playlist: sqlite3.Row) -> None:
    playlist_id = playlist["id"]
    with store.connect() as conn:
        rows = resolve_playlist_rows(conn, playlist_id)

    matched = sum(1 for r in rows if r.matched)

    st.markdown(_PLAYLIST_ACTION_CSS, unsafe_allow_html=True)
    title_col, actions_col = st.columns([3, 2])
    with title_col:
        st.subheader(playlist["name"])
    with actions_col, st.container(horizontal=True, horizontal_alignment="right", gap="xxsmall"):
        _render_sync_button(playlist, rows)
        _render_delete_button(playlist)

    if st.session_state.get(f"confirm_sync_{playlist_id}"):
        _render_sync_confirmation(playlist, rows)
    if st.session_state.get(f"confirm_delete_{playlist_id}"):
        _render_delete_confirmation(playlist)

    st.caption(
        f"{len(rows)} track(s), {matched} matched"
        + (
            f" · linked to YT Music playlist `{playlist['ytmusic_playlist_id']}`"
            if playlist["ytmusic_playlist_id"]
            else ""
        )
    )

    if rows:
        df = _playlist_dataframe(rows)
        edited_df = st.data_editor(
            df,
            key=f"playlist_editor_{playlist_id}",
            hide_index=True,
            width="stretch",
            column_order=PLAYLIST_TRACK_COLUMNS,
            disabled=[c for c in PLAYLIST_TRACK_COLUMNS if c != "remove"],
            column_config={
                **SHARED_COLUMN_CONFIG,
                "remove": st.column_config.CheckboxColumn("", help="Select tracks to remove from this playlist"),
            },
        )
        to_remove = _selected_track_ids(edited_df, "remove")
        if st.button(f"Remove {len(to_remove)} selected", key=f"remove_button_{playlist_id}", disabled=not to_remove):
            with store.connect() as conn:
                store.remove_tracks_from_playlist(conn, playlist_id, to_remove)
                conn.commit()
            del st.session_state[f"playlist_editor_{playlist_id}"]
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


_PLAYLIST_ACTION_CSS = """
<style>
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

    st.warning(
        "This will create (or update) a real playlist on your YT Music account "
        f"named 'Discogs - {playlist['name']}' with these {len(video_ids)} track(s)."
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, push to YT Music", key=f"confirm_sync_yes_{playlist_id}"):
            st.session_state[confirm_key] = False
            if not ytmusic_client.is_authenticated():
                st.error("Not authenticated with YT Music. Use the YT Music page (in the sidebar) to connect.")
                return
            playlist_name = f"Discogs - {playlist['name']}"
            try:
                yt = ytmusic_client.get_client(authenticated=True)
                with store.connect() as conn:
                    existing_id = playlist["ytmusic_playlist_id"]
                    if existing_id:
                        ytmusic_id = existing_id
                    else:
                        ytmusic_id = ytmusic_client.get_or_create_playlist(
                            yt, playlist_name, description="Curated from the Discogs collection app"
                        )
                        store.set_playlist_ytmusic_id(conn, playlist_id, ytmusic_id)
                    conn.commit()
                ytmusic_client.add_tracks(yt, ytmusic_id, video_ids)
            except RuntimeError as e:
                st.error(str(e))
                return
            st.success(f"Pushed {len(video_ids)} track(s) to '{playlist_name}'.")
            st.rerun()
    with col2:
        if st.button("Cancel", key=f"confirm_sync_no_{playlist_id}"):
            st.session_state[confirm_key] = False
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
