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


def test_scan_release_tags_the_given_source_instead_of_defaulting_to_collection(isolated_cache):
    item = _basic_item(1, "Some Artist", "Some EP")

    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False, source_type="wantlist", source_key="alice")

        assert list(store.iter_releases_with_tracks(conn)) == []  # not tagged as "collection"
        releases = list(store.iter_releases_with_tracks(conn, source_type="wantlist", source_key="alice"))
        assert releases[0][0]["release_id"] == 1


def _label_item(release_id: int, artist: str, title: str) -> dict:
    return {"id": release_id, "title": title, "artist": artist, "year": 2020}


class _FakeLabelClient:
    def get_release_detail(self, release_id: int):
        from discogs2ytmusic.discogs import ReleaseDetail, Track

        return ReleaseDetail(
            tracklist=[Track(position="A1", title="Track One", duration=None, artists=[])],
            videos=[],
            artists=["Rush (2)"],
            title="Moving Pictures",
            styles=["Rock"],
            genres=[],
            year=1981,
            labels=["Some Label"],
        )


def test_scan_label_release_fetches_full_detail_for_a_new_release(isolated_cache):
    item = _label_item(1, "Rush", "Moving Pictures")

    with store.connect() as conn:
        scan_engine.scan_label_release(conn, _FakeLabelClient(), item, refresh=False, source_key="123")

        releases = list(store.iter_releases_with_tracks(conn, source_type="label", source_key="123"))
        release, tracks = releases[0]

    assert release["artist"] == "Rush"  # disambiguation suffix stripped, same as scan_release
    assert release["title"] == "Moving Pictures"
    assert [t["title"] for t in tracks] == ["Track One"]


def test_scan_label_release_skips_the_detail_fetch_once_cached_but_still_tags_the_source(isolated_cache):
    item = _label_item(1, "Rush", "Moving Pictures")

    class _BlowUpClient:
        def get_release_detail(self, release_id: int):
            raise AssertionError("should not re-fetch release detail when already cached and refresh=False")

    with store.connect() as conn:
        scan_engine.scan_label_release(conn, _FakeLabelClient(), item, refresh=False, source_key="123")
        scan_engine.scan_label_release(conn, _BlowUpClient(), item, refresh=False, source_key="456")

        releases_456 = list(store.iter_releases_with_tracks(conn, source_type="label", source_key="456"))
        assert releases_456[0][0]["release_id"] == 1


# --- ImportFilter ---


def test_import_filter_is_empty_when_no_criteria_set():
    assert scan_engine.ImportFilter().is_empty() is True
    assert scan_engine.ImportFilter(styles=["House"]).is_empty() is False
    assert scan_engine.ImportFilter(year_min=2000).is_empty() is False


def test_import_filter_matches_style_case_insensitively_against_styles_or_genres():
    filt = scan_engine.ImportFilter(styles=["house"])
    assert filt.matches(styles=["House"], format_text="", year=None) is True
    assert filt.matches(styles=["Techno"], format_text="", year=None) is False
    assert filt.matches(styles=[], format_text="", year=None) is False


def test_import_filter_style_check_is_skipped_when_styles_is_none():
    """`styles=None` is the cheap pre-detail-fetch check — it must never fail the style
    criterion on its own, only format/year."""
    filt = scan_engine.ImportFilter(styles=["house"])
    assert filt.matches(styles=None, format_text="", year=None) is True


def test_import_filter_matches_format_as_a_substring():
    filt = scan_engine.ImportFilter(formats=["Vinyl"])
    assert filt.matches(styles=None, format_text='Vinyl, 12", Album', year=None) is True
    assert filt.matches(styles=None, format_text="CD, Mixed", year=None) is False


def test_import_filter_matches_year_range():
    filt = scan_engine.ImportFilter(year_min=2000, year_max=2010)
    assert filt.matches(styles=None, format_text="", year=2005) is True
    assert filt.matches(styles=None, format_text="", year=1999) is False
    assert filt.matches(styles=None, format_text="", year=2011) is False
    assert filt.matches(styles=None, format_text="", year=None) is False


