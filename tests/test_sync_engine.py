from __future__ import annotations

from discogs2ytmusic import matcher, store, sync_engine


def _seed_release(conn, release_id, artist, title, styles, videos=None, tracks=None):
    store.upsert_release(conn, release_id, artist, title, styles, ["Electronic"], videos=videos or [])
    store.replace_tracks(conn, release_id, tracks or [])


def test_ensure_matches_prefers_a_confident_discogs_video_over_search(isolated_cache, monkeypatch):
    def _blow_up(yt, artist, title):
        raise AssertionError("should not fall back to search when a Discogs video confidently matches")

    monkeypatch.setattr(matcher, "find_match", _blow_up)
    monkeypatch.setattr(matcher, "resolve_channel", lambda video_id: "Yoyaku Record Store")

    with store.connect() as conn:
        _seed_release(
            conn, 1, "Aline Umber, HOSTOM", "Yoyaku Barcelona 2025", ["Deep House"],
            videos=[{"uri": "https://www.youtube.com/watch?v=AAA", "title": "HOSTOM - Tree House", "duration": 300}],
            tracks=[("A2", "Tree House", None, "HOSTOM")],
        )
        releases_with_tracks = list(store.iter_releases_with_tracks(conn))
        sync_engine.ensure_matches(conn, yt=object(), releases_with_tracks=releases_with_tracks)

    with store.connect() as conn:
        match = store.get_match(conn, "HOSTOM", "Tree House")

    assert match["video_id"] == "AAA"
    assert match["source"] == "discogs"
    assert match["channel"] == "Yoyaku Record Store"
    assert match["score"] == 100.0


def test_ensure_matches_falls_back_to_search_when_no_confident_discogs_video(isolated_cache, monkeypatch):
    monkeypatch.setattr(
        matcher, "find_match",
        lambda yt, artist, title: matcher.MatchResult("search-id", title, "ytmusic", 90.0, channel="Some Channel"),
    )

    with store.connect() as conn:
        _seed_release(
            conn, 1, "Solo Artist", "Some EP", ["House"],
            videos=[{"uri": "https://www.youtube.com/watch?v=ZZZ", "title": "Completely Unrelated", "duration": 100}],
            tracks=[("A1", "Some Track", None, None)],
        )
        releases_with_tracks = list(store.iter_releases_with_tracks(conn))
        sync_engine.ensure_matches(conn, yt=object(), releases_with_tracks=releases_with_tracks)

    with store.connect() as conn:
        match = store.get_match(conn, "Solo Artist", "Some Track")

    assert match["video_id"] == "search-id"
    assert match["source"] == "ytmusic"
    assert match["channel"] == "Some Channel"


def test_ensure_matches_skips_tracks_already_cached(isolated_cache, monkeypatch):
    def _blow_up(yt, artist, title):
        raise AssertionError("should not search a track that already has a cached match")

    monkeypatch.setattr(matcher, "find_match", _blow_up)

    with store.connect() as conn:
        _seed_release(conn, 1, "Solo Artist", "Some EP", ["House"], tracks=[("A1", "Some Track", None, None)])
        store.save_match(conn, "Solo Artist", "Some Track", "already-cached", "Video", "ytmusic", 95.0)
        releases_with_tracks = list(store.iter_releases_with_tracks(conn))
        sync_engine.ensure_matches(conn, yt=object(), releases_with_tracks=releases_with_tracks)

    with store.connect() as conn:
        match = store.get_match(conn, "Solo Artist", "Some Track")

    assert match["video_id"] == "already-cached"  # untouched


def test_ensure_matches_calls_on_track_done_once_per_query(isolated_cache, monkeypatch):
    monkeypatch.setattr(
        matcher, "find_match", lambda yt, artist, title: matcher.MatchResult("id", title, "ytmusic", 90.0)
    )

    with store.connect() as conn:
        _seed_release(
            conn, 1, "Solo Artist", "Some EP", ["House"],
            tracks=[("A1", "Track One", None, None), ("A2", "Track Two", None, None)],
        )
        releases_with_tracks = list(store.iter_releases_with_tracks(conn))
        calls = []
        sync_engine.ensure_matches(conn, yt=object(), releases_with_tracks=releases_with_tracks, on_track_done=lambda: calls.append(1))

    assert len(calls) == 2
