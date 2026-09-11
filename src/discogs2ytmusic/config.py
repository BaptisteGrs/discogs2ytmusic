"""File locations and saved settings, outside the repo in OS-standard config/cache dirs.

Defines where every piece of local state lives (`CACHE_DB`, `YTMUSIC_AUTH_FILE`,
`CONFIG_FILE`, all under `platformdirs`-resolved directories) and the `Config` dataclass
for the Discogs credentials/playlist-prefix saved there — nothing here talks to Discogs,
YT Music, or the sqlite schema itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir

APP_NAME = "discogs2ytmusic"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
CACHE_DIR = Path(user_cache_dir(APP_NAME))

CONFIG_FILE = CONFIG_DIR / "config.json"
YTMUSIC_AUTH_FILE = CONFIG_DIR / "ytmusic_auth.json"
CACHE_DB = CACHE_DIR / "cache.sqlite3"


DEFAULT_PLAYLIST_NAME_PREFIX = "Discogs"


@dataclass
class Config:
    """Persisted Discogs credentials and app preferences, stored as JSON at `CONFIG_FILE`."""

    discogs_token: str | None = None
    discogs_username: str | None = None
    playlist_name_prefix: str = DEFAULT_PLAYLIST_NAME_PREFIX

    @classmethod
    def load(cls) -> Config:
        """Read the saved config, or return an empty one if none exists yet."""
        if not CONFIG_FILE.exists():
            return cls()
        data = json.loads(CONFIG_FILE.read_text())
        return cls(
            discogs_token=data.get("discogs_token"),
            discogs_username=data.get("discogs_username"),
            # Missing on any config file saved before this field existed — default keeps
            # pushed playlist names byte-for-byte identical to the old hardcoded "Discogs - ...".
            playlist_name_prefix=data.get("playlist_name_prefix", DEFAULT_PLAYLIST_NAME_PREFIX),
        )

    def save(self) -> None:
        """Write this config to `CONFIG_FILE`, restricting it to owner-only permissions."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "discogs_token": self.discogs_token,
                    "discogs_username": self.discogs_username,
                    "playlist_name_prefix": self.playlist_name_prefix,
                },
                indent=2,
            )
        )
        CONFIG_FILE.chmod(0o600)


def ensure_dirs() -> None:
    """Create the config/cache directories if they don't exist yet.

    `CONFIG_DIR` holds `YTMUSIC_AUTH_FILE` (a live Google session cookie), so it's chmod'd
    to owner-only after creation — `mkdir`'s `mode` argument alone isn't enough since it's
    subject to the process umask (see issue #32).
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
