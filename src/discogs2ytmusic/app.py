from __future__ import annotations

import pandas as pd
import streamlit as st

from discogs2ytmusic import store
from discogs2ytmusic.collection_edits import apply_artist_edits, apply_video_link_edits
from discogs2ytmusic.filters import PlaylistFilter, TrackRow, resolve_rows

st.set_page_config(page_title="Discogs -> YT Music", layout="wide")

COLLECTION_COLUMNS = [
    "release_title", "track_artist", "position", "track_title", "labels", "year", "matched", "discogs_url", "youtube_url",
    "channel", "release_artist", "styles", "genres", "confidence", "video_title",
]


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


def _rows_to_dataframe(rows: list[TrackRow]) -> pd.DataFrame:
    return pd.DataFrame(
        [
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
                "discogs_url": r.discogs_url,
            }
            for r in rows
        ]
    )


def render_collection_tab() -> None:
    st.header("My Discogs Collection")

    all_rows = _all_rows()
    if not all_rows:
        st.info("No collection cached yet. Run `discogs2ytmusic scan` first.")
        return

    tag_options = _tag_options(all_rows)
    label_options = _label_options(all_rows)
    year_lo, year_hi = _year_bounds(all_rows)

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        tags = st.multiselect("Style / genre", tag_options, key="collection_tags")
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

    df = _rows_to_dataframe(rows)
    edited_df = st.data_editor(
        df,
        key="collection_editor",
        hide_index=True,
        width="stretch",
        column_order=COLLECTION_COLUMNS,
        disabled=[
            "release_artist", "position", "track_title", "release_title", "discogs_url",
            "styles", "genres", "labels", "year", "matched", "confidence", "video_title", "channel",
        ],
        column_config={
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
            "channel": st.column_config.TextColumn(
                "Channel", help="Uploader/channel of the matched YouTube video"
            ),
            "confidence": st.column_config.ProgressColumn(
                "Confidence",
                help="Fuzzy-match score between the Discogs track and the YouTube result. Blank for manually-corrected links.",
                min_value=0,
                max_value=100,
                format="%d%%",
            ),
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


def main() -> None:
    tab1, tab2 = st.tabs(["My Discogs Collection", "Playlists"])
    with tab1:
        render_collection_tab()
    with tab2:
        st.info("Coming soon.")


main()
