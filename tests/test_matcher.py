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
