from __future__ import annotations

import json

from discogs2ytmusic import filters, store


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


# --- PlaylistFilter (de)serialization ---


def test_playlist_filter_json_round_trip():
    filt = filters.PlaylistFilter(
        tag_groups=[
            filters.TagGroup(tags=["Electro", "Tech House"], mode="or"),
            filters.TagGroup(tags=["Vinyl Only", "Reissue"], mode="and"),
        ],
        tag_groups_mode="and",
        labels=["Warp"],
        year_min=1990,
        year_max=2010,
        matched_only=True,
        channels=["Warp Records"],
    )
    restored = filters.PlaylistFilter.from_json(filt.to_json())
    assert restored == filt


def test_playlist_filter_from_json_fills_in_missing_keys():
    filt = filters.PlaylistFilter.from_json('{"tag_groups": [{"tags": ["House"]}]}')
    assert filt == filters.PlaylistFilter(tag_groups=[filters.TagGroup(tags=["House"])])


def test_playlist_filter_from_json_migrates_the_pre_20_flat_tags_list_to_one_or_group():
    """`playlist_defs.filter_json` rows saved before #20 store a flat `tags: [...]` list —
    it should load as a single OR group, reproducing the old flat-list-of-tags behavior."""
    filt = filters.PlaylistFilter.from_json('{"tags": ["Acid", "House"]}')
    assert filt == filters.PlaylistFilter(tag_groups=[filters.TagGroup(tags=["Acid", "House"], mode="or")])


def test_playlist_filter_from_json_migrates_an_empty_legacy_tags_list_to_no_groups():
    filt = filters.PlaylistFilter.from_json('{"tags": []}')
    assert filt == filters.PlaylistFilter()


# --- release_matches (unit-level, plain dicts stand in for sqlite3.Row) ---


def _release(styles=(), genres=(), labels=(), year=None):
    """A dict standing in for a `releases` sqlite3.Row — styles/genres/labels are JSON text, same as on disk."""
    return {
        "styles": json.dumps(list(styles)),
        "genres": json.dumps(list(genres)),
        "labels": json.dumps(list(labels)),
        "year": year,
    }


def _tag_filter(*groups: filters.TagGroup, mode: filters.BoolOp = "or") -> filters.PlaylistFilter:
    return filters.PlaylistFilter(tag_groups=list(groups), tag_groups_mode=mode)


def test_release_matches_tag_against_either_style_or_genre():
    release = _release(styles=["Tech House"], genres=["Electronic"])
    assert filters.release_matches(release, _tag_filter(filters.TagGroup(tags=["Tech House"])))
    assert filters.release_matches(release, _tag_filter(filters.TagGroup(tags=["Electronic"])))
    assert not filters.release_matches(release, _tag_filter(filters.TagGroup(tags=["Techno"])))


def test_release_matches_tags_within_a_group_are_case_insensitive_and_or_together():
    release = _release(styles=["tech house"])
    assert filters.release_matches(release, _tag_filter(filters.TagGroup(tags=["Electro", "Tech House"])))


def test_release_matches_and_mode_group_requires_every_tag():
    release = _release(styles=["Tech House", "Electro"])
    assert filters.release_matches(release, _tag_filter(filters.TagGroup(tags=["Tech House", "Electro"], mode="and")))
    assert not filters.release_matches(
        release, _tag_filter(filters.TagGroup(tags=["Tech House", "Techno"], mode="and"))
    )


def test_release_matches_combines_groups_with_or_by_default():
    release = _release(styles=["Acid"])
    filt = _tag_filter(
        filters.TagGroup(tags=["House"], mode="or"),
        filters.TagGroup(tags=["Acid"], mode="or"),
    )
    assert filters.release_matches(release, filt)


def test_release_matches_combines_groups_with_and_when_requested():
    release = _release(styles=["Acid"])  # satisfies the second group only
    filt = _tag_filter(
        filters.TagGroup(tags=["House"], mode="or"),
        filters.TagGroup(tags=["Acid"], mode="or"),
        mode="and",
    )
    assert not filters.release_matches(release, filt)

    release_both = _release(styles=["House", "Acid"])
    assert filters.release_matches(release_both, filt)


def test_release_matches_an_empty_group_contributes_nothing():
    release = _release(styles=["House"])
    filt = _tag_filter(filters.TagGroup(tags=[]), filters.TagGroup(tags=["House"]))
    assert filters.release_matches(release, filt)


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
        rows = filters.resolve_rows(conn, _tag_filter(filters.TagGroup(tags=["Acid", "House"])))

    expected_releases = {r["release_id"] for r in dummy_library if set(r["styles"]) & {"Acid", "House"}}
    assert {r.release_id for r in rows} == expected_releases
    assert len(rows) == len({(r.release_id, r.track_id) for r in rows})  # one row per track, no duplicates


def test_resolve_rows_and_mode_tag_group_requires_every_tag_in_the_group(isolated_cache, dummy_library):
    """Every fixture release is genre-tagged "Electronic", so an AND group of
    ["House", "Electronic"] should behave just like a plain "House" style filter."""
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(
            conn, filters.PlaylistFilter(tag_groups=[filters.TagGroup(tags=["House", "Electronic"], mode="and")])
        )

    expected_releases = {r["release_id"] for r in dummy_library if "House" in r["styles"]}
    assert {r.release_id for r in rows} == expected_releases
    assert expected_releases  # sanity: fixture actually has at least one match, else this test proves nothing


