"""Thin, rate-limited client for the Discogs API.

`DiscogsClient` is the only thing in the codebase that talks to Discogs: it lists a
user's collection and fetches a release's tracklist/embedded videos, self-throttled to
stay under Discogs' rate limit. It knows nothing about the sqlite cache or YT Music —
`scan_engine.py` is what drives it into `store.py`.
"""

from __future__ import annotations

import re
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
    """Full tracklist + embedded videos for one release, from `DiscogsClient.get_release_detail`.

    Also carries the release's own raw (uncleaned) artist/title/styles/genres/year/labels
    fields from the same `/releases/{id}` response — collection/wantlist scans get these
    for free from their basic-listing endpoint instead, but a label's release list doesn't
    include them at all, so `scan_engine.scan_label_release` sources them from here.
    """

    tracklist: list[Track]
    videos: list[DiscogsVideo]
    artists: list[str] = field(default_factory=list)  # raw release-level artist credits
    title: str = ""
    styles: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    year: int | None = None
    labels: list[str] = field(default_factory=list)  # raw label names


class DiscogsError(RuntimeError):
    """Raised for any non-2xx response from the Discogs API."""


def release_url(release_id: int) -> str:
    """Build the public discogs.com page URL for a release id."""
    return f"{WEB_BASE}/release/{release_id}"


# Discogs prefixes localized pages with a language segment right after the domain, e.g.
# discogs.com/fr/user/<name>/collection or discogs.com/pt-br/label/<id>-<name> — optional
# and ignored for parsing purposes, but every pattern below must tolerate it.
_LOCALE = r"(?:/[a-z]{2}(?:-[a-z]{2})?)?"


def parse_source_url(url: str) -> tuple[str, str] | None:
    """Parse a pasted Discogs collection/wantlist/label page URL into (source_type, source_key).

    Recognizes a label page (`/label/<id>`), a user's collection (`/user/<name>/collection`),
    and a wantlist (`/wantlist?user=<name>` or `/user/<name>/wantlist`) — each optionally
    preceded by a locale segment (e.g. `/fr/user/<name>/collection`). Returns None if the
    URL doesn't match any of these shapes.
    """
    url = url.strip()
    m = re.search(rf"discogs\.com{_LOCALE}/label/(\d+)", url)
    if m:
        return "label", m.group(1)
    m = re.search(rf"discogs\.com{_LOCALE}/user/([^/?#]+)/collection", url)
    if m:
        return "user_collection", m.group(1)
    m = re.search(rf"discogs\.com{_LOCALE}/wantlist\?user=([^&#]+)", url) or re.search(
        rf"discogs\.com{_LOCALE}/user/([^/?#]+)/wantlist", url
    )
    if m:
        return "wantlist", m.group(1)
    return None


def source_url(source_type: str, source_key: str) -> str:
    """Build the public discogs.com page URL an Other-source's (source_type, source_key)
    was originally parsed from — the inverse of `parse_source_url`, used to link back to
    the page a source was imported from."""
    if source_type == "label":
        return f"{WEB_BASE}/label/{source_key}"
    if source_type == "wantlist":
        return f"{WEB_BASE}/user/{source_key}/wantlist"
    return f"{WEB_BASE}/user/{source_key}/collection"  # user_collection


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
        """Yield basic_information dicts for every release in the user's collection (folder 0 = All).

        Works for any public collection, not just the token owner's own — used both for
        "my collection" and for an "other user's collection" source.
        """
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

    def iter_wantlist_basic(self, username: str) -> Iterator[dict]:
        """Yield basic_information dicts for every release in the user's wantlist.

        Same item shape as `iter_collection_basic` (each has a `basic_information` key) —
        Discogs just calls the top-level list key "wants" instead of "releases".
        """
        page = 1
        while True:
            data = self._get(f"/users/{username}/wants", params={"page": page, "per_page": 100})
            wants = data.get("wants", [])
            if not wants:
                return
            yield from wants
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", page):
                return
            page += 1

    def iter_label_releases(self, label_id: int) -> Iterator[dict]:
        """Yield release list items for a label's catalogue.

        Unlike collection/wantlist items, these have no `basic_information` — just flat
        `id`/`title`/`artist`/`year`/etc — so `scan_engine.scan_label_release` always
        follows up with `get_release_detail` to get styles/genres/labels.
        """
        page = 1
        while True:
            data = self._get(f"/labels/{label_id}/releases", params={"page": page, "per_page": 100})
            releases = data.get("releases", [])
            if not releases:
                return
            yield from releases
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", page):
                return
            page += 1

    def get_label(self, label_id: int) -> dict:
        """Fetch a label's own info — used only to get a real display name for a label source."""
        return self._get(f"/labels/{label_id}")

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
        return ReleaseDetail(
            tracklist=tracks,
            videos=videos,
            artists=[a["name"] for a in data.get("artists", []) or [] if a.get("name")],
            title=data.get("title", "").strip(),
            styles=data.get("styles", []) or [],
            genres=data.get("genres", []) or [],
            year=data.get("year") or None,
            labels=[label["name"] for label in data.get("labels", []) or [] if label.get("name")],
        )
