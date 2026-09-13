from __future__ import annotations

import json
import stat
import sys

import pytest

from discogs2ytmusic import config as config_module
from discogs2ytmusic.config import Config, ensure_dirs


def _isolate_config_file(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_dir / "config.json")


def test_save_and_load_round_trip_includes_playlist_name_prefix(tmp_path, monkeypatch):
    _isolate_config_file(tmp_path, monkeypatch)

    cfg = Config(discogs_token="tok", discogs_username="user", playlist_name_prefix="My Prefix")
    cfg.save()

    loaded = Config.load()

    assert loaded == cfg


def test_load_defaults_playlist_name_prefix_to_discogs_when_missing():
    """A config file with no saved value yet (never touched this feature) must still
    resolve to "Discogs -" so existing users get byte-for-byte identical playlist names."""
    assert Config().playlist_name_prefix == "Discogs -" == config_module.DEFAULT_PLAYLIST_NAME_PREFIX


def test_load_defaults_playlist_name_prefix_for_a_config_file_saved_before_this_field_existed(tmp_path, monkeypatch):
    _isolate_config_file(tmp_path, monkeypatch)
    config_module.CONFIG_DIR.mkdir(parents=True)
    config_module.CONFIG_FILE.write_text(json.dumps({"discogs_token": "tok", "discogs_username": "user"}))

    loaded = Config.load()

    assert loaded.discogs_token == "tok"
    assert loaded.playlist_name_prefix == "Discogs -"


def test_load_with_no_saved_file_returns_defaults(tmp_path, monkeypatch):
    _isolate_config_file(tmp_path, monkeypatch)

    loaded = Config.load()

    assert loaded == Config()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions don't apply on Windows")
def test_ensure_dirs_restricts_config_dir_to_owner_only(tmp_path, monkeypatch):
    """CONFIG_DIR holds ytmusic_auth.json (a live Google session cookie) — it must not rely on
    an inherited ~/.config permission, since `mkdir`'s `mode` is itself subject to umask (#32)."""
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CACHE_DIR", tmp_path / "cache")

    ensure_dirs()

    mode = stat.S_IMODE(config_dir.stat().st_mode)
    assert mode == 0o700
