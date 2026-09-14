from __future__ import annotations

import datetime
import json
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from discogs2ytmusic import filters, store

APP_PATH = str(Path(__file__).resolve().parents[1] / "src" / "discogs2ytmusic" / "app.py")


def _select_playlist(at: AppTest, playlist_id: int) -> AppTest:
    """Click the sidebar nav button for `playlist_id`, making it the active main-pane view."""
    return at.button(key=f"nav_playlist_{playlist_id}").click().run()


def _open_other_source(at: AppTest, source_id: int) -> AppTest:
    """Click the sidebar nav button for an "Other sources" page, making it the active
    main-pane view."""
    return at.button(key=f"nav_othersrc_{source_id}").click().run()


def _dataframe_by_prefix(at: AppTest, prefix: str) -> pd.DataFrame:
    """The value of the (single) `st.dataframe` on the page whose widget key starts with
    `prefix` — every table's key is `{prefix}_{digest}` (see `app._table_key`), so tests
    match the key prefix rather than assuming a static key."""
    for el in at.main.dataframe:
        if (el.key or "").startswith(prefix):
            return el.value
    raise AssertionError(f"no dataframe found with key prefix {prefix!r}")


def _dataframe_key_by_prefix(at: AppTest, prefix: str) -> str:
    """The widget key of the (single) `st.dataframe` on the page whose key starts with `prefix`."""
    for el in at.main.dataframe:
        if (el.key or "").startswith(prefix):
            return str(el.key)
    raise AssertionError(f"no dataframe found with key prefix {prefix!r}")


def _collection_table_df(at: AppTest) -> pd.DataFrame:
    """The Collection tab's main `st.dataframe` value."""
    return _dataframe_by_prefix(at, "collection_table_")


def _collection_table_key(at: AppTest) -> str:
    """The Collection tab's main `st.dataframe` widget key for the currently rendered page."""
    return _dataframe_key_by_prefix(at, "collection_table_")


def _playlist_table_df(at: AppTest, playlist_id: int) -> pd.DataFrame:
    """A playlist detail view's main tracks `st.dataframe` value."""
    return _dataframe_by_prefix(at, f"playlist_tracks_{playlist_id}_")


def _playlist_table_key(at: AppTest, playlist_id: int) -> str:
    """A playlist detail view's main tracks `st.dataframe` widget key."""
    return _dataframe_key_by_prefix(at, f"playlist_tracks_{playlist_id}_")


def _playlist_search_table_df(at: AppTest, playlist_id: int) -> pd.DataFrame:
    """A playlist detail view's "Add tracks" search-results `st.dataframe` value."""
    return _dataframe_by_prefix(at, f"playlist_search_table_{playlist_id}_")


def _playlist_search_table_key(at: AppTest, playlist_id: int) -> str:
    """A playlist detail view's "Add tracks" search-results `st.dataframe` widget key."""
    return _dataframe_key_by_prefix(at, f"playlist_search_table_{playlist_id}_")


def _select_table_rows(at: AppTest, table_key: str, positions: list[int]) -> AppTest:
    """Simulate a native shift-click (or ctrl-click) row selection on a table."""
    at.session_state[table_key] = {"selection": {"rows": positions, "columns": [], "cells": []}}
    return at.run()


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(
            conn,
            r["release_id"],
            r["artist"],
            r["title"],
            r["styles"],
            r["genres"],
            year=r.get("year"),
            labels=r.get("labels", []),
        )
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
        )