def test_resolve_rows_tag_groups_mode_and_requires_every_group_to_match(isolated_cache, dummy_library):
    """No single fixture release is tagged both House and Techno, so an AND-combined
    House group and Techno group should match nothing."""
    with store.connect() as conn:
        _seed(conn, dummy_library)

    filt = filters.PlaylistFilter(
        tag_groups=[filters.TagGroup(tags=["House"]), filters.TagGroup(tags=["Techno"])],
        tag_groups_mode="and",
    )
    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filt)

    assert rows == []


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


def test_resolve_rows_channel_filter_excludes_other_channels_and_unmatched(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first, second = dummy_library[0], dummy_library[1]
        store.save_match(
            conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0, channel="Yoyaku"
        )
        store.save_match(
            conn,
            second["artist"],
            second["tracklist"][0]["title"],
            "vid2",
            "Video",
            "ytmusic",
            90.0,
            channel="Other Uploader",
        )

    with store.connect() as conn:
        rows = filters.resolve_rows(conn, filters.PlaylistFilter(channels=["Yoyaku"]))

    assert len(rows) == 1
    assert rows[0].video_id == "vid1"


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


# --- resolve_playlist_rows ---


def test_resolve_playlist_rows_returns_tracks_in_playlist_order(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        all_tracks = conn.execute("SELECT id, title FROM tracks ORDER BY id").fetchall()
        t1, t2 = all_tracks[3][0], all_tracks[0][0]  # deliberately out of natural/id order
        playlist_id = store.create_playlist(conn, "My Playlist")
        store.add_tracks_to_playlist(conn, playlist_id, [t1, t2])

    with store.connect() as conn:
        rows = filters.resolve_playlist_rows(conn, playlist_id)

    assert [r.track_id for r in rows] == [t1, t2]


def test_resolve_playlist_rows_empty_playlist(isolated_cache):
    with store.connect() as conn:
        playlist_id = store.create_playlist(conn, "My Playlist")

    with store.connect() as conn:
        rows = filters.resolve_playlist_rows(conn, playlist_id)

    assert rows == []


def test_resolve_playlist_rows_reflects_match_and_locked_state(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()[0]
        store.save_match(conn, first["artist"], first["tracklist"][0]["title"], "vid1", "Video", "ytmusic", 90.0)
        playlist_id = store.create_playlist(conn, "My Playlist")
        store.add_tracks_to_playlist(conn, playlist_id, [track_id])

    with store.connect() as conn:
        rows = filters.resolve_playlist_rows(conn, playlist_id)

    assert len(rows) == 1
    assert rows[0].video_id == "vid1"
    assert rows[0].matched is True
    assert rows[0].locked is False


# --- resolve_rows: locked flag ---


def test_resolve_rows_flags_a_manually_overridden_artist_as_locked(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        track = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (first["release_id"],)).fetchone()
        store.set_track_search_artist(conn, track[0], "Corrected Artist")

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    corrected = next(r for r in rows if r.track_id == track[0])
    assert corrected.locked is True
    others = [r for r in rows if r.track_id != track[0]]
    assert all(not r.locked for r in others)


def test_resolve_rows_flags_a_manual_match_as_locked(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.save_match(
            conn, first["artist"], first["tracklist"][0]["title"], "manual-vid", "Manual pick", "manual", None
        )

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    manual_row = next(r for r in rows if r.video_id == "manual-vid")
    assert manual_row.locked is True


def test_resolve_rows_flags_a_no_tracklist_release_artist_override_as_locked(isolated_cache):
    """Regression test for #42: a release with no tracklist on file falls back to a
    single synthetic row (track_id=None) whose artist/title already reflect the
    release-level override, but `locked` used to only ever check the (empty) per-track
    `artist_overridden` dict, so it always reported False even though the value is in
    fact protected from a rescan clobbering it."""
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Original Artist", "Original Title", [], [])
        store.set_release_artist_override(conn, 1, "Corrected Artist")

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    assert len(rows) == 1
    assert rows[0].track_id is None
    assert rows[0].track_artist == "Corrected Artist"
    assert rows[0].locked is True


def test_resolve_rows_flags_a_no_tracklist_release_title_override_as_locked(isolated_cache):
    with store.connect() as conn:
        store.upsert_release(conn, 1, "Original Artist", "Original Title", [], [])
        store.set_release_title_override(conn, 1, "Corrected Title")

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    assert len(rows) == 1
    assert rows[0].track_id is None
    assert rows[0].release_title == "Corrected Title"
    assert rows[0].locked is True


def test_resolve_rows_flags_a_styles_override_as_locked(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.set_release_styles_override(conn, first["release_id"], ["Corrected Style"])

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    overridden = [r for r in rows if r.release_id == dummy_library[0]["release_id"]]
    assert overridden
    assert all(r.locked for r in overridden)
    assert all(r.styles == ["Corrected Style"] for r in overridden)


def test_resolve_rows_flags_a_genres_override_as_locked(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        first = dummy_library[0]
        store.set_release_genres_override(conn, first["release_id"], ["Corrected Genre"])

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    overridden = [r for r in rows if r.release_id == dummy_library[0]["release_id"]]
    assert overridden
    assert all(r.locked for r in overridden)
    assert all(r.genres == ["Corrected Genre"] for r in overridden)


def test_resolve_rows_unmatched_untouched_track_is_not_locked(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(conn)

    assert all(not r.locked for r in rows)


def test_resolve_rows_combines_criteria_with_and(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    with store.connect() as conn:
        rows = filters.resolve_rows(
            conn, filters.PlaylistFilter(tag_groups=[filters.TagGroup(tags=["Acid"])], year_min=2020)
        )

    expected_releases = {r["release_id"] for r in dummy_library if "Acid" in r["styles"] and r["year"] >= 2020}
    assert {r.release_id for r in rows} == expected_releases
    assert expected_releases  # sanity: fixture actually has at least one match, else this test proves nothing
