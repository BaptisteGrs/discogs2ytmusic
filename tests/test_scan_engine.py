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


def test_scan_label_release_tags_a_seller_source_when_given_source_type_seller(isolated_cache):
    """A seller-inventory item is fetched the same way as a label-catalogue one — only the
    source_type it's tagged with differs (see `DiscogsClient.iter_seller_inventory`)."""
    item = _label_item(1, "Rush", "Moving Pictures")

    with store.connect() as conn:
        scan_engine.scan_label_release(
            conn, _FakeLabelClient(), item, refresh=False, source_key="brocshop21", source_type="seller"
        )

        releases = list(store.iter_releases_with_tracks(conn, source_type="seller", source_key="brocshop21"))

    assert releases[0][0]["artist"] == "Rush"


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


# --- refresh_release ---


def test_refresh_release_replaces_tracklist_and_release_fields_from_full_detail(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Old Artist", "Old Title", ["Old Style"], ["Old Genre"])
        store.replace_tracks(conn, 1, [("A1", "Old Track", None, None)])

        scan_engine.refresh_release(conn, _FakeLabelClient(), 1, source_type="label", source_key="123")

        release, tracks = next(store.iter_releases_with_tracks(conn, source_type="label", source_key="123"))

    assert release["artist"] == "Rush"  # disambiguation suffix stripped, from the fetched detail
    assert release["title"] == "Moving Pictures"
    assert release["styles"] == '["Rock"]'
    assert release["year"] == 1981
    assert [t["title"] for t in tracks] == ["Track One"]


def test_refresh_release_preserves_a_manual_per_track_correction(isolated_cache):
    """Regression guard for the CLAUDE.md locking invariant: refreshing a release must not
    wipe out a manual `search_artist` correction on one of its tracks — `store.replace_tracks`
    keeps it by matching the refreshed tracklist back to the existing track by (position, title)."""
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Artist", "Title", [], [])
        store.replace_tracks(conn, 1, [("A1", "Track One", None, None)])
        track_id = store.get_release_tracks(conn, 1)[0]["id"]
        store.set_track_search_artist(conn, track_id, "My Override")

        scan_engine.refresh_release(conn, _FakeLabelClient(), 1)

        track = store.get_track(conn, track_id)

    assert track["search_artist"] == "My Override"


def test_refresh_release_defaults_to_the_collection_source(isolated_cache):
    with store.connect() as conn:
        scan_engine.refresh_release(conn, _FakeLabelClient(), 1)

        assert list(store.iter_releases_with_tracks(conn))[0][0]["release_id"] == 1


def test_refresh_release_uses_a_prefetched_detail_instead_of_fetching_again(isolated_cache):
    """`scan_label_release` already has to fetch a release's detail early (to re-check its
    `ImportFilter`) — passing that same detail through avoids a second, redundant API call."""
    from discogs2ytmusic.discogs import ReleaseDetail, Track

    class _BlowUpClient:
        def get_release_detail(self, release_id: int):
            raise AssertionError("should not fetch detail again when one was already given")

    prefetched = ReleaseDetail(
        tracklist=[Track(position="A1", title="Track One", duration=None, artists=[])],
        videos=[],
        artists=["Rush (2)"],
        title="Moving Pictures",
        styles=["Rock"],
        genres=[],
        year=1981,
        labels=["Some Label"],
    )

    with store.connect() as conn:
        scan_engine.refresh_release(conn, _BlowUpClient(), 1, detail=prefetched)

        release, tracks = next(store.iter_releases_with_tracks(conn))

    assert release["artist"] == "Rush"
    assert [t["title"] for t in tracks] == ["Track One"]


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


def test_scan_release_stores_discogs_date_added(isolated_cache):
    item = {**_basic_item(1, "Some Artist", "Some EP"), "date_added": "2021-02-19T08:30:44-08:00"}

    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False)

        assert store.get_release_date_added(conn, 1, "collection", "") == "2021-02-19T08:30:44-08:00"


def test_scan_release_updates_date_added_on_rescan(isolated_cache):
    """A re-scan without --refresh must still pick up the date, so an existing cache (whose
    rows predate the column, or whose record was removed and re-added on Discogs) gets it."""
    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), _basic_item(1, "Some Artist", "Some EP"), refresh=False)
        assert store.get_release_date_added(conn, 1, "collection", "") is None

        item = {**_basic_item(1, "Some Artist", "Some EP"), "date_added": "2023-05-16T07:25:33-07:00"}
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False)

        assert store.get_release_date_added(conn, 1, "collection", "") == "2023-05-16T07:25:33-07:00"


def test_scan_release_stores_date_added_under_a_wantlist_source(isolated_cache):
    item = {**_basic_item(1, "Some Artist", "Some EP"), "date_added": "2022-11-25T12:00:00-08:00"}

    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False, source_type="wantlist", source_key="alice")

        assert store.get_release_date_added(conn, 1, "wantlist", "alice") == "2022-11-25T12:00:00-08:00"


def test_refresh_release_keeps_a_stored_date_added(isolated_cache):
    item = {**_basic_item(1, "Some Artist", "Some EP"), "date_added": "2022-11-25T12:00:00-08:00"}

    with store.connect() as conn:
        scan_engine.scan_release(conn, _FakeClient(), item, refresh=False)
        scan_engine.refresh_release(conn, _FakeLabelClient(), 1)

        assert store.get_release_date_added(conn, 1, "collection", "") == "2022-11-25T12:00:00-08:00"
