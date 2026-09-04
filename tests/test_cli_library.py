from __future__ import annotations

import sqlite3

from typer.testing import CliRunner

from discogs2ytmusic import cli, matcher, store
from discogs2ytmusic.matcher import MatchResult

runner = CliRunner()


def _always_matches(yt, artist, title):
    return MatchResult(video_id=f"vid::{artist}::{title}", video_title=title, source="ytmusic", score=100.0)


def test_library_dummy_scan_seeds_from_bundled_fixture(isolated_cache):
    result = runner.invoke(cli.app, ["--library", "dummy", "scan"])

    assert result.exit_code == 0, result.output
    assert "Loaded dummy library (15 releases)" in result.output
    for style in ["House", "Techno", "Deep House", "Acid", "Breakbeat", "Trance"]:
        assert style in result.output


def test_library_dummy_scan_does_not_touch_the_real_cache(isolated_cache):
    result = runner.invoke(cli.app, ["--library", "dummy", "scan"])
    assert result.exit_code == 0, result.output

    dummy_db = isolated_cache.parent / "dummy_cache.sqlite3"
    assert dummy_db.exists()

    # Inspect the real cache file directly (not via store.CACHE_DB — the CLI process
    # just repointed that at the dummy file, which is expected for the rest of this
    # invocation) to make sure dummy scan never wrote to it.
    assert not isolated_cache.exists() or _table_row_count(isolated_cache, "releases") == 0


def _table_row_count(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tables:
            return 0
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_library_dummy_sync_and_export_round_trip(isolated_cache, tmp_path, monkeypatch):
    scan_result = runner.invoke(cli.app, ["--library", "dummy", "scan"])
    assert scan_result.exit_code == 0, scan_result.output

    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    sync_result = runner.invoke(cli.app, ["--library", "dummy", "sync"])
    assert sync_result.exit_code == 0, sync_result.output
    assert "Match preview" in sync_result.output

    out = tmp_path / "dummy_matches.csv"
    export_result = runner.invoke(cli.app, ["--library", "dummy", "export", "--output", str(out)])
    assert export_result.exit_code == 0, export_result.output

    import csv

    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 18
    assert all(r["matched"] == "yes" for r in rows)


def test_library_defaults_to_real(isolated_cache):
    result = runner.invoke(cli.app, ["export", "--output", str(isolated_cache.parent / "out.csv")])
    assert result.exit_code == 0, result.output
    assert "Nothing to export" in result.output
