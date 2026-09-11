from __future__ import annotations

import json

import pandas as pd

from discogs2ytmusic import collection_edits, store


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


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --- apply_artist_edits ---


def test_apply_artist_edits_sets_track_override(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    original = _df([{"track_id": track_id, "release_id": release["release_id"], "track_artist": release["artist"]}])
    edited = _df([{"track_id": track_id, "release_id": release["release_id"], "track_artist": "Corrected Artist"}])

    with store.connect() as conn:
        count = collection_edits.apply_artist_edits(conn, original, edited)

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
    assert count == 1
    assert track["search_artist"] == "Corrected Artist"


def test_apply_artist_edits_clearing_reverts_to_release_artist(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]
        store.set_track_search_artist(conn, track_id, "Temporary Override")

    original = _df([{"track_id": track_id, "release_id": release["release_id"], "track_artist": "Temporary Override"}])
    edited = _df([{"track_id": track_id, "release_id": release["release_id"], "track_artist": "  "}])

    with store.connect() as conn:
        collection_edits.apply_artist_edits(conn, original, edited)

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
    assert track["search_artist"] is None


def test_apply_artist_edits_falls_back_to_release_override_with_no_track_id(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)

    original = _df([{"track_id": None, "release_id": release["release_id"], "track_artist": release["artist"]}])
    edited = _df([{"track_id": None, "release_id": release["release_id"], "track_artist": "Corrected Release Artist"}])

    with store.connect() as conn:
        count = collection_edits.apply_artist_edits(conn, original, edited)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT artist_override FROM releases WHERE release_id = ?", (release["release_id"],)
        ).fetchone()
    assert count == 1
    assert row[0] == "Corrected Release Artist"


