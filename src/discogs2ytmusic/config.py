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


@dataclass
class Config:
    """Persisted Discogs credentials, stored as JSON at `CONFIG_FILE`."""

    discogs_token: str | None = None
    discogs_username: str | None = None

    @classmethod
    def load(cls) -> Config:
        """Read the saved config, or return an empty one if none exists yet."""
        if not CONFIG_FILE.exists():
            return cls()
        data = json.loads(CONFIG_FILE.read_text())
        return cls(
            discogs_token=data.get("discogs_token"),
            discogs_username=data.get("discogs_username"),
        )

    def save(self) -> None:
        """Write this config to `CONFIG_FILE`, restricting it to owner-only permissions."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "discogs_token": self.discogs_token,
                    "discogs_username": self.discogs_username,
                },
                indent=2,
            )
        )
        CONFIG_FILE.chmod(0o600)


def ensure_dirs() -> None:
    """Create the config/cache directories if they don't exist yet, private to the owner.

    `CONFIG_DIR` holds credential files (`CONFIG_FILE`, `YTMUSIC_AUTH_FILE`); `mkdir`'s own
    `mode` argument is still subject to the process umask, so a follow-up `chmod` is needed
    to reliably land on `0700` rather than just "whatever the umask left standing".
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
