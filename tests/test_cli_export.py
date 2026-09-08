from __future__ import annotations

import csv

from typer.testing import CliRunner

from discogs2ytmusic import cli, store

runner = CliRunner()


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"])
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
        )


def _seed_matches(conn, *, matched: bool):
    """Populate the matches cache as if `sync` had already run.

    Keys matches on the *effective* (artist, title) — same resolution
    `sync`/`export` use — not the raw release artist, so tracks with their own
    Discogs-sourced or VA-split artist still come back as matched.
    """
    for release, tracks in store.iter_releases_with_tracks(conn):
        for _track_id, artist, title in store.effective_track_queries(release, tracks):
            if matched:
                store.save_match(
                    conn,
                    artist,
                    title,
                    video_id=f"vid::{artist}::{title}",
                    video_title=title,
                    source="ytmusic",
                    score=90.0,
                )
            else:
                store.save_match(conn, artist, title, video_id=None, video_title=None, source="none", score=0.0)


def test_export_writes_csv_with_expected_columns(isolated_cache, dummy_library, tmp_path):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        _seed_matches(conn, matched=True)

    out = tmp_path / "matches.csv"
    result = runner.invoke(cli.app, ["export", "--output", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()

    with out.open() as f:
        rows = list(csv.DictReader(f))

    assert rows, "expected at least one row"
    assert set(rows[0].keys()) == set(cli.EXPORT_FIELDNAMES)
    assert all(r["matched"] == "yes" for r in rows)
    assert all(r["video_id"].startswith("vid::") for r in rows)
    assert all(r["youtube_url"].startswith("https://music.youtube.com/watch?v=") for r in rows)
    assert all(r["discogs_url"].startswith("https://www.discogs.com/release/") for r in rows)
    assert len({r["match_id"] for r in rows}) == len(rows), "each row should have a distinct match_id"


def test_export_tags_unmatched_tracks_in_matched_column(isolated_cache, dummy_library, tmp_path):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        _seed_matches(conn, matched=False)

    out = tmp_path / "misses.csv"
    result = runner.invoke(cli.app, ["export", "--output", str(out)])

    assert result.exit_code == 0, result.output
    with out.open() as f:
        rows = list(csv.DictReader(f))

    assert rows, "expected rows even when nothing matched"
    assert all(r["matched"] == "no" for r in rows)
    assert all(r["video_id"] == "" for r in rows)


def test_export_style_filter_narrows_output(isolated_cache, dummy_library, tmp_path):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        _seed_matches(conn, matched=True)

    out = tmp_path / "acid.csv"
    result = runner.invoke(cli.app, ["export", "--output", str(out), "--style", "Acid"])

    assert result.exit_code == 0, result.output
    with out.open() as f:
        rows = list(csv.DictReader(f))

    assert rows
    assert all(r["style"] == "Acid" for r in rows)


def test_export_with_empty_cache_reports_nothing_to_export(isolated_cache, tmp_path):
    out = tmp_path / "matches.csv"
    result = runner.invoke(cli.app, ["export", "--output", str(out)])

    assert result.exit_code == 0, result.output
    assert "Nothing to export" in result.output
    assert not out.exists()
