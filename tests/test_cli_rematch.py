from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli, matcher, store
from discogs2ytmusic.matcher import MatchResult

runner = CliRunner()


def _always_matches(yt, artist, title):
    return MatchResult(video_id=f"vid::{artist}::{title}", video_title=title, source="ytmusic", score=100.0, channel="Some Channel")


def test_rematch_without_confirmation_aborts_and_leaves_matches_untouched(isolated_cache, monkeypatch):
    runner.invoke(cli.app, ["--library", "dummy", "scan"])
    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())
    runner.invoke(cli.app, ["--library", "dummy", "sync"])

    with store.connect() as conn:
        before = store.count_matches(conn)
    assert before > 0

    result = runner.invoke(cli.app, ["--library", "dummy", "rematch"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "Aborted" in result.output
    with store.connect() as conn:
        assert store.count_matches(conn) == before


def test_rematch_with_yes_clears_and_rebuilds_matches(isolated_cache, monkeypatch):
    runner.invoke(cli.app, ["--library", "dummy", "scan"])
    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())
    runner.invoke(cli.app, ["--library", "dummy", "sync"])

    result = runner.invoke(cli.app, ["--library", "dummy", "rematch", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Cleared" in result.output
    assert "Match preview" in result.output
    with store.connect() as conn:
        match = store.get_match(conn, *_first_track_query())
    assert match["channel"] == "Some Channel"


def _first_track_query():
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).parent / "fixtures" / "dummy_library.json").read_text())
    r = data["releases"][0]
    t = r["tracklist"][0]
    artist = t.get("discogs_artist") or r["artist"]
    return artist, t["title"]


def test_rematch_prompts_with_current_match_count(isolated_cache, monkeypatch):
    runner.invoke(cli.app, ["--library", "dummy", "scan"])
    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())
    runner.invoke(cli.app, ["--library", "dummy", "sync"])

    with store.connect() as conn:
        n = store.count_matches(conn)

    result = runner.invoke(cli.app, ["--library", "dummy", "rematch"], input="n\n")

    assert f"{n}" in result.output
