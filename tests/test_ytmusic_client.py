from __future__ import annotations

import json

import pytest
from ytmusicapi.auth.types import AuthType

from discogs2ytmusic import config as config_module
from discogs2ytmusic import ytmusic_client


@pytest.fixture
def isolated_auth_file(tmp_path, monkeypatch):
    """Redirect the saved auth file and config (holding the OAuth client id/secret) to temp
    paths, so tests never touch the real OS config/cache directories."""
    auth_file = tmp_path / "ytmusic_auth.json"
    monkeypatch.setattr(ytmusic_client, "YTMUSIC_AUTH_FILE", auth_file)
    monkeypatch.setattr(ytmusic_client, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    return auth_file


def _fake_token(**overrides: object) -> dict:
    token = {
        "scope": "https://www.googleapis.com/auth/youtube",
        "token_type": "Bearer",
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
        "expires_at": 9999999999,
        "expires_in": 3600,
    }
    token.update(overrides)
    return token


def test_is_authenticated_is_false_when_no_file_exists(isolated_auth_file):
    assert ytmusic_client.is_authenticated() is False


def test_is_authenticated_is_false_for_a_leftover_pre_oauth_cookie_file(isolated_auth_file):
    """A file saved by the old cookie-based auth this replaced (cookie + x-goog-authuser,
    no OAuth token fields) must not read as "connected" — it needs a fresh OAuth sign-in."""
    isolated_auth_file.write_text(json.dumps({"cookie": "SID=fake", "x-goog-authuser": "0"}))

    assert ytmusic_client.is_authenticated() is False


def test_is_authenticated_is_true_for_a_complete_oauth_token(isolated_auth_file):
    isolated_auth_file.write_text(json.dumps(_fake_token()))

    assert ytmusic_client.is_authenticated() is True


def test_save_and_load_oauth_client_round_trips(isolated_auth_file):
    assert ytmusic_client.load_oauth_client() is None

    ytmusic_client.save_oauth_client("cid", "secret")

    assert ytmusic_client.load_oauth_client() == ("cid", "secret")


def test_save_oauth_client_raises_value_error_when_a_value_is_missing(isolated_auth_file):
    with pytest.raises(ValueError, match="required"):
        ytmusic_client.save_oauth_client("cid", "")

    assert ytmusic_client.load_oauth_client() is None


def test_begin_oauth_flow_returns_a_device_code(isolated_auth_file, monkeypatch):
    def _get_code(self):
        return {"verification_url": "https://google.com/device", "user_code": "ABC-123", "device_code": "opaque"}

    monkeypatch.setattr(ytmusic_client.OAuthCredentials, "get_code", _get_code)

    code = ytmusic_client.begin_oauth_flow("cid", "secret")

    assert code == ytmusic_client.DeviceCode(
        verification_url="https://google.com/device", user_code="ABC-123", device_code="opaque"
    )


def test_begin_oauth_flow_wraps_a_bad_client_error(isolated_auth_file, monkeypatch):
    from ytmusicapi.auth.oauth.exceptions import BadOAuthClient

    def _get_code(self):
        raise BadOAuthClient("bad client")

    monkeypatch.setattr(ytmusic_client.OAuthCredentials, "get_code", _get_code)

    with pytest.raises(ytmusic_client.YTMusicAuthError, match="bad client"):
        ytmusic_client.begin_oauth_flow("cid", "secret")


def test_complete_oauth_flow_saves_a_token_ytmusicapi_accepts_as_oauth_auth(isolated_auth_file, monkeypatch):
    def _token_from_code(self, device_code):
        assert device_code == "opaque"
        return _fake_token()

    monkeypatch.setattr(ytmusic_client.OAuthCredentials, "token_from_code", _token_from_code)

    ytmusic_client.complete_oauth_flow("cid", "secret", "opaque")

    saved = json.loads(isolated_auth_file.read_text())
    assert saved["access_token"] == "fake-access"
    assert saved["refresh_token"] == "fake-refresh"
    assert ytmusic_client.is_authenticated() is True


def test_complete_oauth_flow_raises_a_clear_error_when_sign_in_is_not_finished_yet(isolated_auth_file, monkeypatch):
    def _token_from_code(self, device_code):
        return {"error": "authorization_pending"}

    monkeypatch.setattr(ytmusic_client.OAuthCredentials, "token_from_code", _token_from_code)

    with pytest.raises(ytmusic_client.YTMusicAuthError, match="Not finished yet"):
        ytmusic_client.complete_oauth_flow("cid", "secret", "opaque")

    assert not isolated_auth_file.exists()


def test_complete_oauth_flow_raises_on_other_device_flow_errors(isolated_auth_file, monkeypatch):
    def _token_from_code(self, device_code):
        return {"error": "expired_token"}

    monkeypatch.setattr(ytmusic_client.OAuthCredentials, "token_from_code", _token_from_code)

    with pytest.raises(ytmusic_client.YTMusicAuthError, match="expired_token"):
        ytmusic_client.complete_oauth_flow("cid", "secret", "opaque")


def test_get_client_requires_setup_first(isolated_auth_file):
    with pytest.raises(RuntimeError, match="Not authenticated"):
        ytmusic_client.get_client(authenticated=True)


def test_get_client_requires_the_oauth_client_id_and_secret_too(isolated_auth_file):
    """A token file can exist without a saved client id/secret (e.g. copied from another
    machine) — refreshing it needs both, so this must fail with an actionable message rather
    than an opaque error from deep inside ytmusicapi."""
    isolated_auth_file.write_text(json.dumps(_fake_token()))

    with pytest.raises(RuntimeError, match="missing its OAuth client"):
        ytmusic_client.get_client(authenticated=True)


def test_get_client_unauthenticated_never_touches_the_saved_auth_file(isolated_auth_file):
    yt = ytmusic_client.get_client(authenticated=False)
    assert yt.auth_type == AuthType.UNAUTHORIZED


def test_get_client_builds_an_authenticated_oauth_client(isolated_auth_file):
    isolated_auth_file.write_text(json.dumps(_fake_token()))
    ytmusic_client.save_oauth_client("cid", "secret")

    yt = ytmusic_client.get_client(authenticated=True)

    assert yt.auth_type == AuthType.OAUTH_CUSTOM_CLIENT


def test_get_client_resecures_the_auth_file_even_if_permissions_had_drifted(isolated_auth_file):
    isolated_auth_file.write_text(json.dumps(_fake_token()))
    isolated_auth_file.chmod(0o644)
    ytmusic_client.save_oauth_client("cid", "secret")

    ytmusic_client.get_client(authenticated=True)

    assert (isolated_auth_file.stat().st_mode & 0o777) == 0o600


class _FakePlaylistsClient:
    """Stands in for a `YTMusic` client for `get_or_create_playlist`/`add_tracks`, with no
    network calls — records what would have been sent instead of hitting a real account."""

    def __init__(self, existing: list[dict] | None = None, create_result: str | dict = "new-playlist-id"):
        self.existing = existing or []
        self.create_result = create_result
        self.created: list[tuple[str, str]] = []
        self.added: list[tuple[str, list[str]]] = []

    def get_library_playlists(self, limit: int = 200) -> list[dict]:
        return self.existing

    def create_playlist(self, name: str, description: str = "") -> str | dict:
        self.created.append((name, description))
        return self.create_result

    def add_playlist_items(self, playlist_id: str, video_ids: list[str], duplicates: bool = False) -> None:
        self.added.append((playlist_id, video_ids))


def test_get_or_create_playlist_reuses_an_existing_playlist_by_name():
    yt = _FakePlaylistsClient(existing=[{"title": "Discogs - My Favorites", "playlistId": "existing-id"}])

    result = ytmusic_client.get_or_create_playlist(yt, "Discogs - My Favorites")  # type: ignore[arg-type]

    assert result == "existing-id"
    assert yt.created == []  # never called create_playlist


def test_get_or_create_playlist_creates_when_no_existing_playlist_matches():
    yt = _FakePlaylistsClient(existing=[], create_result="brand-new-id")

    result = ytmusic_client.get_or_create_playlist(yt, "Discogs - New One", description="desc")  # type: ignore[arg-type]

    assert result == "brand-new-id"
    assert yt.created == [("Discogs - New One", "desc")]


def test_get_or_create_playlist_raises_on_an_error_dict_from_create_playlist():
    """ytmusicapi returns an error dict here instead of raising, on failure."""
    yt = _FakePlaylistsClient(existing=[], create_result={"error": "SERVER_ERROR"})

    with pytest.raises(RuntimeError, match="Failed to create playlist"):
        ytmusic_client.get_or_create_playlist(yt, "Discogs - Oops")  # type: ignore[arg-type]


def test_add_tracks_chunks_large_lists_to_stay_under_the_request_size_limit():
    yt = _FakePlaylistsClient()
    video_ids = [f"v{i}" for i in range(120)]

    ytmusic_client.add_tracks(yt, "playlist-id", video_ids)  # type: ignore[arg-type]

    assert [len(chunk) for _pid, chunk in yt.added] == [50, 50, 20]
    assert all(pid == "playlist-id" for pid, _chunk in yt.added)


def test_add_tracks_is_a_noop_for_an_empty_list():
    yt = _FakePlaylistsClient()

    ytmusic_client.add_tracks(yt, "playlist-id", [])  # type: ignore[arg-type]

    assert yt.added == []
