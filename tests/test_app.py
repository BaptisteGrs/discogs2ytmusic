from __future__ import annotations

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from discogs2ytmusic import filters, store

APP_PATH = str(Path(__file__).resolve().parents[1] / "src" / "discogs2ytmusic" / "app.py")


def _select_playlist(at: AppTest, playlist_id: int) -> AppTest:
    """Click the sidebar nav button for `playlist_id`, making it the active main-pane view."""
    return at.button(key=f"nav_playlist_{playlist_id}").click().run()


def _collection_editor_df(at: AppTest) -> pd.DataFrame:
    """The Collection tab's `st.data_editor` value.

    Its widget key is derived from the currently visible row set (see #19), so tests
    look it up by element type rather than assuming a static key.
    """
    return at.main.dataframe[0].value


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
    assert at.main.caption[0].value == f"{total_tracks} tracks (0 matched)"


def test_app_tag_filter_narrows_the_table(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.multiselect(key="collection_tag_group_0").select("Acid").run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "Acid" in r["styles"])
    assert at.main.caption[0].value == f"{expected} tracks (0 matched)"


def test_app_search_box_narrows_the_table_by_release_title(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    yoyaku_release = next(r for r in dummy_library if r["title"] == "Yoyaku Barcelona 2025")
    at = AppTest.from_file(APP_PATH).run()
    at.text_input(key="collection_search").input("yoyaku").run()

    assert not at.exception
    assert at.main.caption[0].value == f"{len(yoyaku_release['tracklist'])} tracks (0 matched)"


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
    assert at.main.caption[0].value == f"{len(expected)} tracks (0 matched)"
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
    assert at.main.caption[0].value == "0 tracks (0 matched)"
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
    assert at.main.caption[0].value == f"{expected} tracks (0 matched)"


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
    assert at.main.caption[0].value == f"{expected} tracks (0 matched)"


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
    assert at.main.caption[0].value == "0 tracks (0 matched)"


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
    assert at.main.caption[0].value == f"{expected} tracks (0 matched)"
    assert not any(w.key == "collection_tag_group_1" for w in at.multiselect)


def test_app_a_single_style_group_has_no_remove_button_or_combinator(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()

    assert not at.exception
    assert not any(b.key == "collection_tag_group_remove_0" for b in at.button)
    assert not any(r.key == "collection_tag_groups_mode" for r in at.radio)


def test_collection_editor_key_changes_with_the_filtered_row_set(isolated_cache, dummy_library):
    """`_collection_editor_key` (app.py) is what makes #19's crash impossible: it derives
    the `data_editor` widget key from the row set's track_ids, so pending edit state
    (matched by row position) can never be reconciled against a differently-filtered,
    differently-shaped dataframe.
    """
    import discogs2ytmusic.app as app_module
    from discogs2ytmusic.filters import PlaylistFilter, TagGroup, resolve_rows

    with store.connect() as conn:
        _seed(conn, dummy_library)
        all_rows = resolve_rows(conn)
        acid_rows = resolve_rows(conn, PlaylistFilter(tag_groups=[TagGroup(tags=["Acid"])]))
        all_rows_again = resolve_rows(conn)

    assert 0 < len(acid_rows) < len(all_rows)
    assert app_module._collection_editor_key(all_rows) != app_module._collection_editor_key(acid_rows)
    # Same row set, recomputed independently -> same key, so unrelated reruns (e.g. a
    # widget elsewhere on the page changing) don't needlessly reset pending edits.
    assert app_module._collection_editor_key(all_rows) == app_module._collection_editor_key(all_rows_again)


def test_collection_editor_widget_key_is_unique_per_filter_combination(isolated_cache, dummy_library):
    """Regression test for #19 ("selecting a label after narrowing by year+subgenre throws
    an error"): reproduce the narrowing sequence from the bug report and confirm each step
    renders `st.data_editor` under a distinct widget key. Streamlit matches a data_editor's
    pending edits (including our "select" checkbox column) to the previous render by row
    position, not row identity, so reusing one static key across these differently-filtered
    row sets is what let a stale edit be misapplied or throw against a now-out-of-range
    position; distinct keys per row set rule that out.
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
    assert at.main.caption[0].value == "1 tracks (1 matched)"


def test_app_shows_position_in_its_own_column_and_keeps_track_title_clean(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    df = _collection_editor_df(at)

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
    assert at.main.caption[0].value == "1 tracks (0 matched)"


def test_app_shows_match_confidence(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        fuzzy, manual = dummy_library[0], dummy_library[1]
        store.save_match(conn, fuzzy["artist"], fuzzy["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 87.0)
        store.save_match(conn, manual["artist"], manual["tracklist"][0]["title"], "vid2", "Video", "manual", None)

    at = AppTest.from_file(APP_PATH).run()
    df = _collection_editor_df(at).set_index("track_artist")

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
    df = _collection_editor_df(at).set_index("track_artist")

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
    df = _collection_editor_df(at)
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
    df = _collection_editor_df(at).set_index("track_artist")

    assert not at.exception
    assert bool(df.loc[first["artist"], "locked"]) is True


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
    search_df = at.get_by_key(f"playlist_search_editor_{playlist_id}").value
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
    search_df = at.get_by_key(f"playlist_search_editor_{playlist_id}").value
    assert (search_df["track_artist"] == second["artist"]).any()


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


def test_confirming_sync_uses_the_configured_playlist_name_prefix(isolated_cache, dummy_library, monkeypatch):
    from discogs2ytmusic.config import Config

    Config(playlist_name_prefix="My Vinyl").save()

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
    # The KeyError is retried a few times (see _get_playlist_tracks_with_retry) before the
    # stale-id recovery kicks in — skip the real sleeps so the test isn't slow.
    monkeypatch.setattr(app_module.time, "sleep", lambda seconds: None)

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
    monkeypatch.setattr(app_module.time, "sleep", lambda seconds: None)

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
    monkeypatch.setattr(app_module.time, "sleep", lambda seconds: None)

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
    assert at.text_input(key="playlist_name_prefix_input").value == "Discogs"
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
