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


def test_push_style_playlists_dry_run_never_touches_ytmusic(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())

    result = runner.invoke(cli.app, ["push-style-playlists"])

    assert result.exit_code == 0, result.output
    assert "Dry run only" in result.output
    assert "Push preview" in result.output


def test_push_style_playlists_apply_without_confirmation_aborts(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        cli.ytmusic_client,
        "get_or_create_playlist",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not create a playlist without confirmation")),
    )

    result = runner.invoke(cli.app, ["push-style-playlists", "--apply"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output


def test_push_style_playlists_apply_with_yes_creates_playlists(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    created: list[str] = []
    pushed: dict[str, list[str]] = {}

    monkeypatch.setattr(matcher, "find_match", _always_matches)
    monkeypatch.setattr(cli.ytmusic_client, "is_authenticated", lambda: True)
    monkeypatch.setattr(cli.ytmusic_client, "get_client", lambda authenticated=True: object())
    monkeypatch.setattr(
        cli.ytmusic_client,
        "get_or_create_playlist",
        lambda yt, name, description="": created.append(name) or f"playlist::{name}",
    )
    monkeypatch.setattr(
        cli.ytmusic_client,
        "add_tracks",
        lambda yt, playlist_id, video_ids: pushed.setdefault(playlist_id, []).extend(video_ids),
    )

    result = runner.invoke(cli.app, ["push-style-playlists", "--apply", "--yes", "--style", "Acid"])

    assert result.exit_code == 0, result.output
    assert created == ["Discogs - Acid"]
    assert pushed
    assert "Pushed 'Discogs - Acid'" in result.output


def test_push_style_playlists_apply_requires_ytmusic_auth(isolated_cache, dummy_library, monkeypatch):
    with store.connect() as conn:
        _seed(conn, dummy_library)

    monkeypatch.setattr(cli.ytmusic_client, "is_authenticated", lambda: False)

    result = runner.invoke(cli.app, ["push-style-playlists", "--apply", "--yes"])

    assert result.exit_code == 1
    assert "Not authenticated" in result.output
