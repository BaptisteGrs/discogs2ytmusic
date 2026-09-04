from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from discogs2ytmusic import store


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


def test_seeded_library_has_15_tracks_across_6_styles(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))

    total_tracks = sum(len(tracks) for _release, tracks in releases)
    styles = {s for release, _tracks in releases for s in json.loads(release["styles"])}

    assert total_tracks == 15
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
        "Deep House": 2,
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
    conn.execute(
        "CREATE TABLE playlists (style TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute("INSERT INTO playlists VALUES (?, ?, ?)", ("House", "PL-legacy", 1700000000.0))
    conn.commit()
    conn.close()

    with store.connect() as conn:
        row = store.get_playlist_def_by_name(conn, "Discogs - House")
        legacy_cols = {r[1] for r in conn.execute("PRAGMA table_info(playlists)")}

    assert row["ytmusic_playlist_id"] == "PL-legacy"
    assert json.loads(row["filter_json"]) == {"tags": ["House"]}
    assert not legacy_cols  # old table is gone


def test_release_overrides_apply_to_effective_track_queries(isolated_cache, dummy_library):
    release_id = dummy_library[0]["release_id"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.set_release_artist_override(conn, release_id, "Corrected Release Artist")

    with store.connect() as conn:
        release, tracks = next(r for r in store.iter_releases_with_tracks(conn) if r[0]["release_id"] == release_id)
        queries = store.effective_track_queries(release, tracks)

    assert all(artist == "Corrected Release Artist" for _tid, artist, _title in queries)


def test_scan_captures_year_and_labels(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))

    by_id = {r["release_id"]: r for r, _tracks in releases}
    first = dummy_library[0]
    assert by_id[first["release_id"]]["year"] == first["year"]
    assert json.loads(by_id[first["release_id"]]["labels"]) == first["labels"]
