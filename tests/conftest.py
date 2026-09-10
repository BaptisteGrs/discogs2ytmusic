from __future__ import annotations

import json
from pathlib import Path

import pytest

from discogs2ytmusic import config as config_module
from discogs2ytmusic import store as store_module
from discogs2ytmusic import ytmusic_client as ytmusic_client_module
from discogs2ytmusic.discogs import DiscogsVideo, ReleaseDetail, Track

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeDiscogsClient:
    """Stands in for DiscogsClient in tests: same public interface, no network calls.

    Backed by a small library sampled from a real Discogs collection (see
    tests/fixtures/dummy_library.json) so tests exercise realistic data shapes
    without needing a token or hitting the real API.
    """

    def __init__(self, releases: list[dict]):
        self._releases = releases

    def iter_collection_basic(self, username: str):
        for r in self._releases:
            yield {
                "basic_information": {
                    "id": r["release_id"],
                    "artists": [{"name": r["artist"]}],
                    "title": r["title"],
                    "styles": r["styles"],
                    "genres": r["genres"],
                    "year": r.get("year"),
                    "labels": [{"name": name} for name in r.get("labels", [])],
                }
            }

    def get_release_detail(self, release_id: int) -> ReleaseDetail:
        for r in self._releases:
            if r["release_id"] == release_id:
                tracklist = [
                    Track(
                        position=t["position"],
                        title=t["title"],
                        duration=t["duration"],
                        artists=[t["discogs_artist"]] if t.get("discogs_artist") else [],
                    )
                    for t in r["tracklist"]
                ]
                videos = [
                    DiscogsVideo(uri=v["uri"], title=v["title"], duration=v.get("duration"))
                    for v in r.get("videos", [])
                ]
                return ReleaseDetail(tracklist=tracklist, videos=videos)
        return ReleaseDetail(tracklist=[], videos=[])


@pytest.fixture
def dummy_library() -> list[dict]:
    """18 tracks across 6 sub-genres (styles), sampled from a real Discogs collection."""
    data = json.loads((FIXTURES_DIR / "dummy_library.json").read_text())
    return data["releases"]


@pytest.fixture
def fake_discogs_client(dummy_library) -> FakeDiscogsClient:
    return FakeDiscogsClient(dummy_library)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the app's sqlite cache and YT Music auth file to temp paths so tests never
    touch real local state (including whatever's saved on the machine running the tests)."""
    cache_db = tmp_path / "cache.sqlite3"
    monkeypatch.setattr(store_module, "CACHE_DB", cache_db)
    monkeypatch.setattr(ytmusic_client_module, "YTMUSIC_AUTH_FILE", tmp_path / "ytmusic_auth.json")
    monkeypatch.setattr(ytmusic_client_module, "ensure_dirs", lambda: None)
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_dir / "config.json")
    return cache_db
