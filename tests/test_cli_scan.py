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
    assert sum(len(tracks) for _release, tracks in releases) == 15


def test_scan_prints_style_breakdown(isolated_cache, fake_discogs_client, monkeypatch):
    monkeypatch.setattr(cli, "_load_discogs_client", lambda: (fake_discogs_client, "dummyuser"))

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.output
    for style in ["House", "Techno", "Deep House", "Acid", "Breakbeat", "Trance"]:
        assert style in result.output