def test_app_shows_empty_state_when_no_collection_cached(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    assert not at.exception
    assert any("No collection cached yet" in i.value for i in at.info)


def test_app_lists_every_track_by_default(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    total_tracks = sum(len(r["tracklist"]) for r in dummy_library)
    assert at.main.caption[1].value == f"{total_tracks} tracks (0 matched)"


def test_app_tag_filter_narrows_the_table(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.multiselect(key="collection_tag_group_0").select("Acid").run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "Acid" in r["styles"])
    assert at.main.caption[1].value == f"{expected} tracks (0 matched)"


def test_app_search_box_narrows_the_table_by_release_title(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    yoyaku_release = next(r for r in dummy_library if r["title"] == "Yoyaku Barcelona 2025")
    at = AppTest.from_file(APP_PATH).run()
    at.text_input(key="collection_search").input("yoyaku").run()

    assert not at.exception
    assert at.main.caption[1].value == f"{len(yoyaku_release['tracklist'])} tracks (0 matched)"


def test_app_search_box_matches_case_insensitively_and_combines_with_tag_filter(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    query, tag = "DAN", "House"
    with store.connect() as conn:
        tag_filtered = filters.resolve_rows(conn, filters.PlaylistFilter(tag_groups=[filters.TagGroup(tags=[tag])]))
    expected = filters.filter_rows_by_query(tag_filtered, query)

    at = AppTest.from_file(APP_PATH).run()
    at.text_input(key="collection_search").input(query).run()
    at.multiselect(key="collection_tag_group_0").select(tag).run()

    assert not at.exception
    # The search box and the structured Style filter should narrow together (AND), not
    # one overriding the other.
    assert at.main.caption[1].value == f"{len(expected)} tracks (0 matched)"
    assert expected  # sanity: fixture actually produces a non-trivial combination, else this proves nothing


def test_select_all_checkbox_label_reflects_the_current_filtered_count(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    total = sum(len(r["tracklist"]) for r in dummy_library)
    assert at.checkbox(key="collection_select_all").label == f"Select all {total} filtered track(s)"

    at.multiselect(key="collection_tag_group_0").select("Acid").run()
    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "Acid" in r["styles"])
    assert at.checkbox(key="collection_select_all").label == f"Select all {expected} filtered track(s)"


def test_select_all_checkbox_is_disabled_when_the_filter_matches_nothing(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_matched_only").check().run()  # nothing matched yet -> zero rows

    assert not at.exception
    assert at.main.caption[1].value == "0 tracks (0 matched)"
    assert at.checkbox(key="collection_select_all").disabled is True


def test_checking_select_all_adds_every_filtered_track_to_a_new_playlist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.multiselect(key="collection_tag_group_0").select("Acid").run()
    at.checkbox(key="collection_select_all").check().run()
    at.selectbox(key="collection_add_target").select("+ Create new playlist").run()
    at.text_input(key="collection_new_playlist_name").input("Acid Picks").run()
    at.button(key="collection_add_button").click().run()

    assert not at.exception
    expected_titles = {t["title"] for r in dummy_library if "Acid" in r["styles"] for t in r["tracklist"]}
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Acid Picks")
        assert playlist is not None
        rows = filters.resolve_playlist_rows(conn, playlist["id"])
    assert {r.track_title for r in rows} == expected_titles


def test_checking_select_all_after_narrowing_further_only_adds_the_newly_filtered_rows(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_select_all").check().run()
    at.multiselect(key="collection_tag_group_0").select("Acid").run()  # narrow the filter with select-all already on
    at.selectbox(key="collection_add_target").select("+ Create new playlist").run()
    at.text_input(key="collection_new_playlist_name").input("Acid Only").run()
    at.button(key="collection_add_button").click().run()

    assert not at.exception
    expected_titles = {t["title"] for r in dummy_library if "Acid" in r["styles"] for t in r["tracklist"]}
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Acid Only")
        assert playlist is not None
        rows = filters.resolve_playlist_rows(conn, playlist["id"])
    assert {r.track_title for r in rows} == expected_titles


def test_selecting_rows_on_the_main_table_drives_the_add_to_playlist_label(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at = _select_table_rows(at, table_key, [0, 1, 2])

    assert not at.exception
    assert at.selectbox(key="collection_add_target").label == "Add 3 selected track(s) to"


def test_selecting_rows_on_the_main_table_and_adding_them_to_a_new_playlist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at = _select_table_rows(at, table_key, [0, 1, 2])
    expected_titles = set(_collection_table_df(at)["track_title"].iloc[:3])

    at.selectbox(key="collection_add_target").select("+ Create new playlist")
    at.text_input(key="collection_new_playlist_name").input("Range Picks")
    at.button(key="collection_add_button").click()
    at.session_state[table_key] = {"selection": {"rows": [0, 1, 2], "columns": [], "cells": []}}
    at.run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Range Picks")
        assert playlist is not None
        rows = filters.resolve_playlist_rows(conn, playlist["id"])
    assert {r.track_title for r in rows} == expected_titles


def test_creating_a_new_playlist_can_file_it_into_a_brand_new_folder(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_select_all").check().run()
    at.selectbox(key="collection_add_target").select("+ Create new playlist").run()
    at.text_input(key="collection_new_playlist_name").input("Acid Picks").run()
    at.selectbox(key="collection_new_playlist_folder").select("+ Create new folder").run()
    at.text_input(key="collection_new_playlist_folder_name").input("Genres").run()
    at.button(key="collection_add_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Acid Picks")
        folders = store.list_playlist_folders(conn)
    assert len(folders) == 1
    assert folders[0]["name"] == "Genres"
    assert playlist["folder_id"] == folders[0]["id"]


def test_creating_a_new_playlist_can_file_it_into_an_existing_folder(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_select_all").check().run()
    at.selectbox(key="collection_add_target").select("+ Create new playlist").run()
    at.text_input(key="collection_new_playlist_name").input("Acid Picks").run()
    at.selectbox(key="collection_new_playlist_folder").select("Genres").run()
    at.button(key="collection_add_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Acid Picks")
    assert playlist["folder_id"] == folder_id


def test_creating_a_new_playlist_with_no_folder_choice_leaves_it_ungrouped(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_select_all").check().run()
    at.selectbox(key="collection_add_target").select("+ Create new playlist").run()
    at.text_input(key="collection_new_playlist_name").input("Acid Picks").run()
    at.button(key="collection_add_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Acid Picks")
    assert playlist["folder_id"] is None


def test_select_all_overrides_whatever_is_selected_on_the_main_table(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at = _select_table_rows(at, table_key, [0])
    at.checkbox(key="collection_select_all").check()
    # A raw `session_state[key] = ...` assignment (unlike `.click()`/`.check()`/`.select()`)
    # only applies for the one `.run()` right after it, so it has to be restaged here to
    # land in the same rerun as the checkbox click (see `_select_table_rows` callers below).
    at.session_state[table_key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
    at.run()

    total = sum(len(r["tracklist"]) for r in dummy_library)
    assert not at.exception
    assert at.selectbox(key="collection_add_target").label == f"Add {total} selected track(s) to"


def test_app_tag_group_and_mode_requires_every_tag_in_the_group(isolated_cache, dummy_library):
    """Every fixture release is genre-tagged "Electronic", so an AND group of
    ["House", "Electronic"] should behave just like a plain "House" style filter."""
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.multiselect(key="collection_tag_group_0").select("House").select("Electronic").run()
    at.selectbox(key="collection_tag_group_mode_0").select("and").run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "House" in r["styles"])
    assert at.main.caption[1].value == f"{expected} tracks (0 matched)"


def test_app_add_style_group_button_adds_a_second_independent_group(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="collection_tag_group_add").click().run()

    assert not at.exception
    assert at.multiselect(key="collection_tag_group_1") is not None

    at.multiselect(key="collection_tag_group_0").select("House").run()
    at.multiselect(key="collection_tag_group_1").select("Techno").run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if set(r["styles"]) & {"House", "Techno"})
    assert at.main.caption[1].value == f"{expected} tracks (0 matched)"


def test_app_combining_style_groups_with_and_requires_every_group_to_match(isolated_cache, dummy_library):
    """No single fixture release is tagged both House and Techno, so combining a House
    group and a Techno group with AND should match nothing."""
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="collection_tag_group_add").click().run()
    at.multiselect(key="collection_tag_group_0").select("House").run()
    at.multiselect(key="collection_tag_group_1").select("Techno").run()
    at.radio(key="collection_tag_groups_mode").set_value("and").run()

    assert not at.exception
    assert at.main.caption[1].value == "0 tracks (0 matched)"


def test_app_removing_a_style_group_drops_its_tags_from_the_filter(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="collection_tag_group_add").click().run()
    at.multiselect(key="collection_tag_group_0").select("House").run()
    at.multiselect(key="collection_tag_group_1").select("Techno").run()
    at.button(key="collection_tag_group_remove_1").click().run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "House" in r["styles"])
    assert at.main.caption[1].value == f"{expected} tracks (0 matched)"
    assert not any(w.key == "collection_tag_group_1" for w in at.multiselect)


def test_app_a_single_style_group_has_no_remove_button_or_combinator(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert not any(b.key == "collection_tag_group_remove_0" for b in at.button)
    assert not any(r.key == "collection_tag_groups_mode" for r in at.radio)


def test_collection_table_key_changes_with_the_filtered_row_set(isolated_cache, dummy_library):
    """`_table_key` (app.py) is what makes #19's crash impossible: it derives a table's
    widget key from the row set's track_ids, so pending selection state (matched by row
    position) can never be reconciled against a differently-filtered, differently-shaped
    dataframe.
    """
    import discogs2ytmusic.app as app_module
    from discogs2ytmusic.filters import PlaylistFilter, TagGroup, resolve_rows

    with store.connect() as conn:
        _seed(conn, dummy_library)
        all_rows = resolve_rows(conn)
        acid_rows = resolve_rows(conn, PlaylistFilter(tag_groups=[TagGroup(tags=["Acid"])]))
        all_rows_again = resolve_rows(conn)

    assert 0 < len(acid_rows) < len(all_rows)
    assert app_module._table_key("collection_table", all_rows) != app_module._table_key("collection_table", acid_rows)
    # Same row set, recomputed independently -> same key, so unrelated reruns (e.g. a
    # widget elsewhere on the page changing) don't needlessly reset pending selection.
    assert app_module._table_key("collection_table", all_rows) == app_module._table_key(
        "collection_table", all_rows_again
    )
    # Different prefix, same row set -> different key, so the Collection tab's table and a
    # playlist detail view's table (or two different playlists') can never collide (#60).
    assert app_module._table_key("collection_table", all_rows) != app_module._table_key("playlist_tracks_1", all_rows)


def test_collection_table_widget_key_is_unique_per_filter_combination(isolated_cache, dummy_library):
    """Regression test for #19 ("selecting a label after narrowing by year+subgenre throws
    an error"): reproduce the narrowing sequence from the bug report and confirm each step
    renders the main table under a distinct widget key. `st.dataframe` matches its native
    row-selection state to the previous render by row position, not row identity, so
    reusing one static key across these differently-filtered row sets is what let a stale
    selection be misapplied or point past the end of a now-out-of-range dataframe;
    distinct keys per row set rule that out.
    """
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    unfiltered_key = at.main.dataframe[0].key

    at.slider(key="collection_year").set_range(1993, 2021).run()
    assert not at.exception
    year_key = at.main.dataframe[0].key

    at.multiselect(key="collection_tag_group_0").select("Acid").run()
    assert not at.exception
    tag_key = at.main.dataframe[0].key

    at.multiselect(key="collection_labels").select("Djax-Up-Beats").run()
    assert not at.exception
    label_key = at.main.dataframe[0].key

    assert len({unfiltered_key, year_key, tag_key, label_key}) == 4


def test_app_matched_only_checkbox_narrows_the_table(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_matched_only").check().run()

    assert not at.exception
    assert at.main.caption[1].value == "1 tracks (1 matched)"


def test_app_shows_position_in_its_own_column_and_keeps_track_title_clean(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    df = _collection_table_df(at)

    first = dummy_library[0]
    assert first["tracklist"][0]["position"] == "A"
    row = df[df["track_artist"] == first["artist"]].iloc[0]
    assert row["position"] == "A"
    assert row["track_title"] == first["tracklist"][0]["title"]


def test_app_handles_a_collection_with_a_single_distinct_year(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(
            conn, 1, "Solo Artist", "Solo EP", ["House"], ["Electronic"], year=2020, labels=["Some Label"]
        )
        store.replace_tracks(conn, 1, [("A1", "Track One", None, None)])

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert at.main.caption[1].value == "1 tracks (0 matched)"


def test_app_shows_match_confidence(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        fuzzy, manual = dummy_library[0], dummy_library[1]
        store.save_match(conn, fuzzy["artist"], fuzzy["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 87.0)
        store.save_match(conn, manual["artist"], manual["tracklist"][0]["title"], "vid2", "Video", "manual", None)

    at = AppTest.from_file(APP_PATH).run()
    df = _collection_table_df(at).set_index("track_artist")

    assert not at.exception
    assert df.loc[fuzzy["artist"], "confidence"] == 87.0
    assert pd.isna(df.loc[manual["artist"], "confidence"])


def test_app_shows_the_matched_video_channel(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(
            conn,
            first["artist"],
            first["tracklist"][0]["title"],
            "vid1",
            "Video",
            "ytmusic",
            90.0,
            channel="Yoyaku Record Store",
        )

    at = AppTest.from_file(APP_PATH).run()
    df = _collection_table_df(at).set_index("track_artist")

    assert not at.exception
    assert df.loc[first["artist"], "channel"] == "Yoyaku Record Store"


def test_app_channel_filter_narrows_the_table_to_that_channel(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first, second = dummy_library[0], dummy_library[1]
        store.save_match(
            conn,
            first["artist"],
            first["tracklist"][0]["title"],
            "vid1",
            "Video",
            "ytmusic",
            90.0,
            channel="Yoyaku Record Store",
        )
        store.save_match(
            conn,
            second["artist"],
            second["tracklist"][0]["title"],
            "vid2",
            "Video",
            "ytmusic",
            90.0,
            channel="Some Other Channel",
        )

    at = AppTest.from_file(APP_PATH).run()
    at.multiselect(key="collection_channels").select("Yoyaku Record Store").run()

    assert not at.exception
    df = _collection_table_df(at)
    assert df["channel"].tolist() == ["Yoyaku Record Store"]


def test_app_channel_filter_options_only_list_distinct_non_empty_channels(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(
            conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0, channel="Yoyaku"
        )

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert at.multiselect(key="collection_channels").options == ["Yoyaku"]


def test_app_shows_a_manually_corrected_match_as_locked(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "manual", None)

    at = AppTest.from_file(APP_PATH).run()
    df = _collection_table_df(at).set_index("track_artist")

    assert not at.exception
    assert bool(df.loc[first["artist"], "locked"]) is True


# --- Collection tab edit panel (#57) ---


def _edit_field_keys(track_id: int, key_prefix: str = "collection", gen: int = 0) -> tuple[str, str, str, str, str]:
    """The edit panel's widget keys for `track_id` under `key_prefix` — (artist, styles, genres,
    youtube_url, save). `key_prefix` defaults to the Collection tab's; a playlist detail view
    uses `playlist_{playlist_id}` (see `_render_edit_panel`). The four text_input keys carry a
    per-field "generation" suffix that a Reset action bumps (see `_render_edit_panel`'s
    docstring) — `gen` defaults to 0, the value every field starts at before any reset."""
    base = f"{key_prefix}_edit_{track_id}"
    return (
        f"{base}_artist_{gen}",
        f"{base}_styles_{gen}",
        f"{base}_genres_{gen}",
        f"{base}_youtube_url_{gen}",
        f"{base}_save",
    )


def test_edit_panel_shows_a_prompt_when_nothing_is_selected(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert any("Select a track above to edit" in c.value for c in at.main.caption)
    assert not any((ti.key or "").startswith("collection_edit_") for ti in at.main.text_input)


def test_edit_panel_prompts_to_narrow_the_selection_when_multiple_rows_are_selected(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at = _select_table_rows(at, table_key, [0, 1])

    assert not at.exception
    assert any("2 tracks selected" in c.value for c in at.main.caption)
    assert not any((ti.key or "").startswith("collection_edit_") for ti in at.main.text_input)


def test_edit_panel_is_prefilled_with_the_selected_track(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at = _select_table_rows(at, table_key, [0])
    row = _collection_table_df(at).iloc[0]
    artist_key, styles_key, genres_key, youtube_key, _ = _edit_field_keys(int(row["track_id"]))

    assert not at.exception
    assert at.text_input(key=artist_key).value == row["track_artist"]
    assert at.text_input(key=styles_key).value == row["styles"]
    assert at.text_input(key=genres_key).value == row["genres"]
    assert at.text_input(key=youtube_key).value == row["youtube_url"]


def test_saving_an_edit_panel_change_persists_an_artist_override(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at = _select_table_rows(at, table_key, [0])
    row = _collection_table_df(at).iloc[0]
    track_id = int(row["track_id"])
    artist_key, _, _, _, save_key = _edit_field_keys(track_id)

    at.text_input(key=artist_key).input("Corrected Artist")
    at.button(key=save_key).click()
    # A raw `session_state[key] = ...` assignment (unlike `.click()`/`.input()`) only
    # applies for the one `.run()` right after it, so the table's selection has to be
    # restaged here to land in the same rerun as the Save click (see `_select_table_rows`).
    at.session_state[table_key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
    at.run()

    # `_render_edit_panel` calls `st.rerun()` right after `st.success(...)` on a successful
    # save, so — same as the app's other post-save reruns — AppTest settles on the state
    # *after* that rerun, where there's nothing left to save; the persisted DB change below
    # is what actually confirms the save happened.
    assert not at.exception
    with store.connect() as conn2:
        rows = filters.resolve_rows(conn2)
    updated = next(r for r in rows if r.track_id == track_id)
    assert updated.track_artist == "Corrected Artist"
    assert updated.locked is True


def test_selecting_a_different_row_shows_that_rows_own_values(isolated_cache, dummy_library):
    """Each row's edit panel widget keys are derived from track_id (see `_render_edit_panel`),
    so switching the selected row renders a fresh panel rather than reusing widget state
    (and thus stale values) from whichever row was selected before."""
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    df = _collection_table_df(at)
    row_0 = df.iloc[0]
    other_pos = next(i for i in range(1, len(df)) if df.iloc[i]["track_artist"] != row_0["track_artist"])
    row_1 = df.iloc[other_pos]
    artist_key_0, *_ = _edit_field_keys(int(row_0["track_id"]))
    artist_key_1, *_ = _edit_field_keys(int(row_1["track_id"]))

    at = _select_table_rows(at, table_key, [0])
    assert at.text_input(key=artist_key_0).value == row_0["track_artist"]

    at = _select_table_rows(at, table_key, [other_pos])
    assert not at.exception
    assert at.text_input(key=artist_key_1).value == row_1["track_artist"]


# --- Edit panel per-field "Reset" buttons (#66) ---


def _select_collection_row_by_track_id(at: AppTest, table_key: str, track_id: int) -> tuple[AppTest, int]:
    df = _collection_table_df(at)
    position = next(i for i in range(len(df)) if int(df.iloc[i]["track_id"]) == track_id)
    return _select_table_rows(at, table_key, [position]), position


def _click_reset_button(at: AppTest, table_key: str, position: int, reset_key: str) -> AppTest:
    """Click a per-field Reset button and land on the state after the `st.rerun()` it
    triggers, with the row still selected. Same restaging idiom `_edit_field_keys`'s
    save-button callers use: a raw `session_state[table_key] = ...` assignment (unlike
    `.click()`) only applies to the one `.run()` right after it, so the selection has to be
    reasserted alongside the click to land in the same rerun."""
    at.button(key=reset_key).click()
    at.session_state[table_key] = {"selection": {"rows": [position], "columns": [], "cells": []}}
    return at.run()


def test_reset_buttons_are_disabled_when_their_field_has_no_override(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    track_id = int(_collection_table_df(at).iloc[0]["track_id"])
    at = _select_table_rows(at, table_key, [0])

    for field in ("artist", "styles", "genres", "video"):
        assert at.button(key=f"collection_edit_{track_id}_reset_{field}").disabled is True


def test_reset_artist_button_clears_the_override_and_reverts_the_field(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        release, tracks = next(iter(store.iter_releases_with_tracks(conn)))
        track_id = tracks[0]["id"]
        store.set_track_search_artist(conn, track_id, "Corrected Artist")

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at, position = _select_collection_row_by_track_id(at, table_key, track_id)

    reset_key = f"collection_edit_{track_id}_reset_artist"
    assert at.button(key=reset_key).disabled is False
    at = _click_reset_button(at, table_key, position, reset_key)

    # Same as the save-button tests above: AppTest settles on the state after
    # `_render_edit_panel`'s post-reset `st.rerun()`, where the reset row's `track_artist`
    # changed — and `resolve_rows` sorts by `(track_artist, track_title)` — so the row's own
    # table position (and thus whether the just-reasserted selection still lands on it) can
    # shift. The persisted DB change is what actually confirms the reset happened.
    assert not at.exception
    with store.connect() as conn2:
        rows = filters.resolve_rows(conn2)
    updated = next(r for r in rows if r.track_id == track_id)
    assert updated.artist_overridden is False
    assert updated.locked is False
    assert updated.track_artist != "Corrected Artist"


def test_reset_styles_button_clears_the_override_and_reverts_the_field(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        release, tracks = next(iter(store.iter_releases_with_tracks(conn)))
        track_id = tracks[0]["id"]
        store.set_track_styles_override(conn, track_id, ["Track-Only Style"])

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at, position = _select_collection_row_by_track_id(at, table_key, track_id)

    reset_key = f"collection_edit_{track_id}_reset_styles"
    assert at.button(key=reset_key).disabled is False
    at = _click_reset_button(at, table_key, position, reset_key)

    assert not at.exception
    with store.connect() as conn2:
        rows = filters.resolve_rows(conn2)
    updated = next(r for r in rows if r.track_id == track_id)
    assert updated.styles_overridden is False
    assert "Track-Only Style" not in updated.styles


def test_reset_video_button_deletes_the_match_and_searches_again(isolated_cache, dummy_library, monkeypatch):
    """Unlike blanking-and-saving the YouTube link cell (which rejects — `source='manual',
    video_id=None`, and is then left alone by `sync`/`rematch` forever), the Reset button
    must delete the match outright and search again immediately (#66)."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        release, tracks = next(iter(store.iter_releases_with_tracks(conn)))
        track_id, artist, title = store.effective_track_queries(release, tracks)[0]
        store.save_match(conn, artist, title, "manual-vid", "Manual pick", "manual", None)

    import discogs2ytmusic.app as app_module
    from discogs2ytmusic import matcher
    from discogs2ytmusic.matcher import MatchResult

    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        matcher, "find_match", lambda yt, artist, title: MatchResult("fresh-id", title, "ytmusic", 90.0)
    )

    at = AppTest.from_file(APP_PATH).run()
    table_key = _collection_table_key(at)
    at, position = _select_collection_row_by_track_id(at, table_key, track_id)

    reset_key = f"collection_edit_{track_id}_reset_video"
    assert at.button(key=reset_key).disabled is False
    at = _click_reset_button(at, table_key, position, reset_key)

    assert not at.exception
    with store.connect() as conn2:
        match = store.get_match(conn2, artist, title)
    assert match is not None
    assert match["video_id"] == "fresh-id"
    assert match["source"] == "ytmusic"


# --- Scan / Sync matches / Rematch buttons ---
#
# AppTest re-executes the whole app.py source from scratch on every `.run()`, so patching a
# name defined directly at module level in app.py (e.g. a plain `def` there) doesn't stick —
# the fresh exec just redefines it. What does stick is patching an attribute on an already
# -imported module (`config.Config.load`, `discogs.DiscogsClient`, `ytmusic_client.get_client`,
# ...): those modules are cached in sys.modules, so app.py's own fresh `import`/`from ... import`
# lines just re-bind to the same (now-patched) objects.


def _mock_discogs_client(monkeypatch, fake_discogs_client, username="dummyuser"):
    """Make `Config.load()` report saved credentials and `DiscogsClient(token)` return
    `fake_discogs_client`, so app.py's scan action runs against the offline fixture."""
    import discogs2ytmusic.config as config_module
    import discogs2ytmusic.discogs as discogs_module

    monkeypatch.setattr(
        config_module.Config,
        "load",
        classmethod(lambda cls: config_module.Config(discogs_token="test-token", discogs_username=username)),
    )
    monkeypatch.setattr(discogs_module, "DiscogsClient", lambda token: fake_discogs_client)


def test_collection_tab_shows_scan_sync_and_rematch_buttons(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert at.button(key="scan_button")
    assert at.button(key="sync_matches_button")
    assert at.button(key="rematch_button")


def test_empty_state_still_offers_the_scan_button(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert any("No collection cached yet" in i.value for i in at.info)
    assert at.button(key="scan_button")


def test_scan_button_shows_error_when_discogs_not_authenticated(isolated_cache, monkeypatch):
    import discogs2ytmusic.config as config_module

    monkeypatch.setattr(config_module.Config, "load", classmethod(lambda cls: config_module.Config(None, None)))

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="scan_button").click().run()

    assert not at.exception
    assert any("Not authenticated with Discogs" in e.value for e in at.error)


def test_scan_button_populates_the_cache_from_discogs(isolated_cache, fake_discogs_client, monkeypatch):
    _mock_discogs_client(monkeypatch, fake_discogs_client)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="scan_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))
    assert len(releases) == 15
    assert sum(len(tracks) for _release, tracks in releases) == 18


def test_scan_button_skips_a_release_discogs_cant_return_instead_of_aborting(
    isolated_cache, fake_discogs_client, monkeypatch
):
    """A release detail fetch can 404 (e.g. a wantlist item merged into another release id,
    or pulled from Discogs entirely) — that must not abort the whole scan and strand every
    release already committed before it, only skip that one."""
    from discogs2ytmusic.discogs import DiscogsError

    poisoned_id = fake_discogs_client._releases[2]["release_id"]
    real_get_release_detail = fake_discogs_client.get_release_detail

    def flaky_get_release_detail(release_id: int):
        if release_id == poisoned_id:
            raise DiscogsError(f"Discogs API error 404 for /releases/{release_id}: not found")
        return real_get_release_detail(release_id)

    monkeypatch.setattr(fake_discogs_client, "get_release_detail", flaky_get_release_detail)
    _mock_discogs_client(monkeypatch, fake_discogs_client)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="scan_button").click().run()

    # `_run_scan`'s own st.warning/st.success calls don't survive its immediate
    # `st.rerun()` on success (same as every other scan_button test here, which likewise
    # only assert on the resulting cache state) — so assert on what was actually persisted.
    assert not at.exception
    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))
    assert len(releases) == 14  # every release except the poisoned one
    assert poisoned_id not in {r["release_id"] for r, _tracks in releases}


def test_scan_button_never_clobbers_a_manual_artist_override(isolated_cache, fake_discogs_client, monkeypatch):
    """Regression guard for the CLAUDE.md locking invariant: a Scan (`scan --refresh`
    equivalent) must never wipe out a manual `search_artist` correction."""
    _mock_discogs_client(monkeypatch, fake_discogs_client)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="scan_button").click().run()

    with store.connect() as conn:
        release_id = fake_discogs_client._releases[0]["release_id"]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release_id,)).fetchone()[0]
        store.set_track_search_artist(conn, track_id, "My Override")
        conn.commit()

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="scan_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        track = store.get_track(conn, track_id)
    assert track["search_artist"] == "My Override"


def test_sync_matches_button_matches_unmatched_tracks(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    import discogs2ytmusic.app as app_module
    from discogs2ytmusic import matcher
    from discogs2ytmusic.matcher import MatchResult

    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        matcher, "find_match", lambda yt, artist, title: MatchResult(f"vid::{title}", title, "ytmusic", 90.0)
    )

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="sync_matches_button").click().run()

    assert not at.exception
    total_tracks = sum(len(r["tracklist"]) for r in dummy_library)
    with store.connect() as conn:
        assert store.count_matches(conn) == total_tracks


def test_sync_matches_button_with_no_collection_shows_info_and_does_nothing(isolated_cache, monkeypatch):
    import discogs2ytmusic.app as app_module

    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="sync_matches_button").click().run()

    assert not at.exception
    assert any("Nothing to match" in i.value for i in at.info)
    with store.connect() as conn:
        assert store.count_matches(conn) == 0


def test_rematch_button_requires_confirmation(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="rematch_button").click().run()

    assert not at.exception
    assert any("This will delete" in w.value for w in at.warning)
    with store.connect() as conn:
        assert store.count_matches(conn) == 1  # untouched until confirmed


def test_cancelling_rematch_leaves_matches_untouched(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="rematch_button").click().run()
    at.button(key="confirm_rematch_no").click().run()

    assert not at.exception
    with store.connect() as conn:
        assert store.count_matches(conn) == 1


def test_confirming_rematch_clears_and_rebuilds_matches_preserving_manual_by_default(
    isolated_cache, dummy_library, fake_discogs_client, monkeypatch
):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        fuzzy, manual = dummy_library[0], dummy_library[1]
        store.save_match(conn, fuzzy["artist"], fuzzy["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        store.save_match(
            conn, manual["artist"], manual["tracklist"][0]["title"], "manually-picked", "Manual pick", "manual", None
        )

    import discogs2ytmusic.app as app_module
    from discogs2ytmusic import matcher
    from discogs2ytmusic.matcher import MatchResult

    _mock_discogs_client(monkeypatch, fake_discogs_client)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        matcher, "find_match", lambda yt, artist, title: MatchResult(f"vid::{title}", title, "ytmusic", 100.0)
    )

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="rematch_button").click().run()
    at.button(key="confirm_rematch_yes").click().run()

    assert not at.exception
    with store.connect() as conn:
        rebuilt = store.get_match(conn, fuzzy["artist"], fuzzy["tracklist"][0]["title"])
        kept_manual = store.get_match(conn, manual["artist"], manual["tracklist"][0]["title"])
    assert rebuilt["video_id"] == f"vid::{fuzzy['tracklist'][0]['title']}"
    assert kept_manual["video_id"] == "manually-picked"
    assert kept_manual["source"] == "manual"


def test_confirming_rematch_with_include_manual_clears_manual_corrections_too(
    isolated_cache, dummy_library, fake_discogs_client, monkeypatch
):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(
            conn, first["artist"], first["tracklist"][0]["title"], "manually-picked", "Manual pick", "manual", None
        )

    import discogs2ytmusic.app as app_module
    from discogs2ytmusic import matcher
    from discogs2ytmusic.matcher import MatchResult

    _mock_discogs_client(monkeypatch, fake_discogs_client)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        matcher, "find_match", lambda yt, artist, title: MatchResult(f"vid::{title}", title, "ytmusic", 100.0)
    )

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="rematch_button").click().run()
    at.checkbox(key="rematch_include_manual").check().run()
    at.button(key="confirm_rematch_yes").click().run()

    assert not at.exception
    with store.connect() as conn:
        match = store.get_match(conn, first["artist"], first["tracklist"][0]["title"])
    assert match["video_id"] != "manually-picked"
    assert match["source"] != "manual"


# --- Sidebar nav / Playlists ---


def test_sidebar_shows_empty_state_when_no_playlists_exist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert any("No playlists yet" in c.value for c in at.sidebar.caption)


# --- Playlist folders ---


def _open_folder(at: AppTest, folder_id: int) -> AppTest:
    """Click the sidebar nav button for `folder_id`, opening its detail page in the main pane."""
    return at.button(key=f"nav_folder_{folder_id}").click().run()


def _open_playlist_in_folder(at: AppTest, folder_id: int, playlist_id: int) -> AppTest:
    """Open a folder's detail page, then click one of its playlists from there — one of two
    ways to reach a grouped playlist's detail view, the other being the sidebar's own
    expand/collapse chevron (see `test_clicking_the_folder_chevron_...` below)."""
    at = _open_folder(at, folder_id)
    return at.button(key=f"folder_playlist_{playlist_id}").click().run()


def test_a_collapsed_folder_hides_its_playlists_from_the_sidebar(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert any(b.key == f"nav_folder_{folder_id}" for b in at.button)  # the folder row itself renders
    assert not any(b.key == f"nav_playlist_{playlist_id}" for b in at.button)  # but its contents are hidden


def test_clicking_the_folder_chevron_expands_it_to_show_its_playlists_in_the_sidebar(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key=f"nav_folder_toggle_{folder_id}").click().run()

    assert not at.exception
    assert any(b.key == f"nav_playlist_{playlist_id}" for b in at.button)
    assert any(h.value == "My Discogs Collection" for h in at.main.header)  # expanding didn't navigate anywhere


def test_clicking_a_playlist_inside_an_expanded_folder_opens_its_detail_view(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key=f"nav_folder_toggle_{folder_id}").click().run()
    at = _select_playlist(at, playlist_id)

    assert not at.exception
    assert at.session_state["nav_kind"] == "playlist"
    assert at.session_state["nav_playlist_id"] == playlist_id
    assert any(h.value == "My Favorites" for h in at.main.subheader)


def test_clicking_a_folder_opens_its_detail_page_listing_its_playlists(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    at = _open_folder(AppTest.from_file(APP_PATH).run(), folder_id)

    assert not at.exception
    assert at.session_state["nav_kind"] == "folder"
    assert any(h.value == "Genres" for h in at.main.subheader)
    assert any(
        b.key == f"folder_playlist_{playlist_id}" and b.label == "My Favorites - 1 track" for b in at.main.button
    )


def test_folder_detail_page_shows_a_placeholder_when_empty(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")

    at = _open_folder(AppTest.from_file(APP_PATH).run(), folder_id)

    assert not at.exception
    assert any("No playlists in this folder yet" in c.value for c in at.main.caption)


def test_clicking_a_playlist_in_the_folder_detail_page_opens_its_detail_view(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = _open_playlist_in_folder(AppTest.from_file(APP_PATH).run(), folder_id, playlist_id)

    assert not at.exception
    assert at.session_state["nav_kind"] == "playlist"
    assert at.session_state["nav_playlist_id"] == playlist_id
    assert any(h.value == "My Favorites" for h in at.main.subheader)


def test_playlists_outside_any_folder_still_render_directly_in_the_sidebar(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        grouped_id = store.create_playlist(conn, "Grouped")
        store.set_playlist_folder(conn, grouped_id, folder_id)
        ungrouped_id = store.create_playlist(conn, "Ungrouped")

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert any(b.key == f"nav_playlist_{ungrouped_id}" for b in at.button)  # rendered directly, no expand needed
    assert not any(b.key == f"nav_playlist_{grouped_id}" for b in at.button)  # still hidden inside its collapsed folder


def test_playlist_detail_can_move_a_playlist_into_a_new_folder(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        playlist_id = store.create_playlist(conn, "My Favorites")

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.selectbox(key=f"playlist_folder_choice_{playlist_id}").select("+ Create new folder").run()
    at.text_input(key=f"playlist_folder_new_name_{playlist_id}").input("Genres").run()
    at.button(key=f"playlist_folder_move_{playlist_id}").click().run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
        folders = store.list_playlist_folders(conn)
    assert len(folders) == 1
    assert playlist["folder_id"] == folders[0]["id"]


def test_playlist_detail_can_move_a_playlist_back_to_ungrouped(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = _open_playlist_in_folder(AppTest.from_file(APP_PATH).run(), folder_id, playlist_id)
    at.selectbox(key=f"playlist_folder_choice_{playlist_id}").select("No folder").run()
    at.button(key=f"playlist_folder_move_{playlist_id}").click().run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist["folder_id"] is None


def test_playlist_detail_folder_picker_defaults_to_the_playlists_current_folder(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = _open_playlist_in_folder(AppTest.from_file(APP_PATH).run(), folder_id, playlist_id)

    assert not at.exception
    assert at.selectbox(key=f"playlist_folder_choice_{playlist_id}").value == "Genres"


def test_clicking_delete_on_a_folder_page_shows_a_confirmation_and_does_not_delete_yet(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = _open_folder(AppTest.from_file(APP_PATH).run(), folder_id)
    at.button(key=f"delete_folder_button_{folder_id}").click().run()

    assert not at.exception
    assert any("won't be deleted" in w.value for w in at.main.warning)
    with store.connect() as conn:
        assert len(store.list_playlist_folders(conn)) == 1  # not deleted yet — only warned


def test_confirming_folder_delete_removes_the_folder_but_not_its_playlists(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.set_playlist_folder(conn, playlist_id, folder_id)

    at = _open_folder(AppTest.from_file(APP_PATH).run(), folder_id)
    at.button(key=f"delete_folder_button_{folder_id}").click().run()
    at.button(key=f"confirm_delete_folder_yes_{folder_id}").click().run()

    assert not at.exception
    assert at.session_state["nav_kind"] == "collection"  # the folder page no longer exists
    with store.connect() as conn:
        assert store.list_playlist_folders(conn) == []
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist is not None
    assert playlist["folder_id"] is None


def test_cancelling_folder_delete_keeps_the_folder(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")

    at = _open_folder(AppTest.from_file(APP_PATH).run(), folder_id)
    at.button(key=f"delete_folder_button_{folder_id}").click().run()
    at.button(key=f"confirm_delete_folder_no_{folder_id}").click().run()

    assert not at.exception
    assert at.session_state["nav_kind"] == "folder"
    with store.connect() as conn:
        assert len(store.list_playlist_folders(conn)) == 1


def test_deleting_an_empty_folder_shows_a_simpler_confirmation(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        folder_id = store.create_playlist_folder(conn, "Genres")

    at = _open_folder(AppTest.from_file(APP_PATH).run(), folder_id)
    at.button(key=f"delete_folder_button_{folder_id}").click().run()

    assert not at.exception
    assert any("empty folder" in w.value for w in at.main.warning)


def test_playlist_detail_shows_track_and_matched_counts(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)

    assert not at.exception
    assert any("1 track(s), 1 matched" in c.value for c in at.main.caption)
    assert any("Not yet pushed to YT Music" in c.value for c in at.main.caption)


def test_playlist_detail_shows_a_bold_link_to_the_pushed_playlist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])
        store.set_playlist_ytmusic_id(conn, playlist_id, "abc123")

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)

    assert not at.exception
    linked_markdown = [m.value for m in at.main.markdown if "1 track(s), 1 matched" in m.value]
    assert len(linked_markdown) == 1
    assert 'href="https://music.youtube.com/playlist?list=abc123"' in linked_markdown[0]
    assert 'target="_blank"' in linked_markdown[0]
    assert 'rel="noopener"' in linked_markdown[0]
    assert "font-weight: 700" in linked_markdown[0]
    assert "Linked to YT Music playlist" in linked_markdown[0]
    assert not any("1 track(s), 1 matched" in c.value for c in at.main.caption)


def test_playlist_search_excludes_tracks_already_in_the_playlist(isolated_cache, dummy_library):
    multi_track_release = next(r for r in dummy_library if len(r["tracklist"]) > 1)

    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM tracks WHERE release_id = ? ORDER BY id", (multi_track_release["release_id"],)
            )
        ]
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_ids[0]])  # only the first of several

    # track_ids[0] and [1] share the same resolved (discogs_artist) artist — search on that,
    # not the raw release artist string, which VA releases like this one don't map 1:1 to a track.
    shared_artist = multi_track_release["tracklist"][0]["discogs_artist"]
    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.text_input(key=f"playlist_search_{playlist_id}").input(shared_artist).run()

    assert not at.exception
    search_df = _playlist_search_table_df(at, playlist_id)
    assert track_ids[0] not in search_df["track_id"].tolist()
    assert track_ids[1] in search_df["track_id"].tolist()  # still findable — not yet in the playlist


def test_playlist_search_matches_by_artist_or_title_case_insensitively(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        playlist_id = store.create_playlist(conn, "My Favorites")
        second = dummy_library[1]

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.text_input(key=f"playlist_search_{playlist_id}").input(second["artist"].lower()).run()

    assert not at.exception
    search_df = _playlist_search_table_df(at, playlist_id)
    assert (search_df["track_artist"] == second["artist"]).any()


# --- Playlist detail view: shift-click table selection + edit panel (#60) ---
#
# Same UX as the Collection tab (#57): the tracks table and the "Add tracks" search-results
# table are both `st.dataframe`s with native multi-row selection, and per-track corrections
# go through the shared `_render_edit_panel` scoped to this playlist's own widget keys.


def _all_track_ids(conn) -> list[int]:
    return [row[0] for row in conn.execute("SELECT id FROM tracks ORDER BY id")]


def test_playlist_table_multi_row_selection_removes_several_tracks_at_once(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_ids = _all_track_ids(conn)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, track_ids[:3])

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    table_key = _playlist_table_key(at, playlist_id)
    df = _playlist_table_df(at, playlist_id)
    at = _select_table_rows(at, table_key, [0, 2])

    assert not at.exception
    assert at.button(key=f"remove_button_{playlist_id}").label == "Remove 2 selected"

    at.button(key=f"remove_button_{playlist_id}").click()
    at.session_state[table_key] = {"selection": {"rows": [0, 2], "columns": [], "cells": []}}
    at.run()

    assert not at.exception
    with store.connect() as conn:
        rows = filters.resolve_playlist_rows(conn, playlist_id)
    assert {r.track_id for r in rows} == {int(df.iloc[1]["track_id"])}


def test_playlist_search_table_multi_row_selection_adds_several_tracks_at_once(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        playlist_id = store.create_playlist(conn, "My Favorites")

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    # "a" matches most fixture tracks by artist or title — plenty to pick two rows from.
    at.text_input(key=f"playlist_search_{playlist_id}").input("a").run()
    search_table_key = _playlist_search_table_key(at, playlist_id)
    search_df = _playlist_search_table_df(at, playlist_id)
    expected_ids = {int(search_df.iloc[0]["track_id"]), int(search_df.iloc[1]["track_id"])}
    at = _select_table_rows(at, search_table_key, [0, 1])

    assert not at.exception
    assert at.button(key=f"add_from_search_{playlist_id}").label == "Add 2 selected"

    at.button(key=f"add_from_search_{playlist_id}").click()
    at.session_state[search_table_key] = {"selection": {"rows": [0, 1], "columns": [], "cells": []}}
    at.run()

    assert not at.exception
    with store.connect() as conn:
        rows = filters.resolve_playlist_rows(conn, playlist_id)
    assert {r.track_id for r in rows} == expected_ids


def test_playlist_edit_panel_saves_a_correction_scoped_to_this_playlist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_ids = _all_track_ids(conn)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, track_ids[:1])

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    table_key = _playlist_table_key(at, playlist_id)
    at = _select_table_rows(at, table_key, [0])
    row = _playlist_table_df(at, playlist_id).iloc[0]
    track_id = int(row["track_id"])
    key_prefix = f"playlist_{playlist_id}"
    artist_key, _, _, _, save_key = _edit_field_keys(track_id, key_prefix)

    # Prefilled with the selected row's own values, under this playlist's own key prefix —
    # distinct from the Collection tab's edit panel keys for the same track_id.
    assert at.text_input(key=artist_key).value == row["track_artist"]
    assert not any((ti.key or "").startswith(f"collection_edit_{track_id}") for ti in at.main.text_input)

    at.text_input(key=artist_key).input("Playlist-Scoped Artist")
    at.button(key=save_key).click()
    # A raw `session_state[key] = ...` assignment only applies for the one `.run()` right after
    # it, so the table's selection has to be restaged here to land in the same rerun as the
    # Save click (see `_select_table_rows`).
    at.session_state[table_key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
    at.run()

    assert not at.exception
    with store.connect() as conn2:
        rows = filters.resolve_rows(conn2)
    updated = next(r for r in rows if r.track_id == track_id)
    assert updated.track_artist == "Playlist-Scoped Artist"
    assert updated.locked is True


def test_playlist_edit_panel_prompts_when_zero_or_multiple_rows_selected(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_ids = _all_track_ids(conn)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, track_ids[:2])

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)

    assert not at.exception
    assert any("Select a track above to edit" in c.value for c in at.main.caption)

    table_key = _playlist_table_key(at, playlist_id)
    at = _select_table_rows(at, table_key, [0, 1])

    assert not at.exception
    assert any("2 tracks selected" in c.value for c in at.main.caption)


def test_deleting_a_playlist_requires_confirmation(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        playlist_id = store.create_playlist(conn, "My Favorites")

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"delete_button_{playlist_id}").click().run()

    assert not at.exception
    assert any("does NOT delete" in w.value for w in at.warning)
    with store.connect() as conn:
        assert len(store.list_playlists(conn)) == 1  # not deleted yet — only warned


def test_confirming_delete_removes_the_playlist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        playlist_id = store.create_playlist(conn, "My Favorites")

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"delete_button_{playlist_id}").click().run()
    at.button(key=f"confirm_delete_yes_{playlist_id}").click().run()

    assert not at.exception
    with store.connect() as conn:
        assert store.list_playlists(conn) == []


def test_cancelling_delete_keeps_the_playlist(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        playlist_id = store.create_playlist(conn, "My Favorites")

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"delete_button_{playlist_id}").click().run()
    at.button(key=f"confirm_delete_no_{playlist_id}").click().run()

    assert not at.exception
    with store.connect() as conn:
        assert len(store.list_playlists(conn)) == 1


def test_sync_requires_confirmation_and_never_touches_ytmusic_without_it(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    def _blow_up(*a, **k):
        raise AssertionError("should not touch YT Music without confirmation")

    # is_authenticated is legitimately called on every render for the sidebar's connection
    # status, so only the actual network-touching calls are guarded here.
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", _blow_up)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()

    assert not at.exception
    assert any("This will create" in w.value for w in at.warning)

    at.button(key=f"confirm_sync_no_{playlist_id}").click().run()
    assert not at.exception


def test_sync_confirmation_warns_when_two_tracks_share_the_same_video(isolated_cache, dummy_library, monkeypatch):
    """Two different Discogs tracks matched to the same YouTube video is usually a mismatch worth
    a second look — and, left undeduped, can make YT Music reject a whole add request — so the
    sync confirmation should call it out before the user proceeds."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first, second = dummy_library[0], dummy_library[1]
        track_ids = []
        for release in (first, second):
            track_ids.append(
                conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]
            )
            store.save_match(
                conn, release["artist"], release["tracklist"][0]["title"], "shared-vid", "Video", "ytmusic", 90.0
            )
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, track_ids)

    import discogs2ytmusic.app as app_module

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: False)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()

    assert not at.exception
    warning = next(w.value for w in at.warning if "Same YouTube video" in w.value)
    assert f"{first['artist']} - {first['tracklist'][0]['title']}" in warning
    assert f"{second['artist']} - {second['tracklist'][0]['title']}" in warning


def test_sync_confirmation_has_no_duplicate_video_warning_when_all_matches_are_distinct(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()

    assert not at.exception
    assert not any("Same YouTube video" in w.value for w in at.warning)


def _patch_sync_happy_path(
    monkeypatch,
    app_module,
    found_playlist_id: str | None = None,
    remote_tracks: list[dict] | None = None,
    created_playlist_id: str = "ytmusic-playlist-id",
    created: bool = True,
) -> tuple[list, dict, list]:
    """Wire up fakes for a full sync round-trip and return (created_names, pushed, removed)."""
    created_names: list[str] = []
    pushed: dict[str, list[str]] = {}
    removed: list[tuple[str, list[dict]]] = []
    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(app_module.ytmusic_client, "find_playlist", lambda yt, name: found_playlist_id)
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", lambda yt, playlist_id: remote_tracks or [])
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": (created_names.append(name) or created_playlist_id, created),
    )
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "add_tracks",
        lambda yt, playlist_id, video_ids: pushed.setdefault(playlist_id, []).extend(video_ids),
    )
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "remove_tracks",
        lambda yt, playlist_id, tracks: removed.append((playlist_id, tracks)) if tracks else None,
    )
    return created_names, pushed, removed


def test_confirming_sync_pushes_matched_tracks_to_ytmusic(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    created_names, pushed, removed = _patch_sync_happy_path(monkeypatch, app_module)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert created_names == ["Discogs - My Favorites"]
    assert pushed == {"ytmusic-playlist-id": ["vid1"]}
    assert removed == []
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist["ytmusic_playlist_id"] == "ytmusic-playlist-id"
    assert playlist["pushed_at"] is not None


def test_pushed_at_is_recorded_again_on_a_resync_of_an_already_linked_playlist(
    isolated_cache, dummy_library, monkeypatch
):
    """`pushed_at` must reflect the most recent push, not just the first link — the normal
    workflow is adding tracks and re-syncing an already-linked playlist repeatedly."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])
        store.set_playlist_ytmusic_id(conn, playlist_id, "already-linked-id")
        conn.commit()

    import discogs2ytmusic.app as app_module

    _patch_sync_happy_path(monkeypatch, app_module, created_playlist_id="already-linked-id", created=False)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist["ytmusic_playlist_id"] == "already-linked-id"
    assert playlist["pushed_at"] is not None
    assert any("Pushed to YT Music" in c.value for c in at.main.caption)


def test_confirming_sync_uses_the_configured_playlist_name_prefix(isolated_cache, dummy_library, monkeypatch):
    from discogs2ytmusic.config import Config

    Config(playlist_name_prefix="My Vinyl -").save()

    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    created_names, _pushed, _removed = _patch_sync_happy_path(monkeypatch, app_module)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert created_names == ["My Vinyl - My Favorites"]


def test_sync_recovers_from_a_stale_saved_playlist_id(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])
        store.set_playlist_ytmusic_id(conn, playlist_id, "stale-id")
        conn.commit()

    from ytmusicapi.exceptions import YTMusicServerError

    import discogs2ytmusic.app as app_module

    created = []
    pushed = {}

    def _add_tracks(yt, pid, video_ids):
        if pid == "stale-id":
            raise YTMusicServerError("Server returned HTTP 404: Not Found.")
        pushed.setdefault(pid, []).extend(video_ids)

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", lambda yt, playlist_id: [])
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": (created.append(name) or "fresh-id", True),
    )
    monkeypatch.setattr(app_module.ytmusic_client, "add_tracks", _add_tracks)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert not at.error
    assert created == ["Discogs - My Favorites"]
    assert pushed == {"fresh-id": ["vid1"]}
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist["ytmusic_playlist_id"] == "fresh-id"

    # The retry recovered fully — no need to flag the session as suspect.
    at.run()
    assert "is-connected" in _sidebar_pill(at)


def test_sync_recovers_from_a_deleted_playlist_raising_a_bare_keyerror(isolated_cache, dummy_library, monkeypatch):
    """A playlist deleted on the YT Music side doesn't raise a clean YTMusicError when fetched —
    ytmusicapi's `get_playlist` hits a malformed-for-this-case browse response and its internal
    `nav()` helper raises a bare KeyError instead. That must still trigger the same stale-id
    recovery as a YTMusicError, not crash the app."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])
        store.set_playlist_ytmusic_id(conn, playlist_id, "deleted-id")
        conn.commit()

    import discogs2ytmusic.app as app_module

    created = []
    pushed = {}

    def _get_playlist_tracks(yt, pid):
        if pid == "deleted-id":
            raise KeyError("Unable to find 'contents' using path [...] on {...}, exception: 'contents'")
        return []

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", _get_playlist_tracks)
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": (created.append(name) or "fresh-id", True),
    )
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "add_tracks",
        lambda yt, pid, video_ids: pushed.setdefault(pid, []).extend(video_ids),
    )
    # The KeyError is retried a couple of times (see _get_playlist_tracks_with_retry, deliberately
    # short backoff) before the stale-id recovery kicks in — not worth mocking away, and patching
    # the global time.sleep here would also stall Streamlit AppTest's own internal script-run
    # polling, which relies on it.
    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert not at.error
    assert created == ["Discogs - My Favorites"]
    assert pushed == {"fresh-id": ["vid1"]}
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist["ytmusic_playlist_id"] == "fresh-id"


def test_sync_retries_a_transient_keyerror_on_playlist_read_then_succeeds(isolated_cache, dummy_library, monkeypatch):
    """A freshly created playlist can be briefly un-queryable (see
    _get_playlist_tracks_with_retry) — a KeyError that clears up within a couple of retries
    should not fail the sync at all."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    calls = {"n": 0}

    def _get_playlist_tracks(yt, pid):
        calls["n"] += 1
        if calls["n"] < 2:
            raise KeyError("Unable to find 'contents' using path [...] on {...}, exception: 'contents'")
        return []

    created_names, pushed, _removed = _patch_sync_happy_path(monkeypatch, app_module)
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", _get_playlist_tracks)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert not at.error
    assert created_names == ["Discogs - My Favorites"]
    assert pushed == {"ytmusic-playlist-id": ["vid1"]}


def test_first_time_sync_bare_keyerror_does_not_flag_the_session_as_suspect(isolated_cache, dummy_library, monkeypatch):
    """A first-time sync (no saved ytmusic_playlist_id) has no stale id to blame, so a
    persistent KeyError from a not-yet-queryable new playlist just fails the sync — it must not
    be misread as an expired/rotated cookie (see issue #53)."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    def _get_playlist_tracks(yt, pid):
        raise KeyError("Unable to find 'contents' using path [...] on {...}, exception: 'contents'")

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(app_module.ytmusic_client, "find_playlist", lambda yt, name: None)
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", _get_playlist_tracks)
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": ("fresh-id", True),
    )

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert any("unexpected response" in e.value for e in at.error)
    assert not any("cookie" in e.value for e in at.error)

    at.run()
    assert "is-connected" in _sidebar_pill(at)


def test_sync_removes_remote_tracks_no_longer_present_locally(isolated_cache, dummy_library, monkeypatch):
    """A playlist already linked to YT Music (ytmusic_playlist_id saved) should have stray
    remote tracks removed on sync, with no extra confirmation step needed."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])
        store.set_playlist_ytmusic_id(conn, playlist_id, "already-linked-id")

    import discogs2ytmusic.app as app_module

    stray_track = {"videoId": "stray-vid", "setVideoId": "set-1"}
    created_names, pushed, removed = _patch_sync_happy_path(
        monkeypatch, app_module, remote_tracks=[{"videoId": "vid1", "setVideoId": "set-0"}, stray_track]
    )

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()

    # Already linked — no eager pre-existing-playlist check, so no extra warning/confirmation.
    assert not any("Found an existing" in w.value for w in at.warning)

    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert created_names == []  # never called get_or_create_playlist — already linked
    assert pushed == {"already-linked-id": []}  # vid1 already remote, nothing new to add
    assert removed == [("already-linked-id", [stray_track])]


def test_sync_warns_and_requires_a_second_confirmation_for_a_pre_existing_playlist(
    isolated_cache, dummy_library, monkeypatch
):
    """First-time sync attaching to a pre-existing YT Music playlist (same generated name) that
    already has tracks not in the local playlist must warn and require a second confirmation
    before those tracks get removed."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    stray_track = {"videoId": "stray-vid", "setVideoId": "set-1"}
    created_names, pushed, removed = _patch_sync_happy_path(
        monkeypatch,
        app_module,
        found_playlist_id="pre-existing-id",
        remote_tracks=[stray_track],
        created_playlist_id="pre-existing-id",
        created=False,
    )

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()

    assert not at.exception
    assert any("Found an existing" in w.value for w in at.warning)

    # First click only acknowledges the extra warning — nothing pushed/removed yet.
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()
    assert pushed == {}
    assert removed == []

    # Second click actually performs the sync.
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert created_names == ["Discogs - My Favorites"]  # get_or_create_playlist attaches to pre-existing-id
    assert pushed == {"pre-existing-id": ["vid1"]}
    assert removed == [("pre-existing-id", [stray_track])]


def test_failed_sync_flips_the_sidebar_pill_to_not_connected(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    from ytmusicapi.exceptions import YTMusicServerError

    import discogs2ytmusic.app as app_module

    def _blow_up(yt, playlist_id, video_ids):
        raise YTMusicServerError("Server returned HTTP 404: Not Found.")

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(app_module.ytmusic_client, "find_playlist", lambda yt, name: None)
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", lambda yt, playlist_id: [])
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": ("ytmusic-playlist-id", True),
    )
    monkeypatch.setattr(app_module.ytmusic_client, "add_tracks", _blow_up)

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert any("YT Music rejected the request" in e.value for e in at.error)

    # The sidebar already rendered earlier in that same script run, so the pill only picks up
    # the newly-set suspect flag on the *next* rerun (see _mark_ytmusic_auth_suspect).
    at.run()
    pill = _sidebar_pill(at)
    assert "is-off" in pill
    assert "Not connected" in pill

    # Trying again without reconnecting is short-circuited locally, never touching YT Music again.
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()
    assert any("Not authenticated" in e.value for e in at.error)


def test_reconnecting_after_a_failed_sync_clears_the_not_connected_pill(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    from ytmusicapi.exceptions import YTMusicServerError

    import discogs2ytmusic.app as app_module

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(app_module.ytmusic_client, "find_playlist", lambda yt, name: None)
    monkeypatch.setattr(app_module.ytmusic_client, "get_playlist_tracks", lambda yt, playlist_id: [])
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": ("ytmusic-playlist-id", True),
    )
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "add_tracks",
        lambda yt, playlist_id, video_ids: (_ for _ in ()).throw(YTMusicServerError("404")),
    )

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()
    at.run()  # let the sidebar pick up the suspect flag before reconnecting, per the note above
    assert "is-off" in _sidebar_pill(at)

    at = _open_ytmusic_page(at)
    at.text_input(key="ytmusic_auth_cookie_input").input("__Secure-3PAPISID=deadbeef; SID=fake").run()
    at.text_input(key="ytmusic_auth_authuser_input").input("0").run()
    at.button(key="ytmusic_auth_save").click().run()

    assert not at.exception
    assert "is-connected" in _sidebar_pill(at)


def test_sync_with_no_matched_tracks_shows_no_button(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)

    assert not at.exception
    assert any("No matched tracks" in c.value for c in at.main.caption)


def _sidebar_pill(at: AppTest) -> str:
    """The raw markdown source of the sidebar's YT Music status pill (a <span> with
    unsafe_allow_html, so it's read back as markdown source, not rendered HTML)."""
    matches = [m.value for m in at.sidebar.markdown if "<span" in m.value and "yt-status-pill" in m.value]
    assert len(matches) == 1, matches
    return matches[0]


def _open_ytmusic_page(at: AppTest) -> AppTest:
    """Click the sidebar's YT Music nav item, making the dedicated connection page active."""
    return at.button(key="nav_ytmusic_btn").click().run()


def test_sidebar_shows_a_not_connected_pill_by_default(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    pill = _sidebar_pill(at)
    assert "is-off" in pill
    assert "Not connected" in pill


def test_sidebar_shows_a_connected_pill_once_authenticated(isolated_cache, monkeypatch):
    import discogs2ytmusic.app as app_module

    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    pill = _sidebar_pill(at)
    assert "is-connected" in pill
    assert "Connected" in pill


def test_clicking_the_ytmusic_nav_item_opens_the_dedicated_page(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)

    assert not at.exception
    assert at.main.subheader[0].value == "YT Music"
    assert any("How to get your header values" in m.value for m in at.main.markdown)
    assert any("music.youtube.com" in m.value for m in at.main.markdown)
    assert at.main.info[0].value == "Not connected"
    assert at.text_input(key="ytmusic_auth_cookie_input")
    assert at.text_input(key="ytmusic_auth_authuser_input")
    assert at.button(key="ytmusic_auth_save")
    assert at.text_input(key="playlist_name_prefix_input").value == "Discogs -"
    assert at.button(key="playlist_name_prefix_save")


def test_ytmusic_page_form_saves_valid_headers_and_flips_status_to_connected(isolated_cache):
    # The success message itself doesn't survive the st.rerun() that follows it (a fresh
    # script run has no memory of the prior run's elements) — same as every other
    # success-then-rerun action in this app, so what's checked here is the resulting state.
    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.text_input(key="ytmusic_auth_cookie_input").input("__Secure-3PAPISID=deadbeef; SID=fake").run()
    at.text_input(key="ytmusic_auth_authuser_input").input("0").run()
    at.button(key="ytmusic_auth_save").click().run()

    assert not at.exception
    assert at.main.success[0].value == "Connected"
    assert "is-connected" in _sidebar_pill(at)


def test_ytmusic_page_form_shows_error_when_values_missing(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.button(key="ytmusic_auth_save").click().run()

    assert not at.exception
    assert any("required" in e.value for e in at.error)
    assert "is-off" in _sidebar_pill(at)


def test_ytmusic_page_form_shows_error_when_ytmusicapi_rejects_headers(isolated_cache, monkeypatch):
    import discogs2ytmusic.app as app_module

    def _reject(cookie: str, authuser: str) -> None:
        raise app_module.ytmusic_client.YTMusicAuthError("nope")

    monkeypatch.setattr(app_module.ytmusic_client, "save_auth_headers", _reject)

    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.text_input(key="ytmusic_auth_cookie_input").input("x").run()
    at.text_input(key="ytmusic_auth_authuser_input").input("0").run()
    at.button(key="ytmusic_auth_save").click().run()

    assert not at.exception
    assert any("Could not authenticate: nope" in e.value for e in at.error)


def test_ytmusic_page_saves_a_custom_playlist_name_prefix(isolated_cache):
    from discogs2ytmusic.config import Config

    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.text_input(key="playlist_name_prefix_input").input("My Vinyl").run()
    at.button(key="playlist_name_prefix_save").click().run()

    assert not at.exception
    assert Config.load().playlist_name_prefix == "My Vinyl"


def test_reset_button_requires_confirmation(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.button(key="reset_button").click().run()

    assert not at.exception
    assert any("This will permanently delete" in w.value for w in at.warning)
    with store.connect() as conn:
        assert store.count_matches(conn) == 1  # untouched until confirmed
        assert len(list(store.iter_releases_with_tracks(conn))) == len(dummy_library)


def test_cancelling_reset_leaves_cache_untouched(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.button(key="reset_button").click().run()
    at.button(key="confirm_reset_no").click().run()

    assert not at.exception
    with store.connect() as conn:
        assert store.count_matches(conn) == 1
        assert len(list(store.iter_releases_with_tracks(conn))) == len(dummy_library)


def test_confirming_reset_wipes_the_cache(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        store.add_other_source(conn, "label", "123", "Some Label")

    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.button(key="reset_button").click().run()
    at.button(key="confirm_reset_yes").click().run()

    assert not at.exception
    with store.connect() as conn:
        assert store.count_matches(conn) == 0
        assert list(store.iter_releases_with_tracks(conn)) == []
        assert store.list_other_sources(conn) == []


def test_reset_preserves_saved_credentials(isolated_cache, dummy_library):
    from discogs2ytmusic.config import Config

    Config(discogs_token="tok", discogs_username="user", playlist_name_prefix="My Vinyl").save()
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at = _open_ytmusic_page(at)
    at.button(key="reset_button").click().run()
    at.button(key="confirm_reset_yes").click().run()

    assert not at.exception
    loaded = Config.load()
    assert loaded.discogs_token == "tok"
    assert loaded.discogs_username == "user"
    assert loaded.playlist_name_prefix == "My Vinyl"


# --- Other sources (issue #13) ---


def test_collapsing_other_sources_hides_its_entries(isolated_cache):
    with store.connect() as conn:
        source_id = store.add_other_source(conn, "label", "123", "Some Label")

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_other_sources_toggle").click().run()

    assert not at.exception
    assert not any(b.key == f"nav_othersrc_{source_id}" for b in at.button)
    assert not any(b.key == "nav_add_source_page" for b in at.button)


def test_add_source_page_links_to_discogs(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()

    assert not at.exception
    assert any("discogs.com" in m.value for m in at.main.markdown)


def test_add_source_year_field_is_a_range_slider(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/label/999-Some-Label").run()

    assert not at.exception
    current_year = datetime.date.today().year
    assert at.slider(key="add_source_year_range").value == (1960, current_year)


def test_add_source_year_prefilter_only_imports_releases_in_range(
    isolated_cache, fake_discogs_client, dummy_library, monkeypatch
):
    _mock_discogs_client(monkeypatch, fake_discogs_client)
    expected_ids = {r["release_id"] for r in dummy_library if 2020 <= r["year"] <= 2025}
    assert expected_ids
    assert len(expected_ids) < len(dummy_library)  # sanity: the range actually narrows something

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/label/999-Some-Label").run()
    at.slider(key="add_source_year_range").set_range(2020, 2025).run()
    at.button(key="add_source_submit").click().run()

    assert not at.exception
    at.button(key="scan_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        imported_ids = {r["release_id"] for r, _t in store.iter_releases_with_tracks(conn, "label", "999")}
    assert imported_ids == expected_ids


def test_add_source_style_prefilter_only_imports_matching_releases(
    isolated_cache, fake_discogs_client, dummy_library, monkeypatch
):
    """Point 3: pre-filtering a source by Style before it's ever scanned should mean a
    non-matching release never gets imported under that source at all."""
    _mock_discogs_client(monkeypatch, fake_discogs_client)
    expected_ids = {r["release_id"] for r in dummy_library if "Acid" in r["styles"]}
    assert expected_ids  # sanity: the fixture actually has at least one Acid release

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/label/999-Some-Label").run()
    at.multiselect(key="add_source_styles").select("Acid").run()
    at.button(key="add_source_submit").click().run()

    assert not at.exception
    at.button(key="scan_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        imported_ids = {r["release_id"] for r, _t in store.iter_releases_with_tracks(conn, "label", "999")}
    assert imported_ids == expected_ids


def test_add_source_format_field_shows_empty_state_before_anything_is_scanned(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/label/999-Some-Label").run()

    assert not at.exception
    assert at.multiselect(key="add_source_formats").options == []
    assert any("No formats seen yet" in c.value for c in at.main.caption)


def test_add_source_format_field_is_populated_from_previously_scanned_releases(isolated_cache):
    with store.connect() as conn:
        store.record_known_formats(conn, ["Vinyl", '12"', "Album"])

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/label/999-Some-Label").run()

    assert not at.exception
    assert at.multiselect(key="add_source_formats").options == ['12"', "Album", "Vinyl"]


def test_other_sources_section_renders_added_sources_and_navigates_to_them(isolated_cache):
    with store.connect() as conn:
        source_id = store.add_other_source(conn, "label", "123", "Some Label")

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert at.button(key=f"nav_othersrc_{source_id}")

    at = _open_other_source(at, source_id)

    assert not at.exception
    assert any("Some Label" in h.value for h in at.main.header)


def test_other_source_page_links_back_to_the_discogs_page_it_was_imported_from(isolated_cache):
    with store.connect() as conn:
        label_id = store.add_other_source(conn, "label", "123", "Some Label")
        wantlist_id = store.add_other_source(conn, "wantlist", "alice", "alice's wantlist")

    at = AppTest.from_file(APP_PATH).run()

    at_label = _open_other_source(at, label_id)
    assert not at_label.exception
    assert any("discogs.com/label/123" in m.value for m in at_label.main.markdown)

    at_wantlist = _open_other_source(AppTest.from_file(APP_PATH).run(), wantlist_id)
    assert not at_wantlist.exception
    assert any("discogs.com/user/alice/wantlist" in m.value for m in at_wantlist.main.markdown)


def test_adding_a_source_via_a_pasted_label_url_creates_and_opens_a_page(
    isolated_cache, fake_discogs_client, monkeypatch
):
    _mock_discogs_client(monkeypatch, fake_discogs_client)

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/label/123-Some-Label").run()
    at.button(key="add_source_submit").click().run()

    assert not at.exception
    with store.connect() as conn:
        sources = store.list_other_sources(conn)
    assert len(sources) == 1
    assert sources[0]["source_type"] == "label"
    assert sources[0]["source_key"] == "123"
    assert at.session_state["nav_kind"] == "other_source"
    assert at.session_state["nav_other_source_id"] == sources[0]["id"]


def test_adding_a_source_via_a_pasted_seller_url_creates_and_opens_a_page(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/fr/seller/adamlee1995/profile").run()
    at.button(key="add_source_submit").click().run()

    assert not at.exception
    with store.connect() as conn:
        sources = store.list_other_sources(conn)
    assert len(sources) == 1
    assert sources[0]["source_type"] == "seller"
    assert sources[0]["source_key"] == "adamlee1995"
    assert at.session_state["nav_kind"] == "other_source"
    assert at.session_state["nav_other_source_id"] == sources[0]["id"]


def test_adding_a_source_via_the_my_wantlist_url_resolves_to_the_authenticated_username(
    isolated_cache, fake_discogs_client, monkeypatch
):
    """`/mywantlist` has no username in it — it must resolve to the locally authenticated
    Discogs username instead of being rejected as unrecognized."""
    _mock_discogs_client(monkeypatch, fake_discogs_client, username="dummyuser")

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/fr/mywantlist").run()
    at.button(key="add_source_submit").click().run()

    assert not at.exception
    with store.connect() as conn:
        sources = store.list_other_sources(conn)
    assert len(sources) == 1
    assert sources[0]["source_type"] == "wantlist"
    assert sources[0]["source_key"] == "dummyuser"
    assert at.session_state["nav_kind"] == "other_source"
    assert at.session_state["nav_other_source_id"] == sources[0]["id"]


def test_adding_a_source_via_the_my_wantlist_url_without_credentials_shows_an_error(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/mywantlist").run()

    assert not at.exception
    assert any("Not authenticated with Discogs" in e.value for e in at.error)
    with store.connect() as conn:
        assert store.list_other_sources(conn) == []


def test_adding_a_source_with_an_unrecognized_url_shows_an_error_and_creates_nothing(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("not a discogs url").run()

    assert not at.exception
    assert any("Paste a Discogs" in e.value for e in at.error)
    with store.connect() as conn:
        assert store.list_other_sources(conn) == []


def test_pasting_an_already_added_source_link_warns_but_still_shows_the_filter_form(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/user/alice/wantlist").run()
    at.button(key="add_source_submit").click().run()

    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/user/alice/wantlist").run()

    assert not at.exception
    assert any("already added this source" in w.value for w in at.main.warning)
    assert any(m.key == "add_source_styles" for m in at.multiselect)  # pre-filter form is still editable
    assert at.button(key="add_source_submit").label == "Update source"
    with store.connect() as conn:
        assert len(store.list_other_sources(conn)) == 1


def test_updating_the_pre_filter_on_an_already_added_source_replaces_it_without_duplicating(isolated_cache):
    """Point 2: pasting a link that's already registered must still let its pre-filter be
    changed — confirming replaces the existing page's filter in place, it doesn't create
    a second page for the same source."""
    with store.connect() as conn:
        source_id = store.add_other_source(conn, "wantlist", "alice", "alice's wantlist", {"styles": ["House"]})

    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/user/alice/wantlist").run()

    assert at.multiselect(key="add_source_styles").value == ["House"]  # prefilled from the existing filter

    at.multiselect(key="add_source_styles").select("Techno").run()
    at.button(key="add_source_submit").click().run()

    assert not at.exception
    assert at.session_state["nav_kind"] == "other_source"
    assert at.session_state["nav_other_source_id"] == source_id
    with store.connect() as conn:
        sources = store.list_other_sources(conn)
        assert len(sources) == 1
        assert json.loads(sources[0]["filter_json"])["styles"] == ["House", "Techno"]


def test_confirming_the_existing_source_warning_navigates_without_creating_a_duplicate(isolated_cache):
    at = AppTest.from_file(APP_PATH).run()
    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/user/alice/wantlist").run()
    at.button(key="add_source_submit").click().run()
    with store.connect() as conn:
        source_id = store.get_other_source_by_key(conn, "wantlist", "alice")["id"]

    at.button(key="nav_add_source_page").click().run()
    at.text_input(key="add_source_url").input("https://www.discogs.com/user/alice/wantlist").run()
    at.button(key="add_source_go_to_existing").click().run()

    assert not at.exception
    assert at.session_state["nav_kind"] == "other_source"
    assert at.session_state["nav_other_source_id"] == source_id
    with store.connect() as conn:
        assert len(store.list_other_sources(conn)) == 1


def test_collection_tab_never_shows_a_different_sources_releases(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.upsert_release(
            conn, 999999, "Label Artist", "Label Only Title", [], [], source_type="label", source_key="123"
        )
        store.replace_tracks(conn, 999999, [("A1", "Label Only Track", None, None)])

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    df = _collection_table_df(at)
    assert "Label Only Track" not in set(df["track_title"])


def test_other_source_page_scan_populates_only_that_sources_tracks(isolated_cache, fake_discogs_client, monkeypatch):
    _mock_discogs_client(monkeypatch, fake_discogs_client)
    with store.connect() as conn:
        source_id = store.add_other_source(conn, "label", "123", "Some Label")

    at = AppTest.from_file(APP_PATH).run()
    at = _open_other_source(at, source_id)
    at.button(key="scan_button").click().run()

    assert not at.exception
    with store.connect() as conn:
        collection_releases = list(store.iter_releases_with_tracks(conn))
        label_releases = list(store.iter_releases_with_tracks(conn, source_type="label", source_key="123"))
    assert collection_releases == []
    assert len(label_releases) == len(fake_discogs_client._releases)


def test_playlist_created_from_an_other_source_page_is_never_offered_on_the_collection_tab(
    isolated_cache, dummy_library
):
    with store.connect() as conn:
        _seed(conn, dummy_library)  # gives the Collection tab some tracks too
        store.add_other_source(conn, "label", "123", "Some Label")
        store.upsert_release(conn, 1, "Label Artist", "Label Title", [], [], source_type="label", source_key="123")
        store.replace_tracks(conn, 1, [("A1", "Label Track", None, None)])
        source = store.get_other_source_by_key(conn, "label", "123")
    source_id = source["id"]

    at = AppTest.from_file(APP_PATH).run()
    at = _open_other_source(at, source_id)
    table_key = _dataframe_key_by_prefix(at, f"othersrc_{source_id}_table_")
    at = _select_table_rows(at, table_key, [0])

    at.selectbox(key=f"othersrc_{source_id}_add_target").select("+ Create new playlist")
    at.text_input(key=f"othersrc_{source_id}_new_playlist_name").input("Label Picks")
    at.button(key=f"othersrc_{source_id}_add_button").click()
    at.session_state[table_key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
    at.run()

    assert not at.exception
    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Label Picks")
    assert playlist["source_type"] == "label"
    assert playlist["source_key"] == "123"

    at = at.button(key="nav_collection").click().run()
    collection_table_key = _collection_table_key(at)
    at = _select_table_rows(at, collection_table_key, [0])

    assert "Label Picks" not in at.selectbox(key="collection_add_target").options


def test_playlist_survives_switching_the_active_source(isolated_cache, dummy_library):
    """Issue #13's own regression requirement: a playlist built from one source keeps
    resolving correctly regardless of which source page is currently being viewed."""
    with store.connect() as conn:
        _seed(conn, dummy_library)  # source A: "my own collection"
        store.add_other_source(conn, "label", "123", "Some Label")
        store.upsert_release(conn, 999999, "Label Artist", "Label Title", [], [], source_type="label", source_key="123")
        store.replace_tracks(conn, 999999, [("A1", "Label Track", None, None)])

        track_id = conn.execute(
            "SELECT id FROM tracks WHERE release_id = ?", (dummy_library[0]["release_id"],)
        ).fetchone()[0]
        playlist_id = store.create_playlist(conn, "From Collection")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    with store.connect() as conn:
        source_b = store.get_other_source_by_key(conn, "label", "123")

    at = AppTest.from_file(APP_PATH).run()
    at = _open_other_source(at, source_b["id"])  # switch the active view to source B
    at = _select_playlist(at, playlist_id)  # then open the playlist built from source A

    assert not at.exception
    with store.connect() as conn:
        rows = filters.resolve_playlist_rows(conn, playlist_id)
    assert len(rows) == 1
    assert rows[0].track_id == track_id


def test_removing_an_other_source_shows_a_confirmation_then_navigates_back_to_collection(isolated_cache):
    with store.connect() as conn:
        source_id = store.add_other_source(conn, "label", "123", "Some Label")
        store.upsert_release(conn, 1, "Label Artist", "Label Title", [], [], source_type="label", source_key="123")

    at = AppTest.from_file(APP_PATH).run()
    at = _open_other_source(at, source_id)
    at.button(key=f"remove_othersrc_{source_id}").click().run()

    assert not at.exception
    assert any("removes the page" in w.value for w in at.main.warning)
    with store.connect() as conn:
        assert store.get_other_source(conn, source_id) is not None  # not deleted yet — only warned

    at.button(key=f"confirm_remove_othersrc_yes_{source_id}").click().run()

    assert not at.exception
    assert at.session_state["nav_kind"] == "collection"
    with store.connect() as conn:
        assert store.get_other_source(conn, source_id) is None
        assert store.get_release(conn, 1) is not None  # cached release itself is untouched

    # Regression test for issue #78: removing a source untags its release, leaving it with
    # zero release_sources rows — landing on the Collection tab right after (as this flow
    # does) must not have _migrate_release_sources_backfill mistake that for a pre-existing
    # cache and silently re-tag the release "collection".
    assert any("No collection cached yet" in i.value for i in at.info)
