from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

API_BASE = "https://api.discogs.com"
USER_AGENT = "discogs2ytmusic/0.1 +local-cli"

# Discogs allows 60 req/min for authenticated requests. Stay comfortably under that.
MIN_REQUEST_INTERVAL = 1.1


@dataclass
class Track:
    position: str
    title: str
    duration: str | None


@dataclass
class Release:
    release_id: int
    artist: str
    title: str
    styles: list[str]
    genres: list[str]
    tracklist: list[Track] = field(default_factory=list)


class DiscogsError(RuntimeError):
    pass


class DiscogsClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.headers["Authorization"] = f"Discogs token={token}"
        self._last_request = 0.0

    def _get(self, path: str, params: dict | None = None) -> dict:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        resp = self.session.get(f"{API_BASE}{path}", params=params)
        self._last_request = time.monotonic()
        if resp.status_code == 429:
            time.sleep(5)
            return self._get(path, params)
        if not resp.ok:
            raise DiscogsError(f"Discogs API error {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    def identity(self) -> dict:
        """Resolve the token owner (used to confirm auth + default username)."""
        return self._get("/oauth/identity")

    def iter_collection_basic(self, username: str):
        """Yield basic_information dicts for every release in the user's collection (folder 0 = All)."""
        page = 1
        while True:
            data = self._get(
                f"/users/{username}/collection/folders/0/releases",
                params={"page": page, "per_page": 100},
            )
            releases = data.get("releases", [])
            if not releases:
                return
            for item in releases:
                yield item
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", page):
                return
            page += 1

    def get_release_tracklist(self, release_id: int) -> list[Track]:
        data = self._get(f"/releases/{release_id}")
        tracks = []
        for t in data.get("tracklist", []):
            if t.get("type_", "track") != "track":
                continue  # skip headings/subtracks-as-index rows
            tracks.append(
                Track(
                    position=t.get("position", ""),
                    title=t.get("title", "").strip(),
                    duration=t.get("duration") or None,
                )
            )
        return tracks
