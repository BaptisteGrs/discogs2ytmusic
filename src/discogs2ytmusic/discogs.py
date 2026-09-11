"""Thin, rate-limited client for the Discogs API.

`DiscogsClient` is the only thing in the codebase that talks to Discogs: it lists a
user's collection and fetches a release's tracklist/embedded videos, self-throttled to
stay under Discogs' rate limit. It knows nothing about the sqlite cache or YT Music —
`scan_engine.py` is what drives it into `store.py`.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import requests

API_BASE = "https://api.discogs.com"
WEB_BASE = "https://www.discogs.com"
USER_AGENT = "discogs2ytmusic/0.1 +local-cli"

# Discogs allows 60 req/min for authenticated requests. Stay comfortably under that.
MIN_REQUEST_INTERVAL = 1.1


@dataclass
class Track:
    """One entry from a release's Discogs tracklist."""

    position: str
    title: str
    duration: str | None
    # Per-track credit; only present when it differs from the release artist.
    artists: list[str] = field(default_factory=list)


@dataclass
class DiscogsVideo:
    """A video Discogs itself embeds on a release page — usually a YouTube link."""

    uri: str  # YouTube watch URL
    title: str
    duration: int | None = None


@dataclass
class ReleaseDetail:
    """Full tracklist + embedded videos for one release, from `DiscogsClient.get_release_detail`."""

    tracklist: list[Track]
    videos: list[DiscogsVideo]


class DiscogsError(RuntimeError):
    """Raised for any non-2xx response from the Discogs API."""


def release_url(release_id: int) -> str:
    """Build the public discogs.com page URL for a release id."""
    return f"{WEB_BASE}/release/{release_id}"


class DiscogsClient:
    """Thin, rate-limited wrapper around the Discogs API endpoints this app needs."""

    def __init__(self, token: str):
        """Create a client authenticated with a Discogs personal access token."""
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

    def iter_collection_basic(self, username: str) -> Iterator[dict]:
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
            yield from releases
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", page):
                return
            page += 1

    def get_release_detail(self, release_id: int) -> ReleaseDetail:
        """Tracklist + Discogs' own embedded YouTube links, from a single `/releases/{id}` call."""
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
                    artists=[a["name"] for a in t.get("artists", []) or [] if a.get("name")],
                )
            )
        videos = [
            DiscogsVideo(uri=v["uri"], title=v.get("title", ""), duration=v.get("duration"))
            for v in data.get("videos", []) or []
            if v.get("uri")
        ]
        return ReleaseDetail(tracklist=tracks, videos=videos)
