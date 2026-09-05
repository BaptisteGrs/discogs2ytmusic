from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

API_BASE = "https://api.discogs.com"
WEB_BASE = "https://www.discogs.com"
USER_AGENT = "discogs2ytmusic/0.1 +local-cli"

# Discogs allows 60 req/min for authenticated requests. Stay comfortably under that.
MIN_REQUEST_INTERVAL = 1.1


@dataclass
class Track:
    position: str
    title: str
    duration: str | None
    artists: list[str] = field(default_factory=list)  # per-track credit, only present when it differs from the release artist


@dataclass
class DiscogsVideo:
    uri: str  # YouTube watch URL
    title: str
    duration: int | None = None


@dataclass
class ReleaseDetail:
    tracklist: list[Track]
    videos: list[DiscogsVideo]


class DiscogsError(RuntimeError):
    pass


def release_url(release_id: int) -> str:
    return f"{WEB_BASE}/release/{release_id}"


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
