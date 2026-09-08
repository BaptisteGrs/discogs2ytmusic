from __future__ import annotations

from discogs2ytmusic import matcher


class FakeYTMusic:
    def __init__(self, songs=None, videos=None):
        self._songs = songs or []
        self._videos = videos or []

    def search(self, query, filter=None, limit=5):
        return self._songs if filter == "songs" else self._videos


def test_search_ytmusic_picks_best_scoring_result(dummy_library):
    artist = dummy_library[0]["artist"]
    title = dummy_library[0]["tracklist"][0]["title"]
    yt = FakeYTMusic(
        songs=[
            {"videoId": "bad", "title": "Completely Unrelated Song", "artists": [{"name": "Nobody"}]},
            {"videoId": "good", "title": title, "artists": [{"name": artist}]},
        ]
    )

    result = matcher.search_ytmusic(yt, artist, title)

    assert result is not None
    assert result.video_id == "good"
    assert result.source == "ytmusic"


def test_search_ytmusic_returns_none_below_threshold():
    yt = FakeYTMusic(songs=[{"videoId": "x", "title": "zzz", "artists": [{"name": "qqq"}]}])

    result = matcher.search_ytmusic(yt, "Aline Umber, HOSTOM", "Oto")

    assert result is None


def test_find_match_falls_back_to_ytdlp_when_ytmusic_has_nothing(monkeypatch, dummy_library):
    track = dummy_library[3]
    artist, title = track["artist"], track["tracklist"][0]["title"]

    monkeypatch.setattr(matcher, "search_ytmusic", lambda yt, a, t: None)
    monkeypatch.setattr(
        matcher,
        "search_ytdlp",
        lambda a, t: matcher.MatchResult("fallback-id", f"{a} - {t}", "ytdlp", 80.0),
    )

    result = matcher.find_match(yt=object(), artist=artist, title=title)

    assert result.video_id == "fallback-id"
    assert result.source == "ytdlp"


def test_find_match_returns_none_result_when_both_sources_fail(monkeypatch):
    monkeypatch.setattr(matcher, "search_ytmusic", lambda yt, a, t: None)
    monkeypatch.setattr(matcher, "search_ytdlp", lambda a, t: None)

    result = matcher.find_match(yt=object(), artist="Nobody", title="Nothing")

    assert result.video_id is None
    assert result.source == "none"


def test_search_ytmusic_captures_the_channel_name():
    yt = FakeYTMusic(songs=[{"videoId": "good", "title": "Tree House", "artists": [{"name": "Yoyaku Record Store"}]}])

    result = matcher.search_ytmusic(yt, "HOSTOM", "Tree House")

    assert result is not None
    assert result.channel == "Yoyaku Record Store"


# --- match_against_discogs_videos ---


def test_match_against_discogs_videos_matches_by_title():
    videos = [
        {"uri": "https://www.youtube.com/watch?v=AAA", "title": "HOSTOM - Tree House", "duration": 300},
        {"uri": "https://www.youtube.com/watch?v=BBB", "title": "Aline Umber - Oto", "duration": 300},
    ]
    queries = [(1, "HOSTOM", "Tree House"), (2, "Aline Umber", "Oto")]

    results = matcher.match_against_discogs_videos(videos, queries)

    assert results[1].video_id == "AAA"
    assert results[1].source == "discogs"
    assert results[2].video_id == "BBB"


def test_match_against_discogs_videos_ignores_unrelated_bonus_videos():
    videos = [
        {"uri": "https://www.youtube.com/watch?v=AAA", "title": "HOSTOM - Tree House", "duration": 300},
        {"uri": "https://www.youtube.com/watch?v=ZZZ", "title": "Completely Unrelated Live Set", "duration": 3000},
    ]
    queries = [(1, "HOSTOM", "Tree House")]

    results = matcher.match_against_discogs_videos(videos, queries)

    assert set(results.keys()) == {1}
    assert results[1].video_id == "AAA"


def test_match_against_discogs_videos_bonus_remix_does_not_steal_a_different_track():
    """A remix-of-Tree-House video is plausible for the Tree House track but must not
    score well enough to get attached to a completely different track ("It's a Dream")
    just because a release has more videos than songs."""
    videos = [
        {"uri": "https://www.youtube.com/watch?v=AAA", "title": "HOSTOM - Tree House", "duration": 300},
        {"uri": "https://www.youtube.com/watch?v=BBB", "title": "HOSTOM - Tree House (Acoustic)", "duration": 300},
    ]
    queries = [(1, "HOSTOM", "It's a Dream"), (2, "HOSTOM", "Tree House")]

    results = matcher.match_against_discogs_videos(videos, queries)

    assert 1 not in results  # neither Tree House video is a confident match for "It's a Dream"
    assert 2 in results
    assert len(results) == 1  # only one video is spent on the one track that actually matches


def test_match_against_discogs_videos_never_assigns_the_same_video_twice():
    videos = [{"uri": "https://www.youtube.com/watch?v=AAA", "title": "HOSTOM - Tree House", "duration": 300}]
    queries = [(1, "HOSTOM", "Tree House"), (2, "HOSTOM", "Tree House")]  # contrived duplicate-title edge case

    results = matcher.match_against_discogs_videos(videos, queries)

    assert len(results) == 1  # the single video can only go to one of the two tracks


def test_match_against_discogs_videos_skips_the_no_tracklist_fallback_row():
    videos = [{"uri": "https://www.youtube.com/watch?v=AAA", "title": "Some Artist Some Title", "duration": 100}]
    queries = [(None, "Some Artist", "Some Title")]

    results = matcher.match_against_discogs_videos(videos, queries)

    assert results == {}


def test_match_against_discogs_videos_empty_inputs():
    assert matcher.match_against_discogs_videos([], [(1, "A", "B")]) == {}
    assert matcher.match_against_discogs_videos([{"uri": "x", "title": "y"}], []) == {}


# --- resolve_channel ---


class _FakeResponse:
    def __init__(self, json_body=None, raises=False):
        self._json_body = json_body or {}
        self._raises = raises

    def raise_for_status(self):
        if self._raises:
            raise RuntimeError("boom")

    def json(self):
        return self._json_body


def test_resolve_channel_returns_the_channel_name(monkeypatch):
    monkeypatch.setattr(
        matcher.requests,
        "get",
        lambda url, params=None, timeout=None: _FakeResponse({"author_name": "Yoyaku Record Store"}),
    )

    assert matcher.resolve_channel("abc123") == "Yoyaku Record Store"


def test_resolve_channel_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(matcher.requests, "get", lambda url, params=None, timeout=None: _FakeResponse(raises=True))

    assert matcher.resolve_channel("abc123") is None


def test_resolve_channel_returns_none_when_request_itself_raises(monkeypatch):
    def _blow_up(url, params=None, timeout=None):
        raise ConnectionError("network down")

    monkeypatch.setattr(matcher.requests, "get", _blow_up)

    assert matcher.resolve_channel("abc123") is None


def test_resolve_channel_queries_the_oembed_endpoint_for_the_given_video(monkeypatch):
    captured = {}

    def _fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse({"author_name": "Some Channel"})

    monkeypatch.setattr(matcher.requests, "get", _fake_get)

    matcher.resolve_channel("abc123")

    assert captured["url"] == matcher.OEMBED_URL
    assert captured["params"]["url"] == "https://www.youtube.com/watch?v=abc123"
