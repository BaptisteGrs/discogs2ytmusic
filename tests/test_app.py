from __future__ import annotations

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from discogs2ytmusic import store

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
    at.multiselect(key="collection_tags").select("Acid").run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "Acid" in r["styles"])
    assert at.main.caption[0].value == f"{expected} tracks (0 matched)"


def test_collection_editor_key_changes_with_the_filtered_row_set(isolated_cache, dummy_library):
    """`_collection_editor_key` (app.py) is what makes #19's crash impossible: it derives
    the `data_editor` widget key from the row set's track_ids, so pending edit state
    (matched by row position) can never be reconciled against a differently-filtered,
    differently-shaped dataframe.
    """
    import discogs2ytmusic.app as app_module
    from discogs2ytmusic.filters import PlaylistFilter, resolve_rows

    with store.connect() as conn:
        _seed(conn, dummy_library)
        all_rows = resolve_rows(conn)
        acid_rows = resolve_rows(conn, PlaylistFilter(tags=["Acid"]))
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

    at.multiselect(key="collection_tags").select("Acid").run()
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


def test_confirming_sync_pushes_matched_tracks_to_ytmusic(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Favorites")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    import discogs2ytmusic.app as app_module

    created = []
    pushed = {}
    monkeypatch.setattr(app_module.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(app_module.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": created.append(name) or "ytmusic-playlist-id",
    )
    monkeypatch.setattr(
        app_module.ytmusic_client,
        "add_tracks",
        lambda yt, playlist_id, video_ids: pushed.setdefault(playlist_id, []).extend(video_ids),
    )

    at = AppTest.from_file(APP_PATH).run()
    at = _select_playlist(at, playlist_id)
    at.button(key=f"sync_button_{playlist_id}").click().run()
    at.button(key=f"confirm_sync_yes_{playlist_id}").click().run()

    assert not at.exception
    assert created == ["Discogs - My Favorites"]
    assert pushed == {"ytmusic-playlist-id": ["vid1"]}
    with store.connect() as conn:
        playlist = store.get_playlist(conn, playlist_id)
    assert playlist["ytmusic_playlist_id"] == "ytmusic-playlist-id"


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