def test_apply_artist_edits_unchanged_rows_are_noop(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    df = _df([{"track_id": track_id, "release_id": release["release_id"], "track_artist": release["artist"]}])

    with store.connect() as conn:
        count = collection_edits.apply_artist_edits(conn, df, df.copy())

    assert count == 0


# --- apply_style_edits / apply_genre_edits ---


def test_apply_style_edits_sets_track_override(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    original = _df(
        [{"track_id": track_id, "release_id": release["release_id"], "styles": ", ".join(release["styles"])}]
    )
    edited = _df(
        [{"track_id": track_id, "release_id": release["release_id"], "styles": "Corrected Style, Another Style"}]
    )

    with store.connect() as conn:
        count = collection_edits.apply_style_edits(conn, original, edited)
        track = store.get_track(conn, track_id)

    assert count == 1
    assert json.loads(track["styles_override"]) == ["Corrected Style", "Another Style"]


def test_apply_style_edits_clearing_reverts_to_release_styles(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]
        store.set_track_styles_override(conn, track_id, ["Temporary Style"])

    original = _df([{"track_id": track_id, "release_id": release["release_id"], "styles": "Temporary Style"}])
    edited = _df([{"track_id": track_id, "release_id": release["release_id"], "styles": "  "}])

    with store.connect() as conn:
        collection_edits.apply_style_edits(conn, original, edited)
        track = store.get_track(conn, track_id)

    assert track["styles_override"] is None


def test_apply_style_edits_falls_back_to_release_override_with_no_track_id(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)

    original = _df([{"track_id": None, "release_id": release["release_id"], "styles": ", ".join(release["styles"])}])
    edited = _df([{"track_id": None, "release_id": release["release_id"], "styles": "Corrected Style, Another Style"}])

    with store.connect() as conn:
        count = collection_edits.apply_style_edits(conn, original, edited)
        row = conn.execute(
            "SELECT styles_override FROM releases WHERE release_id = ?", (release["release_id"],)
        ).fetchone()

    assert count == 1
    assert json.loads(row[0]) == ["Corrected Style", "Another Style"]


def test_apply_style_edits_unchanged_rows_are_noop(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    df = _df([{"track_id": track_id, "release_id": release["release_id"], "styles": ", ".join(release["styles"])}])

    with store.connect() as conn:
        count = collection_edits.apply_style_edits(conn, df, df.copy())

    assert count == 0


def test_apply_genre_edits_sets_track_override(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    original = _df(
        [{"track_id": track_id, "release_id": release["release_id"], "genres": ", ".join(release["genres"])}]
    )
    edited = _df([{"track_id": track_id, "release_id": release["release_id"], "genres": "Corrected Genre"}])

    with store.connect() as conn:
        count = collection_edits.apply_genre_edits(conn, original, edited)
        track = store.get_track(conn, track_id)

    assert count == 1
    assert json.loads(track["genres_override"]) == ["Corrected Genre"]


def test_apply_genre_edits_clearing_reverts_to_release_genres(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]
        store.set_track_genres_override(conn, track_id, ["Temporary Genre"])

    original = _df([{"track_id": track_id, "release_id": release["release_id"], "genres": "Temporary Genre"}])
    edited = _df([{"track_id": track_id, "release_id": release["release_id"], "genres": ""}])

    with store.connect() as conn:
        collection_edits.apply_genre_edits(conn, original, edited)
        track = store.get_track(conn, track_id)

    assert track["genres_override"] is None


def test_apply_genre_edits_falls_back_to_release_override_with_no_track_id(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)

    original = _df([{"track_id": None, "release_id": release["release_id"], "genres": ", ".join(release["genres"])}])
    edited = _df([{"track_id": None, "release_id": release["release_id"], "genres": "Corrected Genre"}])

    with store.connect() as conn:
        count = collection_edits.apply_genre_edits(conn, original, edited)
        row = conn.execute(
            "SELECT genres_override FROM releases WHERE release_id = ?", (release["release_id"],)
        ).fetchone()

    assert count == 1
    assert json.loads(row[0]) == ["Corrected Genre"]


# --- apply_video_link_edits ---


def test_apply_video_link_edits_creates_new_match(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    original = _df([{"match_id": None, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": ""}])
    edited = _df(
        [
            {
                "match_id": None,
                "track_artist": "Some Artist",
                "track_title": "Some Title",
                "youtube_url": "https://music.youtube.com/watch?v=abc123XYZ",
            }
        ]
    )

    with store.connect() as conn:
        count, errors = collection_edits.apply_video_link_edits(conn, original, edited)
        match = store.get_match(conn, "Some Artist", "Some Title")

    assert count == 1
    assert errors == []
    assert match["video_id"] == "abc123XYZ"
    assert match["source"] == "manual"


def test_apply_video_link_edits_uses_edited_artist_for_a_simultaneous_correction(isolated_cache, dummy_library):
    """A row where both artist and youtube_url are edited in the same pass must save the
    new match under the *new* artist, not the stale one — otherwise a future sync (which
    resolves the artist via the now-updated override) would never find this match."""
    with store.connect() as conn:
        _seed(conn, dummy_library)

    original = _df([{"match_id": None, "track_artist": "Old Artist", "track_title": "Some Title", "youtube_url": ""}])
    edited = _df(
        [{"match_id": None, "track_artist": "New Artist", "track_title": "Some Title", "youtube_url": "abc123XYZ"}]
    )

    with store.connect() as conn:
        collection_edits.apply_video_link_edits(conn, original, edited)
        assert store.get_match(conn, "New Artist", "Some Title") is not None
        assert store.get_match(conn, "Old Artist", "Some Title") is None


def test_apply_video_link_edits_updates_existing_match(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, "Some Artist", "Some Title", "wrong-id", "Wrong", "ytmusic", 70.0)
        match_id = store.get_match(conn, "Some Artist", "Some Title")["id"]

    old_url = "https://music.youtube.com/watch?v=wrong-id"
    original = _df(
        [{"match_id": match_id, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": old_url}]
    )
    edited = _df(
        [{"match_id": match_id, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": "right-id"}]
    )

    with store.connect() as conn:
        count, errors = collection_edits.apply_video_link_edits(conn, original, edited)
        match = store.get_match_by_id(conn, match_id)

    assert count == 1
    assert errors == []
    assert match["video_id"] == "right-id"
    assert match["source"] == "manual"


def test_apply_video_link_edits_clearing_rejects_existing_match(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, "Some Artist", "Some Title", "some-id", "Some", "ytmusic", 70.0)
        match_id = store.get_match(conn, "Some Artist", "Some Title")["id"]

    old_url = "https://music.youtube.com/watch?v=some-id"
    original = _df(
        [{"match_id": match_id, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": old_url}]
    )
    edited = _df(
        [{"match_id": match_id, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": ""}]
    )

    with store.connect() as conn:
        count, errors = collection_edits.apply_video_link_edits(conn, original, edited)
        match = store.get_match_by_id(conn, match_id)

    assert count == 1
    assert errors == []
    assert match["video_id"] is None
    assert match["source"] == "manual"


def test_apply_video_link_edits_invalid_url_reports_error_and_skips(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    original = _df([{"match_id": None, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": ""}])
    edited = _df(
        [
            {
                "match_id": None,
                "track_artist": "Some Artist",
                "track_title": "Some Title",
                "youtube_url": "https://example.com/no-video-id-here",
            }
        ]
    )

    with store.connect() as conn:
        count, errors = collection_edits.apply_video_link_edits(conn, original, edited)
        match = store.get_match(conn, "Some Artist", "Some Title")

    assert count == 0
    assert len(errors) == 1
    assert match is None


def test_str_treats_nan_as_empty_not_the_literal_text_nan():
    """Regression test for the bug behind #64's cookie-storage-branch follow-up: a data-editor
    cell can come back as pandas NaN even in a string column, and `str(nan)` silently produces
    the text "nan" — which then reads as real (garbage) user input downstream."""
    assert collection_edits._str(float("nan")) == ""
    assert collection_edits._str("real value") == "real value"


def test_apply_video_link_edits_nan_cell_is_treated_as_clearing_not_a_literal_video_id(isolated_cache, dummy_library):
    """A cleared youtube_url cell can arrive as pandas NaN rather than "" — this must be
    treated the same as an explicit clear, never saved as the literal video id "nan" (which
    would only fail later, confusingly, when YT Music rejects a push containing it)."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, "Some Artist", "Some Title", "some-id", "Some", "ytmusic", 70.0)
        match_id = store.get_match(conn, "Some Artist", "Some Title")["id"]

    old_url = "https://music.youtube.com/watch?v=some-id"
    original = _df(
        [{"match_id": match_id, "track_artist": "Some Artist", "track_title": "Some Title", "youtube_url": old_url}]
    )
    edited = _df(
        [
            {
                "match_id": match_id,
                "track_artist": "Some Artist",
                "track_title": "Some Title",
                "youtube_url": float("nan"),
            }
        ]
    )

    with store.connect() as conn:
        count, errors = collection_edits.apply_video_link_edits(conn, original, edited)
        match = store.get_match_by_id(conn, match_id)

    assert count == 1
    assert errors == []
    assert match["video_id"] is None
    assert match["video_id"] != "nan"
