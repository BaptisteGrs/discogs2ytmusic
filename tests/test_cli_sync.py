from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli, matcher, store
from discogs2ytmusic.matcher import MatchResult

runner = CliRunner()


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"])
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
        )


def _always_matches(yt, artist, title):
    return MatchResult(video_id=f"vid::{artist}::{title}", video_title=title, source="ytmusic", score=100.0)


def _never_matches(yt, artist, title):
    return MatchResult(video_id=None, video_title=None, source="none", score=0.0)


def test_sync_dry_run_matches_every_track_against_fixture(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync"])

    assert result.exit_code == 0, result.output
    assert "Match preview" in result.output
    for style in ["House", "Techno", "Deep House", "Acid", "Breakbeat", "Trance"]:
        assert style in result.output


def test_sync_dry_run_reports_unmatched_tracks(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(matcher, "find_match", _never_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync", "--style", "House"])

    assert result.exit_code == 0, result.output
    assert "House" in result.output
    # 0 matched out of 3 House tracks in the fixture
    assert "0" in result.output
    assert "3" in result.output


def test_sync_style_filter_narrows_to_one_subgenre(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["sync", "--style", "Acid"])

    assert result.exit_code == 0, result.output
    assert "Acid" in result.output
    for style in ["House", "Techno", "Deep House", "Breakbeat", "Trance"]:
        assert style not in result.output
