from __future__ import annotations

from discogs2ytmusic import scan_engine, store


def test_clean_artist_names_strips_each_names_own_disambiguation_suffix():
    assert scan_engine._clean_artist_names(["Rush (2)", "Genesis (3)"]) == "Rush, Genesis"
    assert scan_engine._clean_artist_names(["HOSTOM"]) == "HOSTOM"
    assert scan_engine._clean_artist_names([]) is None


def _basic_item(release_id: int, artist: str, title: str, styles: list[str] | None = None) -> dict:
    return {
        "basic_information": {
            "id": release_id,
            "artists": [{"name": artist}],
            "title": title,
            "styles": styles or [],
            "genres": [],
            "year": 2020,
            "labels": [{"name": "Some Label"}],
        }
    }


class _FakeClient:
    def get_release_detail(self, release_id: int):
        from discogs2ytmusic.discogs import ReleaseDetail, Track

        return ReleaseDetail(tracklist=[Track(position="A1", title="Track One", duration=None, artists=[])], videos=[])


def test_scan_release_upserts_release_and_fetches_tracklist_when_uncached(isolated_cache):
    item = _basic_item(1, "Rush (2)", "Moving Pictures", ["Rock"])

    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False)

        release, tracks = next(store.iter_releases_with_tracks(conn))

    assert release["artist"] == "Rush"  # disambiguation suffix stripped
    assert release["title"] == "Moving Pictures"
    assert [t["title"] for t in tracks] == ["Track One"]


def test_scan_release_skips_tracklist_fetch_when_already_cached_and_not_refreshing(isolated_cache):
    item = _basic_item(1, "Solo Artist", "Some EP")

    class _BlowUpClient:
        def get_release_detail(self, release_id: int):
            raise AssertionError("should not re-fetch tracklist detail when already cached and refresh=False")

    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False)
        scan_engine.scan_release(conn, _BlowUpClient(), item, refresh=False)
