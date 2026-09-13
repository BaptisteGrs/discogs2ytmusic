from __future__ import annotations

import re

from typer.testing import CliRunner

from discogs2ytmusic import cli, store
from discogs2ytmusic.discogs import DiscogsError

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _plain(output: str) -> str:
    """Strip ANSI codes from CLI output — rich's own highlighter wraps punctuation/digits
    (e.g. the "1" and "()" in "Skipped 1 release(s)") in their own SGR codes, which breaks a
    plain substring check on raw output whenever color is forced on (e.g. FORCE_COLOR=1)."""
    return _ANSI_RE.sub("", output)


def test_scan_populates_cache_from_dummy_library(isolated_cache, fake_discogs_client, monkeypatch):
    monkeypatch.setattr(cli, "_load_discogs_client", lambda: (fake_discogs_client, "dummyuser"))

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.output
    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))
    assert len(releases) == 15
    assert sum(len(tracks) for _release, tracks in releases) == 18


def test_scan_skips_a_release_discogs_cant_return_instead_of_aborting(isolated_cache, fake_discogs_client, monkeypatch):
    """A release detail fetch can 404 (e.g. a wantlist item merged into another release id,
    or pulled from Discogs entirely) — that must not abort the whole scan and strand every
    release already committed before it, only skip that one."""
    poisoned_id = fake_discogs_client._releases[2]["release_id"]
    real_get_release_detail = fake_discogs_client.get_release_detail

    def flaky_get_release_detail(release_id: int):
        if release_id == poisoned_id:
            raise DiscogsError(f"Discogs API error 404 for /releases/{release_id}: not found")
        return real_get_release_detail(release_id)

    monkeypatch.setattr(fake_discogs_client, "get_release_detail", flaky_get_release_detail)
    monkeypatch.setattr(cli, "_load_discogs_client", lambda: (fake_discogs_client, "dummyuser"))

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.output
    assert "Skipped 1 release" in _plain(result.output)
    with store.connect() as conn:
        releases = list(store.iter_releases_with_tracks(conn))
    assert len(releases) == 14  # every release except the poisoned one
    assert poisoned_id not in {r["release_id"] for r, _tracks in releases}


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
        release, tracks = next((r, t) for r, t in store.iter_releases_with_tracks(conn) if r["release_id"] == 34365844)
        queries = store.effective_track_queries(release, tracks)

    by_title = {title: artist for _tid, artist, title in queries}
    assert by_title["Tree House"] == "HOSTOM"
    assert by_title["Oto"] == "Aline Umber"
