from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict

import pytest

from discogs2ytmusic import store


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


def test_seeded_library_has_18_tracks_across_6_styles(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))

    total_tracks = sum(len(tracks) for _release, tracks in releases)
    styles = {s for release, _tracks in releases for s in json.loads(release["styles"])}

    assert total_tracks == 18
    assert len(styles) == 6


def test_style_breakdown_matches_fixture_counts(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))

    counts: dict[str, int] = defaultdict(int)
    for release, tracks in releases:
        for style in json.loads(release["styles"]):
            counts[style] += len(tracks)

    assert counts == {
        "House": 3,
        "Techno": 3,
        "Deep House": 5,
        "Acid": 3,
        "Breakbeat": 2,
        "Trance": 2,
    }


def test_has_tracks_reflects_seeded_state(isolated_cache, dummy_library):
    release_id = dummy_library[0]["release_id"]
    with store.connect() as conn:
        assert store.has_tracks(conn, release_id) is False
        _seed(conn, dummy_library)
        assert store.has_tracks(conn, release_id) is True


def test_match_cache_round_trip(isolated_cache, dummy_library):
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    with store.connect() as conn:
        assert store.get_match(conn, artist, title) is None
        store.save_match(conn, artist, title, "abc123", "A fake video", "ytmusic", 90.0)

    with store.connect() as conn:
        row = store.get_match(conn, artist, title)
        assert row["video_id"] == "abc123"
        assert row["source"] == "ytmusic"


def test_match_rows_get_a_stable_surrogate_id(isolated_cache, dummy_library):
    artist1, title1 = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    artist2, title2 = dummy_library[1]["artist"], dummy_library[1]["tracklist"][0]["title"]
    with store.connect() as conn:
        store.save_match(conn, artist1, title1, "vid1", "Video 1", "ytmusic", 90.0)
        store.save_match(conn, artist2, title2, "vid2", "Video 2", "ytmusic", 80.0)

    with store.connect() as conn:
        row1 = store.get_match(conn, artist1, title1)
        row2 = store.get_match(conn, artist2, title2)

    assert row1["id"] != row2["id"]

    # Re-saving (as a re-sync would for an already-cached track) keeps the same id.
    with store.connect() as conn:
        store.save_match(conn, artist1, title1, "vid1-updated", "Video 1 updated", "ytmusic", 95.0)
        row1_again = store.get_match(conn, artist1, title1)

    assert row1_again["id"] == row1["id"]
    assert row1_again["video_id"] == "vid1-updated"


