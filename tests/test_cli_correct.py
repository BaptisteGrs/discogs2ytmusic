from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli, matcher, store

runner = CliRunner()


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"])
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
        )


def test_correct_video_id_overwrites_match(isolated_cache, dummy_library):
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, artist, title, "wrong-id", "Wrong video", "ytmusic", 70.0)
        match_id = store.get_match(conn, artist, title)["id"]

    result = runner.invoke(cli.app, ["correct", str(match_id), "--video-id", "right-id"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        row = store.get_match_by_id(conn, match_id)
    assert row["video_id"] == "right-id"
    assert row["source"] == "manual"
    assert row["score"] is None


def test_correct_video_id_parses_youtube_url(isolated_cache, dummy_library):
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, artist, title, "wrong-id", "Wrong video", "ytmusic", 70.0)
        match_id = store.get_match(conn, artist, title)["id"]

    result = runner.invoke(
        cli.app, ["correct", str(match_id), "--video-id", "https://music.youtube.com/watch?v=abc123XYZ&foo=bar"]
    )
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        row = store.get_match_by_id(conn, match_id)
    assert row["video_id"] == "abc123XYZ"


def test_correct_reject_clears_video_but_keeps_row(isolated_cache, dummy_library):
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, artist, title, "some-id", "Some video", "ytmusic", 70.0)
        match_id = store.get_match(conn, artist, title)["id"]

    result = runner.invoke(cli.app, ["correct", str(match_id), "--reject"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        row = store.get_match_by_id(conn, match_id)
    assert row is not None
    assert row["video_id"] is None
    assert row["source"] == "manual"


def test_correct_clear_deletes_the_row(isolated_cache, dummy_library):
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, artist, title, "some-id", "Some video", "ytmusic", 70.0)
        match_id = store.get_match(conn, artist, title)["id"]

    result = runner.invoke(cli.app, ["correct", str(match_id), "--clear"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        assert store.get_match_by_id(conn, match_id) is None
        assert store.get_match(conn, artist, title) is None


def test_correct_requires_exactly_one_mode(isolated_cache, dummy_library):
    artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        store.save_match(conn, artist, title, "some-id", "Some video", "ytmusic", 70.0)
        match_id = store.get_match(conn, artist, title)["id"]

    no_mode = runner.invoke(cli.app, ["correct", str(match_id)])
    assert no_mode.exit_code != 0
    assert "exactly one" in no_mode.output

    two_modes = runner.invoke(cli.app, ["correct", str(match_id), "--reject", "--clear"])
    assert two_modes.exit_code != 0
    assert "exactly one" in two_modes.output


def test_correct_unknown_match_id_errors(isolated_cache):
    result = runner.invoke(cli.app, ["correct", "9999", "--reject"])
    assert result.exit_code != 0
    assert "No cached match" in result.output


def test_fix_artist_overrides_search_query_used_by_sync(isolated_cache, dummy_library, monkeypatch):
    release = dummy_library[0]
    track = release["tracklist"][0]

    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute(
            "SELECT id FROM tracks WHERE release_id = ? AND title = ?", (release["release_id"], track["title"])
        ).fetchone()[0]

    fix_result = runner.invoke(cli.app, ["fix-artist", str(track_id), "--artist", "Corrected Artist"])
    assert fix_result.exit_code == 0, fix_result.output

    seen_queries = []

    def _record(yt, artist, title):
        seen_queries.append((artist, title))
        from discogs2ytmusic.matcher import MatchResult

        return MatchResult(video_id="vid", video_title=title, source="ytmusic", score=100.0)

    monkeypatch.setattr(matcher, "find_match", _record)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    sync_result = runner.invoke(cli.app, ["sync", "--style", release["styles"][0]])
    assert sync_result.exit_code == 0, sync_result.output
    assert ("Corrected Artist", track["title"]) in seen_queries
    assert not any(q[0] == release["artist"] and q[1] == track["title"] for q in seen_queries)

    with store.connect() as conn:
        match = store.get_match(conn, "Corrected Artist", track["title"])
    assert match is not None
    assert match["video_id"] == "vid"


def test_fix_artist_clear_reverts_to_release_artist(isolated_cache, dummy_library):
    release = dummy_library[0]
    track = release["tracklist"][0]

    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute(
            "SELECT id FROM tracks WHERE release_id = ? AND title = ?", (release["release_id"], track["title"])
        ).fetchone()[0]
        store.set_track_search_artist(conn, track_id, "Temporary Override")

    result = runner.invoke(cli.app, ["fix-artist", str(track_id), "--clear"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        row = store.get_track(conn, track_id)
    assert row["search_artist"] is None


def test_fix_artist_requires_exactly_one_mode(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks LIMIT 1").fetchone()[0]

    no_mode = runner.invoke(cli.app, ["fix-artist", str(track_id)])
    assert no_mode.exit_code != 0
    assert "exactly one" in no_mode.output

    both = runner.invoke(cli.app, ["fix-artist", str(track_id), "--artist", "X", "--clear"])
    assert both.exit_code != 0
    assert "exactly one" in both.output


def test_fix_artist_unknown_track_id_errors(isolated_cache):
    result = runner.invoke(cli.app, ["fix-artist", "9999", "--artist", "X"])
    assert result.exit_code != 0
    assert "No track" in result.output


def test_fix_style_overrides_effective_styles(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    result = runner.invoke(cli.app, ["fix-style", str(track_id), "--style", "Corrected Style", "--style", "Another"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        release_row = store.get_release(conn, release["release_id"])
    assert store.effective_track_styles(track, release_row) == ["Corrected Style", "Another"]


def test_fix_style_clear_reverts_to_release_styles(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]
        store.set_track_styles_override(conn, track_id, ["Temporary Style"])

    result = runner.invoke(cli.app, ["fix-style", str(track_id), "--clear"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        release_row = store.get_release(conn, release["release_id"])
    assert track["styles_override"] is None
    assert store.effective_track_styles(track, release_row) == release["styles"]


def test_fix_style_requires_exactly_one_mode(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks LIMIT 1").fetchone()[0]

    no_mode = runner.invoke(cli.app, ["fix-style", str(track_id)])
    assert no_mode.exit_code != 0

    both = runner.invoke(cli.app, ["fix-style", str(track_id), "--style", "X", "--clear"])
    assert both.exit_code != 0


def test_fix_style_unknown_track_id_errors(isolated_cache):
    result = runner.invoke(cli.app, ["fix-style", "9999", "--style", "X"])
    assert result.exit_code != 0
    assert "No track" in result.output


def test_fix_genre_overrides_effective_genres(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]

    result = runner.invoke(cli.app, ["fix-genre", str(track_id), "--genre", "Corrected Genre"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        release_row = store.get_release(conn, release["release_id"])
    assert store.effective_track_genres(track, release_row) == ["Corrected Genre"]


def test_fix_genre_clear_reverts_to_release_genres(isolated_cache, dummy_library):
    release = dummy_library[0]
    with store.connect() as conn:
        _seed(conn, dummy_library)
        track_id = conn.execute("SELECT id FROM tracks WHERE release_id = ?", (release["release_id"],)).fetchone()[0]
        store.set_track_genres_override(conn, track_id, ["Temporary Genre"])

    result = runner.invoke(cli.app, ["fix-genre", str(track_id), "--clear"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        release_row = store.get_release(conn, release["release_id"])
    assert track["genres_override"] is None
    assert store.effective_track_genres(track, release_row) == release["genres"]


def test_fix_genre_unknown_track_id_errors(isolated_cache):
    result = runner.invoke(cli.app, ["fix-genre", "9999", "--genre", "X"])
    assert result.exit_code != 0
    assert "No track" in result.output
