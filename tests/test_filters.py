from __future__ import annotations

import json

from discogs2ytmusic import filters, store


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(
            conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"],
            year=r.get("year"), labels=r.get("labels", []),
        )
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
        )


# --- PlaylistFilter (de)serialization ---


def test_playlist_filter_json_round_trip():
    filt = filters.PlaylistFilter(tags=["Electro", "Tech House"], labels=["Warp"], year_min=1990, year_max=2010, matched_only=True)
    restored = filters.PlaylistFilter.from_json(filt.to_json())
    assert restored == filt


def test_playlist_filter_from_json_fills_in_missing_keys():
    filt = filters.PlaylistFilter.from_json('{"tags": ["House"]}')
    assert filt == filters.PlaylistFilter(tags=["House"])


# --- release_matches (unit-level, plain dicts stand in for sqlite3.Row) ---


def _release(styles=(), genres=(), labels=(), year=None):
    """A dict standing in for a `releases` sqlite3.Row — styles/genres/labels are JSON text, same as on disk."""
    return {
        "styles": json.dumps(list(styles)),
        "genres": json.dumps(list(genres)),
        "labels": json.dumps(list(labels)),
        "year": year,
    }


def test_release_matches_tag_against_either_style_or_genre():
    release = _release(styles=["Tech House"], genres=["Electronic"])
    assert filters.release_matches(release, filters.PlaylistFilter(tags=["Tech House"]))
    assert filters.release_matches(release, filters.PlaylistFilter(tags=["Electronic"]))
    assert not filters.release_matches(release, filters.PlaylistFilter(tags=["Techno"]))


def test_release_matches_tags_are_case_insensitive_and_or_together():
    release = _release(styles=["tech house"])
    assert filters.release_matches(release, filters.PlaylistFilter(tags=["Electro", "Tech House"]))


def test_release_matches_label():
    release = _release(labels=["Warp Records"])
    assert filters.release_matches(release, filters.PlaylistFilter(labels=["warp records"]))
    assert not filters.release_matches(release, filters.PlaylistFilter(labels=["Other Label"]))


def test_release_matches_year_range_is_inclusive():
    release = _release(year=2000)
    assert filters.release_matches(release, filters.PlaylistFilter(year_min=1990, year_max=2010))
    assert filters.release_matches(release, filters.PlaylistFilter(year_min=2000, year_max=2000))
    assert not filters.release_matches(release, filters.PlaylistFilter(year_min=2001))
    assert not filters.release_matches(release, filters.PlaylistFilter(year_max=1999))


def test_release_matches_year_range_excludes_unknown_year():
    release = _release(year=None)
    assert not filters.release_matches(release, filters.PlaylistFilter(year_min=1990))


def test_release_matches_no_criteria_matches_everything():
    assert filters.release_matches(_release(), filters.PlaylistFilter())


# --- resolve_rows (integration, against the dummy library) ---


def test_resolve_rows_with_no_filter_returns_every_track_once(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    assert len(rows) == sum(len(r["tracklist"]) for r in dummy_library)
    assert len(rows) == len({(r.release_id, r.track_id) for r in rows})  # no duplicates


def test_resolve_rows_tags_filters_to_matching_styles(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filters.PlaylistFilter(tags=["Acid", "House"]))

    expected_releases = {r["release_id"] for r in dummy_library if set(r["styles"]) & {"Acid", "House"}}
    assert {r.release_id for r in rows} == expected_releases
    assert len(rows) == len({(r.release_id, r.track_id) for r in rows})  # one row per track, no duplicates


def test_resolve_rows_year_range_matches_fixture(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filters.PlaylistFilter(year_min=2020))

    expected_releases = {r["release_id"] for r in dummy_library if r["year"] >= 2020}
    assert {r.release_id for r in rows} == expected_releases


def test_resolve_rows_label_filter(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    target = next(r for r in dummy_library if r["labels"] == ["Yoyaku"])
    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filters.PlaylistFilter(labels=["Yoyaku"]))

    assert {r.release_id for r in rows} == {target["release_id"]}


def test_resolve_rows_matched_only_excludes_unmatched_tracks(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)

    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filters.PlaylistFilter(matched_only=True))

    assert len(rows) == 1
    assert rows[0].release_id == first["release_id"]
    assert rows[0].video_id == "vid1"


def test_resolve_rows_combines_criteria_with_and(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filters.PlaylistFilter(tags=["Acid"], year_min=2020))

    expected_releases = {r["release_id"] for r in dummy_library if "Acid" in r["styles"] and r["year"] >= 2020}
    assert {r.release_id for r in rows} == expected_releases
    assert expected_releases  # sanity: fixture actually has at least one match, else this test proves nothing
