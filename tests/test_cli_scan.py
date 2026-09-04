from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli, store

runner = CliRunner()


def test_scan_populates_cache_from_dummy_library(isolated_cache, fake_discogs_client, monkeypatch):
    monkeypatch.setattr(cli, "_load_discogs_client", lambda: (fake_discogs_client, "dummyuser"))

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.output
    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))
    assert len(releases) == 15
    assert sum(len(tracks) for _release, tracks in releases) == 18


def test_scan_prints_style_breakdown(isolated_cache, fake_discogs_client, monkeypatch):
    monkeypatch.setattr(cli, "_load_discogs_client", lambda: (fake_discogs_client, "dummyuser"))

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.output
    for style in ["House", "Techno", "Deep House", "Acid", "Breakbeat", "Trance"]:
        assert style in result.output


def test_scan_captures_discogs_per_track_artist_credits(isolated_cache, fake_discogs_client, monkeypatch):
    """End-to-end regression test: on a various-artists release, scan must capture each
    track's own Discogs artist credit, not just the release's comma-joined artist string."""
    monkeypatch.setattr(cli, "_load_discogs_client", lambda: (fake_discogs_client, "dummyuser"))

    result = runner.invoke(cli.app, ["scan"])
    assert result.exit_code == 0, result.output

    with store.connect() as conn:
        release, tracks = next(
            (r, t) for r, t in store.iter_releases_with_tracks(conn) if r["release_id"] == 34365844
        )
        queries = store.effective_track_queries(release, tracks)

    by_title = {title: artist for _tid, artist, title in queries}
    assert by_title["Tree House"] == "HOSTOM"
    assert by_title["Oto"] == "Aline Umber"


def test_clean_artist_names_strips_each_names_own_disambiguation_suffix():
    assert cli._clean_artist_names(["Rush (2)", "Genesis (3)"]) == "Rush, Genesis"
    assert cli._clean_artist_names(["HOSTOM"]) == "HOSTOM"
    assert cli._clean_artist_names([]) is None
