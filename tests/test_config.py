from __future__ import annotations

from discogs2ytmusic import config as config_module


def test_ensure_dirs_restricts_config_dir_to_owner_only(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CACHE_DIR", tmp_path / "cache")

    config_module.ensure_dirs()

    assert (config_dir.stat().st_mode & 0o777) == 0o700


def test_ensure_dirs_is_idempotent_on_an_existing_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o777)
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CACHE_DIR", tmp_path / "cache")

    config_module.ensure_dirs()

    assert (config_dir.stat().st_mode & 0o777) == 0o700
