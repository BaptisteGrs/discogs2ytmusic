from __future__ import annotations

import pytest

from discogs2ytmusic import discogs as discogs_module
from discogs2ytmusic.discogs import DiscogsClient, parse_source_url, source_url


@pytest.fixture(autouse=True)
def _no_rate_limit_delay(monkeypatch):
    """These tests make several fake requests back-to-back — skip the real client's
    request-spacing sleep so they don't take multiple seconds for no reason."""
    monkeypatch.setattr(discogs_module, "MIN_REQUEST_INTERVAL", 0.0)


def test_parse_source_url_recognizes_a_label_page():
    assert parse_source_url("https://www.discogs.com/label/123-Some-Label") == ("label", "123")


def test_parse_source_url_recognizes_a_user_collection_page():
    assert parse_source_url("https://www.discogs.com/user/alice/collection") == ("user_collection", "alice")


def test_parse_source_url_recognizes_a_wantlist_query_string_page():
    assert parse_source_url("https://www.discogs.com/wantlist?user=alice") == ("wantlist", "alice")


def test_parse_source_url_recognizes_a_wantlist_path_page():
    assert parse_source_url("https://www.discogs.com/user/alice/wantlist") == ("wantlist", "alice")


def test_parse_source_url_tolerates_a_locale_prefix():
    assert parse_source_url("https://www.discogs.com/fr/label/123-Some-Label") == ("label", "123")
    assert parse_source_url("https://www.discogs.com/fr/user/alice/collection") == ("user_collection", "alice")
    assert parse_source_url("https://www.discogs.com/fr/user/alice/wantlist") == ("wantlist", "alice")
    assert parse_source_url("https://www.discogs.com/pt-br/wantlist?user=alice") == ("wantlist", "alice")


def test_parse_source_url_still_rejects_a_seller_profile_page():
    assert parse_source_url("https://www.discogs.com/fr/seller/brocshop21/profile") is None


def test_parse_source_url_strips_surrounding_whitespace():
    assert parse_source_url("  https://www.discogs.com/label/123-Some-Label  ") == ("label", "123")


def test_parse_source_url_returns_none_for_an_unrecognized_url():
    assert parse_source_url("https://www.discogs.com/release/34365844") is None
    assert parse_source_url("not a url at all") is None


def test_source_url_round_trips_with_parse_source_url():
    for url in [
        "https://www.discogs.com/label/123-Some-Label",
        "https://www.discogs.com/user/alice/collection",
        "https://www.discogs.com/user/alice/wantlist",
    ]:
        parsed = parse_source_url(url)
        assert parsed is not None
        assert parse_source_url(source_url(*parsed)) == parsed


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict | None = None):
        self.calls.append((url, params or {}))
        page = (params or {}).get("page", 1)
        return _FakeResponse(self._pages[page - 1])


def _client_with_pages(pages: list[dict]) -> tuple[DiscogsClient, _FakeSession]:
    client = DiscogsClient("token")
    session = _FakeSession(pages)
    client.session = session  # type: ignore[assignment]
    client._last_request = 0.0
    return client, session


def test_iter_wantlist_basic_paginates_over_the_wants_key():
    pages = [
        {"wants": [{"basic_information": {"id": 1}}], "pagination": {"pages": 2}},
        {"wants": [{"basic_information": {"id": 2}}], "pagination": {"pages": 2}},
    ]
    client, session = _client_with_pages(pages)

    items = list(client.iter_wantlist_basic("alice"))

    assert [i["basic_information"]["id"] for i in items] == [1, 2]
    assert session.calls[0][0] == "https://api.discogs.com/users/alice/wants"


def test_iter_label_releases_paginates_over_the_releases_key():
    pages = [
        {"releases": [{"id": 1, "title": "A"}], "pagination": {"pages": 2}},
        {"releases": [{"id": 2, "title": "B"}], "pagination": {"pages": 2}},
    ]
    client, session = _client_with_pages(pages)

    items = list(client.iter_label_releases(123))

    assert [i["id"] for i in items] == [1, 2]
    assert session.calls[0][0] == "https://api.discogs.com/labels/123/releases"


def test_iter_label_releases_stops_on_an_empty_page():
    pages = [{"releases": [], "pagination": {"pages": 1}}]
    client, _session = _client_with_pages(pages)

    assert list(client.iter_label_releases(123)) == []