def test_matches_table_migrates_from_pre_id_schema(isolated_cache, dummy_library):
    """Caches created before the surrogate `id` column existed must upgrade in place, keeping data."""
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]

    conn = sqlite3.connect(isolated_cache)
    conn.execute(
        """CREATE TABLE matches (
            query_key TEXT PRIMARY KEY,
            video_id TEXT,
            video_title TEXT,
            source TEXT,
            score REAL,
            searched_at REAL NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO matches VALUES (?, ?, ?, ?, ?, ?)",
        (store.match_key(artist, title), "legacy-vid", "Legacy video", "ytmusic", 88.0, 1700000000.0),
    )
    conn.commit()
    conn.close()

    with store.connect() as conn:
        row = store.get_match(conn, artist, title)

    assert row["video_id"] == "legacy-vid"
    assert row["id"] is not None


def test_playlist_def_round_trip(isolated_cache):
    with store.connect() as conn:
        assert store.get_playlist_def_by_name(conn, "Electro / Tech House") is None
        filter_json = json.dumps({"tags": ["Electro", "Tech House"], "year_min": 1990, "year_max": 2010})
        def_id = store.upsert_playlist_def(conn, "Electro / Tech House", filter_json)
        store.set_playlist_def_ytmusic_id(conn, def_id, "PL123")

    with store.connect() as conn:
        row = store.get_playlist_def_by_name(conn, "Electro / Tech House")
        assert row["id"] == def_id
        assert row["ytmusic_playlist_id"] == "PL123"
        assert json.loads(row["filter_json"])["tags"] == ["Electro", "Tech House"]


def test_playlists_table_migrates_into_playlist_defs(isolated_cache, dummy_library):
    """Caches created before playlist_defs existed must fold the old style->playlist_id table in."""
    conn = sqlite3.connect(isolated_cache)
    conn.execute("CREATE TABLE playlists (style TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.execute("INSERT INTO playlists VALUES (?, ?, ?)", ("House", "PL-legacy", 1700000000.0))
    conn.commit()
    conn.close()

    with store.connect() as conn:
        row = store.get_playlist_def_by_name(conn, "Discogs - House")
        legacy_cols = {r[1] for r in conn.execute("PRAGMA table_info(playlists)")}

    assert row["ytmusic_playlist_id"] == "PL-legacy"
    assert json.loads(row["filter_json"]) == {"tags": ["House"]}
    assert not legacy_cols  # old table is gone


def test_connect_ignores_playlists_table_with_unrelated_shape(isolated_cache, dummy_library):
    """A `playlists` table that isn't the old style->playlist_id shape must not crash connect().

    Regression test: a `playlists` table from some other, unrelated schema (e.g. an
    abandoned prototype) was previously assumed to always be the legacy shape, which
    crashed with "no such column: style" instead of just being left alone.
    """
    conn = sqlite3.connect(isolated_cache)
    conn.execute("CREATE TABLE playlists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, ytmusic_playlist_id TEXT)")
    conn.execute("INSERT INTO playlists (name, ytmusic_playlist_id) VALUES (?, ?)", ("Unrelated", "PL-other"))
    conn.commit()
    conn.close()

    with store.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(playlists)")}

    assert cols == {"id", "name", "ytmusic_playlist_id"}  # left untouched, not mistaken for the legacy shape


def test_release_overrides_apply_to_effective_track_queries(isolated_cache, dummy_library):
    release_id = dummy_library[0]["release_id"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.set_release_artist_override(conn, release_id, "Corrected Release Artist")

    with store.connect() as conn:
        release, tracks = next(r for r in store.iter_releases_with_tracks(conn) if r[0]["release_id"] == release_id)
        queries = store.effective_track_queries(release, tracks)

    assert all(artist == "Corrected Release Artist" for _tid, artist, _title in queries)


def test_release_styles_and_genres_override_round_trip(isolated_cache, dummy_library):
    release_id = dummy_library[0]["release_id"]
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        release = store.get_release(conn, release_id)
        assert store.effective_release_styles(release) == json.loads(release["styles"])
        assert store.effective_release_genres(release) == json.loads(release["genres"])

        store.set_release_styles_override(conn, release_id, ["Corrected Style"])
        store.set_release_genres_override(conn, release_id, ["Corrected Genre"])

    with store.connect() as conn:
        release = store.get_release(conn, release_id)
        assert store.effective_release_styles(release) == ["Corrected Style"]
        assert store.effective_release_genres(release) == ["Corrected Genre"]

        store.set_release_styles_override(conn, release_id, None)
        store.set_release_genres_override(conn, release_id, None)

    with store.connect() as conn:
        release = store.get_release(conn, release_id)
        assert store.effective_release_styles(release) == json.loads(release["styles"])
        assert store.effective_release_genres(release) == json.loads(release["genres"])


def test_upsert_release_preserves_styles_and_genres_override_across_a_rescan(isolated_cache, dummy_library):
    """Regression coverage for the CLAUDE.md invariant: a manual correction must never be
    silently clobbered by a rescan. `upsert_release` unconditionally overwrites the raw
    `styles`/`genres` columns on every call (that's Discogs-sourced data, meant to refresh),
    but must leave `styles_override`/`genres_override` alone."""
    first = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.set_release_styles_override(conn, first["release_id"], ["My Style"])
        store.set_release_genres_override(conn, first["release_id"], ["My Genre"])

    with store.connect() as conn:
        # Re-scan with fresh (different) Discogs-sourced styles/genres, as `scan --refresh` would.
        store.upsert_release(
            conn,
            first["release_id"],
            first["artist"],
            first["title"],
            ["Some New Discogs Style"],
            ["Some New Discogs Genre"],
            year=first.get("year"),
            labels=first.get("labels", []),
        )

    with store.connect() as conn:
        release = store.get_release(conn, first["release_id"])

    assert json.loads(release["styles"]) == ["Some New Discogs Style"]  # Discogs-sourced value did refresh
    assert store.effective_release_styles(release) == ["My Style"]  # override still wins
    assert store.effective_release_genres(release) == ["My Genre"]


def test_releases_table_migrates_in_styles_and_genres_override_columns(isolated_cache, dummy_library):
    """Caches created before styles_override/genres_override existed must upgrade in place,
    following the same PRAGMA table_info guard pattern as the other release override columns."""
    conn = sqlite3.connect(isolated_cache)
    conn.execute(
        """CREATE TABLE releases (
            release_id INTEGER PRIMARY KEY,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            styles TEXT NOT NULL,
            genres TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO releases (release_id, artist, title, styles, genres, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (1, "Old Artist", "Old Title", "[]", "[]", 1700000000.0),
    )
    conn.commit()
    conn.close()

    with store.connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(releases)")}
        release = store.get_release(conn, 1)

    assert {"styles_override", "genres_override"} <= cols
    assert release["styles_override"] is None
    assert release["genres_override"] is None


def test_get_release_tracks_returns_all_tracks_for_that_release_only(isolated_cache, dummy_library):
    first, second = dummy_library[0], dummy_library[1]
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        tracks = store.get_release_tracks(conn, first["release_id"])

    assert [t["title"] for t in tracks] == [t["title"] for t in first["tracklist"]]
    assert all(t["release_id"] == first["release_id"] for t in tracks)
    other_titles = {t["title"] for t in second["tracklist"]} - {t["title"] for t in first["tracklist"]}
    assert not (other_titles & {t["title"] for t in tracks})


def test_track_styles_and_genres_override_round_trip(isolated_cache, dummy_library):
    """Unlike the release-level override (used only for the no-tracklist fallback row), a
    per-track override is the normal path: Discogs only reports styles/genres per release,
    so a multi-style release needs per-track correction to say which track is which."""
    with store.connect() as conn:
        _seed(conn, dummy_library)
        release_id = dummy_library[0]["release_id"]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release_id,)).fetchone()[0]

    with store.connect() as conn:
        release = store.get_release(conn, release_id)
        track = store.get_track(conn, track_id)
        assert store.effective_track_styles(track, release) == store.effective_release_styles(release)
        assert store.effective_track_genres(track, release) == store.effective_release_genres(release)

        store.set_track_styles_override(conn, track_id, ["Track-Only Style"])
        store.set_track_genres_override(conn, track_id, ["Track-Only Genre"])

    with store.connect() as conn:
        release = store.get_release(conn, release_id)
        track = store.get_track(conn, track_id)
        assert store.effective_track_styles(track, release) == ["Track-Only Style"]
        assert store.effective_track_genres(track, release) == ["Track-Only Genre"]
        # A sibling track on the same release is unaffected — this is the whole point.
        other_track_id = conn.execute(
            "SELECT id FROM tracks WHERE release_id = ? AND id != ?", (release_id, track_id)
        ).fetchone()
        if other_track_id is not None:
            other_track = store.get_track(conn, other_track_id[0])
            assert store.effective_track_styles(other_track, release) == store.effective_release_styles(release)

        store.set_track_styles_override(conn, track_id, None)
        store.set_track_genres_override(conn, track_id, None)

    with store.connect() as conn:
        release = store.get_release(conn, release_id)
        track = store.get_track(conn, track_id)
        assert store.effective_track_styles(track, release) == store.effective_release_styles(release)
        assert store.effective_track_genres(track, release) == store.effective_release_genres(release)


def test_replace_tracks_preserves_a_manual_styles_and_genres_override_for_an_unchanged_track(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Various", "Comp EP", ["Tech House", "Breaks"], ["Electronic"])
        store.replace_tracks(conn, 1, [("A1", "Some Track", None, None)])
        track = conn.execute("SELECT id FROM tracks WHERE release_id = 1").fetchone()
        store.set_track_styles_override(conn, track[0], ["Breaks"])
        store.set_track_genres_override(conn, track[0], ["My Genre"])

        # Re-scan with the exact same tracklist (as a real `scan --refresh` would do)
        store.replace_tracks(conn, 1, [("A1", "Some Track", None, None)])

        refreshed = store.get_track(conn, track[0])

    assert refreshed["styles_override"] is not None
    assert json.loads(refreshed["styles_override"]) == ["Breaks"]
    assert json.loads(refreshed["genres_override"]) == ["My Genre"]


def test_tracks_table_migrates_in_styles_and_genres_override_columns(isolated_cache):
    """Caches created before tracks had styles_override/genres_override must upgrade in
    place, following the same PRAGMA table_info guard pattern used elsewhere."""
    conn = sqlite3.connect(isolated_cache)
    conn.execute(
        """CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id INTEGER NOT NULL,
            position TEXT NOT NULL,
            title TEXT NOT NULL,
            duration TEXT,
            search_artist TEXT,
            discogs_artist TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO tracks (release_id, position, title) VALUES (?, ?, ?)",
        (1, "A1", "Old Track"),
    )
    conn.commit()
    conn.close()

    with store.connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        conn.row_factory = sqlite3.Row
        track = conn.execute("SELECT * FROM tracks WHERE release_id = 1").fetchone()

    assert {"styles_override", "genres_override"} <= cols
    assert track["styles_override"] is None
    assert track["genres_override"] is None


def test_scan_captures_year_and_labels(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))

    by_id = {r["release_id"]: r for r, _tracks in releases}
    first = dummy_library[0]
    assert by_id[first["release_id"]]["year"] == first["year"]
    assert json.loads(by_id[first["release_id"]]["labels"]) == first["labels"]


# --- various-artists track title splitting ---


def test_split_va_track_title_splits_on_dash_when_release_has_multiple_artists():
    assert store._split_va_track_title("Aline Umber, HOSTOM", "HOSTOM - Oto") == ("HOSTOM", "Oto")


def test_split_va_track_title_requires_a_comma_in_the_release_artist():
    assert store._split_va_track_title("Solo Artist", "Some Artist - Some Track") is None


def test_split_va_track_title_requires_a_dash_in_the_track_title():
    assert store._split_va_track_title("A, B", "No dash here") is None


def test_split_va_track_title_rejects_an_empty_side():
    assert store._split_va_track_title("A, B", " - Track") is None
    assert store._split_va_track_title("A, B", "Artist - ") is None


def test_effective_track_queries_applies_the_va_split_when_unambiguous():
    release = {
        "artist": "Aline Umber, HOSTOM",
        "artist_override": None,
        "title": "Yoyaku Barcelona 2025",
        "title_override": None,
    }
    tracks = [
        {"id": 1, "search_artist": None, "discogs_artist": None, "title": "HOSTOM - Oto", "position": "A1"},
        {"id": 2, "search_artist": None, "discogs_artist": None, "title": "No Dash Here", "position": "A2"},
    ]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "HOSTOM", "Oto"), (2, "Aline Umber, HOSTOM", "No Dash Here")]


def test_effective_track_queries_manual_override_wins_over_the_va_split():
    release = {
        "artist": "Aline Umber, HOSTOM",
        "artist_override": None,
        "title": "Yoyaku Barcelona 2025",
        "title_override": None,
    }
    tracks = [
        {
            "id": 1,
            "search_artist": "Manually Corrected",
            "discogs_artist": None,
            "title": "HOSTOM - Oto",
            "position": "A1",
        }
    ]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "Manually Corrected", "HOSTOM - Oto")]


def test_effective_track_queries_does_not_split_single_artist_releases():
    release = {"artist": "Solo Artist", "artist_override": None, "title": "Some EP", "title_override": None}
    tracks = [
        {"id": 1, "search_artist": None, "discogs_artist": None, "title": "Some Artist - Some Track", "position": "A1"}
    ]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "Solo Artist", "Some Artist - Some Track")]


# --- discogs_artist: Discogs' own structured per-track credit ---


def test_effective_track_queries_prefers_discogs_artist_over_the_va_split_guess():
    """Even though the title *also* matches the dash-split pattern, a structured
    discogs_artist credit (the real fix for this release shape) must win — the
    dash-split is only a fallback for releases where Discogs gave no such credit."""
    release = {
        "artist": "Aline Umber, HOSTOM",
        "artist_override": None,
        "title": "Yoyaku Barcelona 2025",
        "title_override": None,
    }
    tracks = [{"id": 1, "search_artist": None, "discogs_artist": "HOSTOM", "title": "HOSTOM - Oto", "position": "A1"}]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "HOSTOM", "HOSTOM - Oto")]  # title is left as-is; only the artist comes from discogs_artist


def test_effective_track_queries_uses_discogs_artist_for_a_clean_title_with_no_dash():
    """The actual real-world case: Discogs' tracklist API gives a per-track artist credit
    without ever embedding it into the title text (e.g. title is just "Tree House")."""
    release = {
        "artist": "Aline Umber, HOSTOM",
        "artist_override": None,
        "title": "Yoyaku Barcelona 2025",
        "title_override": None,
    }
    tracks = [{"id": 1, "search_artist": None, "discogs_artist": "HOSTOM", "title": "Tree House", "position": "A2"}]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "HOSTOM", "Tree House")]


def test_effective_track_queries_manual_override_wins_over_discogs_artist():
    release = {
        "artist": "Aline Umber, HOSTOM",
        "artist_override": None,
        "title": "Yoyaku Barcelona 2025",
        "title_override": None,
    }
    tracks = [
        {
            "id": 1,
            "search_artist": "Manually Corrected",
            "discogs_artist": "HOSTOM",
            "title": "Tree House",
            "position": "A2",
        }
    ]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "Manually Corrected", "Tree House")]


def test_real_va_release_regression_hostom_tree_house(isolated_cache, dummy_library):
    """Regression test for the exact reported bug: on the 'Aline Umber, HOSTOM' release,
    the track 'Tree House' must resolve to artist 'HOSTOM' (from Discogs' own per-track
    credit), not the full comma-joined release artist."""
    release_fixture = next(r for r in dummy_library if r["release_id"] == 34365844)
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        release, tracks = next((r, t) for r, t in store.iter_releases_with_tracks(conn) if r["release_id"] == 34365844)
        queries = store.effective_track_queries(release, tracks)

    by_title = {title: artist for _tid, artist, title in queries}
    assert by_title["Tree House"] == "HOSTOM"
    assert by_title["Oto"] == "Aline Umber"
    assert release_fixture["artist"] == "Aline Umber, HOSTOM"  # sanity: the release-level artist stays the full credit


# --- "Untitled" tracks fall back to release title + position ---


def test_is_untitled_matches_only_the_exact_placeholder():
    assert store._is_untitled("Untitled")
    assert store._is_untitled("  untitled  ")
    assert not store._is_untitled("Untitled (How Does It Feel)")  # a real song title — must not be caught
    assert not store._is_untitled("Some Track")


def test_effective_track_queries_resolves_untitled_to_release_title_plus_position():
    release = {"artist": "Solo Artist", "artist_override": None, "title": "Some EP", "title_override": None}
    tracks = [{"id": 1, "search_artist": None, "discogs_artist": None, "title": "Untitled", "position": "A2"}]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "Solo Artist", "Some EP A2")]


def test_effective_track_queries_untitled_fallback_applies_after_discogs_artist_resolution():
    """The untitled fallback is a title-only normalization — it must not disturb whichever
    tier (override/discogs_artist/VA-split/release artist) already resolved the artist."""
    release = {
        "artist": "Aline Umber, HOSTOM",
        "artist_override": None,
        "title": "Yoyaku Barcelona 2025",
        "title_override": None,
    }
    tracks = [{"id": 1, "search_artist": None, "discogs_artist": "HOSTOM", "title": "Untitled", "position": "B3"}]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "HOSTOM", "Yoyaku Barcelona 2025 B3")]


def test_effective_track_queries_leaves_real_titles_untouched():
    release = {"artist": "Solo Artist", "artist_override": None, "title": "Some EP", "title_override": None}
    tracks = [{"id": 1, "search_artist": None, "discogs_artist": None, "title": "A Real Song", "position": "A1"}]

    result = store.effective_track_queries(release, tracks)

    assert result == [(1, "Solo Artist", "A Real Song")]


# --- replace_tracks preserves manual corrections across a rescan ---


def test_replace_tracks_preserves_a_manual_search_artist_override_for_an_unchanged_track(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Various", "Comp EP", ["House"], ["Electronic"])
        store.replace_tracks(conn, 1, [("A1", "Some Track", None, None)])
        track = conn.execute("SELECT id FROM tracks WHERE release_id = 1").fetchone()
        store.set_track_search_artist(conn, track[0], "Real Artist")

        # Re-scan with the exact same tracklist (as a real `scan --refresh` would do)
        store.replace_tracks(conn, 1, [("A1", "Some Track", None, None)])

        refreshed = store.get_track(conn, track[0])

    assert refreshed["search_artist"] == "Real Artist"


def test_replace_tracks_drops_the_override_only_for_a_track_that_actually_disappeared(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Various", "Comp EP", ["House"], ["Electronic"])
        store.replace_tracks(conn, 1, [("A1", "Track One", None, None), ("A2", "Track Two", None, None)])
        rows = {r["position"]: r["id"] for r in conn.execute("SELECT id, position FROM tracks WHERE release_id = 1")}
        store.set_track_search_artist(conn, rows["A1"], "Real Artist")
        store.set_track_search_artist(conn, rows["A2"], "Other Artist")

        # A2 renamed on Discogs (position no longer matches) — its override can't carry over,
        # but A1's must survive untouched.
        store.replace_tracks(conn, 1, [("A1", "Track One", None, None), ("A2", "Renamed Track", None, None)])

        remaining = {r["position"]: r for r in conn.execute("SELECT * FROM tracks WHERE release_id = 1")}

    assert remaining["A1"]["search_artist"] == "Real Artist"
    assert remaining["A2"]["title"] == "Renamed Track"
    assert remaining["A2"]["search_artist"] is None


def test_replace_tracks_updates_duration_and_discogs_artist_for_an_unchanged_track(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Various", "Comp EP", ["House"], ["Electronic"])
        store.replace_tracks(conn, 1, [("A1", "Some Track", None, None)])

        store.replace_tracks(conn, 1, [("A1", "Some Track", "3:45", "HOSTOM")])

        track = conn.execute("SELECT * FROM tracks WHERE release_id = 1").fetchone()

    assert track["duration"] == "3:45"
    assert track["discogs_artist"] == "HOSTOM"


def test_replace_tracks_handles_duplicate_position_and_title_on_the_same_release(isolated_cache):
    """Seen in real Discogs data: two distinct tracks sharing the exact same (position, title)."""
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Various", "Comp EP", ["House"], ["Electronic"])
        store.replace_tracks(conn, 1, [("A", "Hot Legs", None, None), ("A", "Hot Legs", None, None)])

        store.replace_tracks(conn, 1, [("A", "Hot Legs", None, None), ("A", "Hot Legs", None, None)])

        count = conn.execute("SELECT COUNT(*) FROM tracks WHERE release_id = 1").fetchone()[0]

    assert count == 2


# --- clear_all_matches preserves manual corrections by default ---


def test_clear_all_matches_preserves_manual_matches_by_default(isolated_cache):
    with store.connect() as conn:
        store.save_match(conn, "Artist A", "Track A", "auto-vid", "Video", "ytmusic", 90.0)
        store.save_match(conn, "Artist B", "Track B", "manual-vid", "Video", "manual", None)

        n_cleared = store.clear_all_matches(conn)

        remaining = conn.execute("SELECT query_key FROM matches").fetchall()

    assert n_cleared == 1
    assert [r[0] for r in remaining] == [store.match_key("Artist B", "Track B")]


def test_clear_all_matches_include_manual_clears_everything(isolated_cache):
    with store.connect() as conn:
        store.save_match(conn, "Artist A", "Track A", "auto-vid", "Video", "ytmusic", 90.0)
        store.save_match(conn, "Artist B", "Track B", "manual-vid", "Video", "manual", None)

        n_cleared = store.clear_all_matches(conn, include_manual=True)

        remaining = store.count_matches(conn)

    assert n_cleared == 2
    assert remaining == 0


# --- curated playlists ---


def _seed_two_tracks(conn):
    store.upsert_release(conn, 1, "Solo Artist", "Some EP", ["House"], ["Electronic"])
    store.replace_tracks(conn, 1, [("A1", "Track One", None, None), ("A2", "Track Two", None, None)])
    rows = {r["position"]: r["id"] for r in conn.execute("SELECT id, position FROM tracks WHERE release_id = 1")}
    return rows["A1"], rows["A2"]


def test_create_playlist_starts_empty(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        playlists = store.list_playlists(conn)

    assert len(playlists) == 1
    assert playlists[0]["id"] == playlist_id
    assert playlists[0]["name"] == "My Playlist"
    assert playlists[0]["track_count"] == 0
    assert playlists[0]["ytmusic_playlist_id"] is None


def test_create_playlist_rejects_duplicate_names(isolated_cache):
    with store.connect() as conn:
        store.create_playlist(conn, "My Playlist")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_playlist(conn, "My Playlist")


def test_add_tracks_to_playlist_appends_in_order_and_dedupes(isolated_cache):
    with store.connect() as conn:
        t1, t2 = _seed_two_tracks(conn)
        playlist_id = store.create_playlist(conn, "My Playlist")

        added_first = store.add_tracks_to_playlist(conn, playlist_id, [t1, t2])
        added_again = store.add_tracks_to_playlist(conn, playlist_id, [t1])  # already present

        ordered_ids = store.list_playlist_track_ids(conn, playlist_id)

    assert added_first == 2
    assert added_again == 0
    assert ordered_ids == [t1, t2]


def test_add_tracks_to_playlist_appends_after_existing_tracks(isolated_cache):
    with store.connect() as conn:
        t1, t2 = _seed_two_tracks(conn)
        playlist_id = store.create_playlist(conn, "My Playlist")

        store.add_tracks_to_playlist(conn, playlist_id, [t1])
        store.add_tracks_to_playlist(conn, playlist_id, [t2])

        ordered_ids = store.list_playlist_track_ids(conn, playlist_id)

    assert ordered_ids == [t1, t2]


def test_remove_tracks_from_playlist(isolated_cache):
    with store.connect() as conn:
        t1, t2 = _seed_two_tracks(conn)
        playlist_id = store.create_playlist(conn, "My Playlist")
        store.add_tracks_to_playlist(conn, playlist_id, [t1, t2])

        store.remove_tracks_from_playlist(conn, playlist_id, [t1])

        ordered_ids = store.list_playlist_track_ids(conn, playlist_id)

    assert ordered_ids == [t2]


def test_list_playlists_reflects_track_count(isolated_cache):
    with store.connect() as conn:
        t1, t2 = _seed_two_tracks(conn)
        playlist_id = store.create_playlist(conn, "My Playlist")
        store.add_tracks_to_playlist(conn, playlist_id, [t1, t2])

        playlists = store.list_playlists(conn)

    assert playlists[0]["track_count"] == 2


def test_delete_playlist_removes_its_track_links_too(isolated_cache):
    with store.connect() as conn:
        t1, _t2 = _seed_two_tracks(conn)
        playlist_id = store.create_playlist(conn, "My Playlist")
        store.add_tracks_to_playlist(conn, playlist_id, [t1])

        store.delete_playlist(conn, playlist_id)

        assert store.list_playlists(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0] == 0


def test_set_playlist_ytmusic_id(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        store.set_playlist_ytmusic_id(conn, playlist_id, "PL123")

        playlist = store.get_playlist(conn, playlist_id)

    assert playlist["ytmusic_playlist_id"] == "PL123"


def test_set_playlist_ytmusic_id_does_not_bump_updated_at(isolated_cache):
    """Linking to YT Music is not a content edit — `updated_at` should reflect add/remove
    track calls only, not linking (see `set_playlist_pushed_at` for tracking pushes)."""
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        before = store.get_playlist(conn, playlist_id)["updated_at"]

        store.set_playlist_ytmusic_id(conn, playlist_id, "PL123")

        after = store.get_playlist(conn, playlist_id)["updated_at"]

    assert after == before


def test_set_playlist_pushed_at_records_a_timestamp(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        assert store.get_playlist(conn, playlist_id)["pushed_at"] is None

        store.set_playlist_pushed_at(conn, playlist_id)

        playlist = store.get_playlist(conn, playlist_id)

    assert playlist["pushed_at"] is not None
    assert playlist["pushed_at"] <= time.time()


def test_set_playlist_pushed_at_does_not_bump_updated_at(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        before = store.get_playlist(conn, playlist_id)["updated_at"]

        store.set_playlist_pushed_at(conn, playlist_id)

        after = store.get_playlist(conn, playlist_id)["updated_at"]

    assert after == before


def test_playlists_table_migrates_in_pushed_at_column(isolated_cache):
    """Curated-playlist caches created before `pushed_at` existed must upgrade in place."""
    now = 1700000000.0
    conn = sqlite3.connect(isolated_cache)
    conn.execute(
        """CREATE TABLE playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ytmusic_playlist_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO playlists (name, ytmusic_playlist_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Old Playlist", "PL-old", now, now),
    )
    conn.commit()
    conn.close()

    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Old Playlist")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(playlists)")}

    assert "pushed_at" in cols
    assert playlist["pushed_at"] is None
    assert playlist["ytmusic_playlist_id"] == "PL-old"  # pre-existing data preserved


# --- Playlist folders ---


def test_create_playlist_folder_starts_empty(isolated_cache):
    with store.connect() as conn:
        folder_id = store.create_playlist_folder(conn, "My Folder")
        folders = store.list_playlist_folders(conn)

    assert len(folders) == 1
    assert folders[0]["id"] == folder_id
    assert folders[0]["name"] == "My Folder"


def test_create_playlist_folder_rejects_duplicate_names(isolated_cache):
    with store.connect() as conn:
        store.create_playlist_folder(conn, "My Folder")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_playlist_folder(conn, "My Folder")


def test_get_playlist_folder_by_id(isolated_cache):
    with store.connect() as conn:
        folder_id = store.create_playlist_folder(conn, "My Folder")

        folder = store.get_playlist_folder(conn, folder_id)

    assert folder is not None
    assert folder["name"] == "My Folder"


def test_rename_playlist_folder(isolated_cache):
    with store.connect() as conn:
        folder_id = store.create_playlist_folder(conn, "Old Name")

        store.rename_playlist_folder(conn, folder_id, "New Name")

        folder = store.get_playlist_folder(conn, folder_id)

    assert folder["name"] == "New Name"


def test_new_playlist_has_no_folder_by_default(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")

        playlist = store.get_playlist(conn, playlist_id)
        playlists = store.list_playlists(conn)

    assert playlist["folder_id"] is None
    assert playlists[0]["folder_id"] is None


def test_set_playlist_folder_assigns_a_playlist_to_a_folder(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        folder_id = store.create_playlist_folder(conn, "My Folder")

        store.set_playlist_folder(conn, playlist_id, folder_id)

        playlist = store.get_playlist(conn, playlist_id)

    assert playlist["folder_id"] == folder_id


def test_set_playlist_folder_can_move_a_playlist_back_to_ungrouped(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        folder_id = store.create_playlist_folder(conn, "My Folder")
        store.set_playlist_folder(conn, playlist_id, folder_id)

        store.set_playlist_folder(conn, playlist_id, None)

        playlist = store.get_playlist(conn, playlist_id)

    assert playlist["folder_id"] is None


def test_deleting_a_playlist_folder_unassigns_but_does_not_delete_its_playlists(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")
        folder_id = store.create_playlist_folder(conn, "My Folder")
        store.set_playlist_folder(conn, playlist_id, folder_id)

        store.delete_playlist_folder(conn, folder_id)

        playlist = store.get_playlist(conn, playlist_id)
        folders = store.list_playlist_folders(conn)

    assert playlist is not None  # the playlist itself survives
    assert playlist["folder_id"] is None  # but is no longer filed under the deleted folder
    assert folders == []


def test_list_playlist_folders_orders_by_name(isolated_cache):
    with store.connect() as conn:
        store.create_playlist_folder(conn, "Zebra")
        store.create_playlist_folder(conn, "Alpha")

        folders = store.list_playlist_folders(conn)

    assert [f["name"] for f in folders] == ["Alpha", "Zebra"]


def test_playlists_table_migrates_in_folder_id_column(isolated_cache):
    """Curated-playlist caches created before `folder_id` existed must upgrade in place,
    without disturbing pre-existing data."""
    now = 1700000000.0
    conn = sqlite3.connect(isolated_cache)
    conn.execute(
        """CREATE TABLE playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ytmusic_playlist_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            pushed_at REAL
        )"""
    )
    conn.execute(
        "INSERT INTO playlists (name, ytmusic_playlist_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Old Playlist", "PL-old", now, now),
    )
    conn.commit()
    conn.close()

    with store.connect() as conn:
        playlist = store.get_playlist_by_name(conn, "Old Playlist")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(playlists)")}

    assert "folder_id" in cols
    assert playlist["folder_id"] is None
    assert playlist["ytmusic_playlist_id"] == "PL-old"  # pre-existing data preserved
