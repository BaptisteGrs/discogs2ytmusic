from __future__ import annotations

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from discogs2ytmusic import store

APP_PATH = str(Path(__file__).resolve().parents[1] / "src" / "discogs2ytmusic" / "app.py")


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(
            conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"],
            year=r.get("year"), labels=r.get("labels", []),
        )
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"]) for t in r["tracklist"]],
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
    assert at.tabs[0].caption[0].value == f"{total_tracks} tracks (0 matched)"


def test_app_tag_filter_narrows_the_table(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    at = AppTest.from_file(APP_PATH).run()
    at.multiselect(key="collection_tags").select("Acid").run()

    assert not at.exception
    expected = sum(len(r["tracklist"]) for r in dummy_library if "Acid" in r["styles"])
    assert at.tabs[0].caption[0].value == f"{expected} tracks (0 matched)"


def test_app_matched_only_checkbox_narrows_the_table(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    at = AppTest.from_file(APP_PATH).run()
    at.checkbox(key="collection_matched_only").check().run()

    assert not at.exception
    assert at.tabs[0].caption[0].value == "1 tracks (1 matched)"


def test_app_shows_match_confidence(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        fuzzy, manual = dummy_library[0], dummy_library[1]
        store.save_match(conn, fuzzy["artist"], fuzzy["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 87.0)
        store.save_match(conn, manual["artist"], manual["tracklist"][0]["title"], "vid2", "Video", "manual", None)

    at = AppTest.from_file(APP_PATH).run()
    df = at.get_by_key("collection_editor").value.set_index("artist")

    assert not at.exception
    assert df.loc[fuzzy["artist"], "confidence"] == 87.0
    assert pd.isna(df.loc[manual["artist"], "confidence"])