def test_import_filter_round_trips_through_dict():
    filt = scan_engine.ImportFilter(styles=["House"], formats=["Vinyl"], year_min=2000, year_max=2010)
    assert scan_engine.ImportFilter.from_dict(filt.to_dict()) == filt


def test_scan_release_skips_a_release_that_fails_the_import_filter(isolated_cache):
    item = _basic_item(1, "Some Artist", "Some EP", styles=["Techno"])
    filt = scan_engine.ImportFilter(styles=["House"])

    with store.connect() as conn:
        kept = scan_engine.scan_release(
            conn, _FakeClient(), item, refresh=False, source_type="wantlist", source_key="alice", import_filter=filt
        )

        assert kept is False
        assert store.get_release(conn, 1) is None  # never even cached


def test_scan_release_imports_a_release_that_passes_the_import_filter(isolated_cache):
    item = _basic_item(1, "Some Artist", "Some EP", styles=["House"])
    filt = scan_engine.ImportFilter(styles=["House"])

    with store.connect() as conn:
        kept = scan_engine.scan_release(
            conn, _FakeClient(), item, refresh=False, source_type="wantlist", source_key="alice", import_filter=filt
        )

        assert kept is True
        assert store.get_release(conn, 1) is not None


def test_scan_release_records_known_formats_even_when_excluded_by_the_filter(isolated_cache):
    item = _basic_item(1, "Some Artist", "Some EP", styles=["Techno"])
    item["basic_information"]["formats"] = [{"name": "Vinyl", "descriptions": ['12"', "Album"]}]
    filt = scan_engine.ImportFilter(styles=["House"])  # excludes this release on style

    with store.connect() as conn:
        kept = scan_engine.scan_release(conn, _FakeClient(), item, refresh=False, import_filter=filt)

        assert kept is False
        assert store.list_known_formats(conn) == ['12"', "Album", "Vinyl"]


def test_scan_label_release_skips_the_detail_fetch_for_a_release_excluded_by_format(isolated_cache):
    item = {**_label_item(1, "Rush", "Moving Pictures"), "format": "CD, Mixed"}
    filt = scan_engine.ImportFilter(formats=["Vinyl"])

    class _BlowUpClient:
        def get_release_detail(self, release_id: int):
            raise AssertionError("should not fetch detail for a release already excluded by format")

    with store.connect() as conn:
        kept = scan_engine.scan_label_release(
            conn, _BlowUpClient(), item, refresh=False, source_key="123", import_filter=filt
        )

        assert kept is False
        assert store.get_release(conn, 1) is None
        assert store.list_known_formats(conn) == ["CD", "Mixed"]  # recorded even though excluded


def test_scan_label_release_excludes_by_style_only_after_fetching_detail(isolated_cache):
    """Format/year passed the cheap pre-check, but the release's actual style (only known
    after a detail fetch) doesn't match — the release must still end up excluded."""
    item = {**_label_item(1, "Rush", "Moving Pictures"), "format": "Vinyl"}
    filt = scan_engine.ImportFilter(styles=["House"])  # _FakeLabelClient's detail is styled "Rock"

    with store.connect() as conn:
        kept = scan_engine.scan_label_release(
            conn, _FakeLabelClient(), item, refresh=False, source_key="123", import_filter=filt
        )

        assert kept is False
        assert store.get_release(conn, 1) is None


def test_scan_label_release_checks_cached_style_for_an_already_known_release(isolated_cache):
    """A release already cached (e.g. from a prior unfiltered scan) must be excluded from
    *this* filtered source without needing another detail fetch."""
    item = _label_item(1, "Rush", "Moving Pictures")
    with store.connect() as conn:
        # Seed it as already-cached with a style that won't match the filter below.
        store.upsert_release(conn, 1, "Rush", "Moving Pictures", ["Jazz"], [])

    class _BlowUpClient:
        def get_release_detail(self, release_id: int):
            raise AssertionError("should not fetch detail for an already-cached release")

    filt = scan_engine.ImportFilter(styles=["House"])
    with store.connect() as conn:
        kept = scan_engine.scan_label_release(
            conn, _BlowUpClient(), item, refresh=False, source_key="123", import_filter=filt
        )

        assert kept is False
        assert list(store.iter_releases_with_tracks(conn, source_type="label", source_key="123")) == []
