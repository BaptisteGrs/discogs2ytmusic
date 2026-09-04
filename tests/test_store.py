from __future__ import annotations

import json
from collections import defaultdict

from discogs2ytmusic import store


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"])
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


def test_playlist_id_round_trip(isolated_cache):
    with store.connect() as conn:
        assert store.get_playlist_id(conn, "House") is None
        store.save_playlist_id(conn, "House", "PL123")

    with store.connect() as conn:
        assert store.get_playlist_id(conn, "House") == "PL123"
