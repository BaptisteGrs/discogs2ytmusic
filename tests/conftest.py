from __future__ import annotations

import json
from pathlib import Path

import pytest

from discogs2ytmusic import store as store_module
from discogs2ytmusic.discogs import Track

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
                }
            }

    def get_release_tracklist(self, release_id: int) -> list[Track]:
        for r in self._releases:
            if r["release_id"] == release_id:
                return [
                    Track(position=t["position"], title=t["title"], duration=t["duration"])
                    for t in r["tracklist"]
                ]
        return []


@pytest.fixture
def dummy_library() -> list[dict]:
    """15 tracks across 6 sub-genres (styles), sampled from a real Discogs collection."""
    data = json.loads((FIXTURES_DIR / "dummy_library.json").read_text())
    return data["releases"]


@pytest.fixture
def fake_discogs_client(dummy_library) -> FakeDiscogsClient:
    return FakeDiscogsClient(dummy_library)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the app's sqlite cache to a temp file so tests never touch the real local cache."""
    cache_db = tmp_path / "cache.sqlite3"
    monkeypatch.setattr(store_module, "CACHE_DB", cache_db)
    return cache_db
