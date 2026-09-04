from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli, store
from discogs2ytmusic.matcher import MatchResult

runner = CliRunner()


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"])
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"]) for t in r["tracklist"]],
        )


def _always_matches(yt, artist, title):
    return MatchResult(video_id=f"vid::{artist}::{title}", video_title=title, source="ytmusic", score=100.0)


def _never_matches(yt, artist, title):
    return MatchResult(video_id=None, video_title=None, source="none", score=0.0)


def test_sync_dry_run_matches_every_track_against_fixture(
    isolated_cache, isolated_missing_tracks_file, dummy_library, monkeypatch
):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(cli.matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync"])

    assert result.exit_code == 0, result.output
    assert "Dry run only" in result.output
    for style in ["House", "Techno", "Deep House", "Acid", "Breakbeat", "Trance"]:
        assert style in result.output


def test_sync_dry_run_reports_unmatched_tracks(
    isolated_cache, isolated_missing_tracks_file, dummy_library, monkeypatch
):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(cli.matcher, "find_match", _never_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync", "--style", "House"])

    assert result.exit_code == 0, result.output
    assert "House" in result.output
    # 0 matched out of 3 House tracks in the fixture
    assert "0" in result.output
    assert "3" in result.output


def test_sync_writes_missing_tracks_report_grouped_by_style(
    isolated_cache, isolated_missing_tracks_file, dummy_library, monkeypatch
):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(cli.matcher, "find_match", _never_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync", "--style", "House"])

    assert result.exit_code == 0, result.output
    # Rich may line-wrap a long path, so just check the filename rather than the exact path string.
    assert isolated_missing_tracks_file.name in result.output
    assert isolated_missing_tracks_file.exists()
    content = isolated_missing_tracks_file.read_text()
    assert "## House (3)" in content
    assert "Techno" not in content  # filtered out of this run, so absent from the report


def test_sync_clears_stale_missing_tracks_report_once_everything_matches(
    isolated_cache, isolated_missing_tracks_file, dummy_library, monkeypatch
):
    isolated_missing_tracks_file.parent.mkdir(parents=True, exist_ok=True)
    isolated_missing_tracks_file.write_text("# Missing tracks\n\nstale content from a previous run\n")

    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(cli.matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync"])

    assert result.exit_code == 0, result.output
    assert not isolated_missing_tracks_file.exists()


def test_sync_style_filter_narrows_to_one_subgenre(isolated_cache, isolated_missing_tracks_file, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(cli.matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync", "--style", "Acid"])

    assert result.exit_code == 0, result.output
    assert "Acid" in result.output
    for style in ["House", "Techno", "Deep House", "Breakbeat", "Trance"]:
        assert style not in result.output
