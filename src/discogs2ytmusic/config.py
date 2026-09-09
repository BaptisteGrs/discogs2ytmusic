from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir

APP_NAME = "discogs2ytmusic"

CONFIG_DIR = Path(user_config_dir(APP_NAME))
CACHE_DIR = Path(user_cache_dir(APP_NAME))

CONFIG_FILE = CONFIG_DIR / "config.json"
YTMUSIC_AUTH_FILE = CONFIG_DIR / "ytmusic_auth.json"
CACHE_DB = CACHE_DIR / "cache.sqlite3"


def write_private_file(path: Path, content: str) -> None:
    """Write `content` to `path`, owner-read/write only (`0600`).

    Creates the file with that mode from the moment it exists, rather than writing with
    the process's default umask and `chmod`-ing afterwards — the latter leaves a brief
    window where a file full of credentials (a Discogs token, an OAuth client secret, a
    saved session/refresh token) is world-readable before the permissions catch up.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


@dataclass
class Config:
    """Persisted Discogs + YT Music OAuth client credentials, stored as JSON at `CONFIG_FILE`."""

    discogs_token: str | None = None
    discogs_username: str | None = None
    ytmusic_oauth_client_id: str | None = None
    ytmusic_oauth_client_secret: str | None = None

    @classmethod
    def load(cls) -> Config:
        """Read the saved config, or return an empty one if none exists yet."""
        if not CONFIG_FILE.exists():
            return cls()
        data = json.loads(CONFIG_FILE.read_text())
        return cls(
            discogs_token=data.get("discogs_token"),
            discogs_username=data.get("discogs_username"),
            ytmusic_oauth_client_id=data.get("ytmusic_oauth_client_id"),
            ytmusic_oauth_client_secret=data.get("ytmusic_oauth_client_secret"),
        )

    def save(self) -> None:
        """Write this config to `CONFIG_FILE`, restricting it to owner-only permissions."""
        ensure_dirs()
        write_private_file(
            CONFIG_FILE,
            json.dumps(
                {
                    "discogs_token": self.discogs_token,
                    "discogs_username": self.discogs_username,
                    "ytmusic_oauth_client_id": self.ytmusic_oauth_client_id,
                    "ytmusic_oauth_client_secret": self.ytmusic_oauth_client_secret,
                },
                indent=2,
            ),
        )


def ensure_dirs() -> None:
    """Create the config/cache directories if they don't exist yet, private to the owner.

    `CONFIG_DIR` holds credential files (`CONFIG_FILE`, `YTMUSIC_AUTH_FILE`); restricting the
    directory itself, not just those files, means a listing can't leak their names either.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
