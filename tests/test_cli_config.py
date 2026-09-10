from __future__ import annotations

from typer.testing import CliRunner

from discogs2ytmusic import cli
from discogs2ytmusic.config import Config

runner = CliRunner()


def test_set_playlist_prefix_saves_and_preserves_other_fields(isolated_cache):
    Config(discogs_token="tok", discogs_username="user").save()

    result = runner.invoke(cli.app, ["set-playlist-prefix", "My Vinyl"])

    assert result.exit_code == 0, result.output
    assert "My Vinyl" in result.output
    loaded = Config.load()
    assert loaded.playlist_name_prefix == "My Vinyl"
    assert loaded.discogs_token == "tok"
    assert loaded.discogs_username == "user"
