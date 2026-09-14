from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli, store
from discogs2ytmusic.config import Config

runner = CliRunner()


def _seed(conn, dummy_library):
    for r in dummy_library:
        store.upsert_release(conn, r["release_id"], r["artist"], r["title"], r["styles"], r["genres"])
        store.replace_tracks(
            conn,
            r["release_id"],
            [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
        )


def test_reset_without_confirmation_aborts_and_leaves_cache_untouched(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
        store.save_match(conn, artist, title, "vid1", "Video", "ytmusic", 90.0)
        conn.commit()

    result = runner.invoke(cli.app, ["reset"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "Aborted" in result.output
    with store.connect() as conn:
        assert store.count_matches(conn) == 1
        assert len(list(store.iter_releases_with_tracks(conn))) == len(dummy_library)


def test_reset_prompt_names_what_will_be_deleted(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
        store.save_match(conn, artist, title, "vid1", "Video", "ytmusic", 90.0)
        store.add_other_source(conn, "label", "123", "Some Label")
        conn.commit()
        n_releases = len(list(store.iter_releases_with_tracks(conn)))

    result = runner.invoke(cli.app, ["reset"], input="n\n")

    assert f"{n_releases} release(s)" in result.output
    assert "1 cached match(es)" in result.output
    assert "1 Other Source(s)" in result.output
    assert "re-add any Other Source" in result.output


def test_reset_with_yes_wipes_the_cache(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
        store.save_match(conn, artist, title, "vid1", "Video", "ytmusic", 90.0)
        conn.commit()

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert "reset" in result.output.lower()
    with store.connect() as conn:
        assert list(store.iter_releases_with_tracks(conn)) == []
        assert store.count_matches(conn) == 0
        assert store.list_other_sources(conn) == []


def test_reset_discards_manual_corrections(isolated_cache, dummy_library):
    with store.connect() as conn:
        _seed(conn, dummy_library)
        artist, title = dummy_library[0]["artist"], dummy_library[0]["tracklist"][0]["title"]
        store.save_match(conn, artist, title, "manually-picked", "Manual pick", "manual", None)
        conn.commit()

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    with store.connect() as conn:
        assert store.get_match(conn, artist, title) is None


def test_reset_preserves_saved_credentials(isolated_cache, dummy_library):
    Config(discogs_token="tok", discogs_username="user", playlist_name_prefix="My Vinyl").save()
    with store.connect() as conn:
        _seed(conn, dummy_library)
        conn.commit()

    result = runner.invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    loaded = Config.load()
    assert loaded.discogs_token == "tok"
    assert loaded.discogs_username == "user"
    assert loaded.playlist_name_prefix == "My Vinyl"
