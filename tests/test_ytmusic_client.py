from __future__ import annotations

import json
from pathlib import Path

import pytest
from ytmusicapi import YTMusic
from ytmusicapi.auth.types import AuthType
from ytmusicapi.exceptions import YTMusicError, YTMusicUserError

from discogs2ytmusic import ytmusic_client


@pytest.fixture
def isolated_auth_file(tmp_path, monkeypatch):
    """Redirect the saved-auth-headers path to a temp file, and no-op `ensure_dirs`
    so `run_setup` never touches the real OS config/cache directories in a test."""
    auth_file = tmp_path / "ytmusic_auth.json"
    monkeypatch.setattr(ytmusic_client, "YTMUSIC_AUTH_FILE", auth_file)
    monkeypatch.setattr(ytmusic_client, "ensure_dirs", lambda: None)
    return auth_file


def _fake_headers_file(tmp_path: Path, headers: dict) -> Path:
    path = tmp_path / "pasted_headers.txt"
    path.write_text("\n".join(f"{k}: {v}" for k, v in headers.items()))
    return path


def test_is_authenticated_reflects_whether_the_auth_file_exists(isolated_auth_file):
    assert ytmusic_client.is_authenticated() is False
    isolated_auth_file.write_text("{}")
    assert ytmusic_client.is_authenticated() is True


def test_run_setup_saves_headers_ytmusicapi_accepts_as_browser_auth(tmp_path, isolated_auth_file):
    """Regression test for the "Sync to YT Music doesn't work" bug (#15): a saved auth file
    with only cookie + x-goog-authuser (no `authorization`) makes ytmusicapi's
    `determine_auth_type` fall through to expecting an OAuth token, so `YTMusic(path)` raises
    immediately — before any request is even sent. `run_setup` must inject a marker so the
    saved file is recognized as browser/cookie auth instead."""
    headers_file = _fake_headers_file(
        tmp_path,
        {"cookie": "__Secure-3PAPISID=deadbeef; SID=fake", "x-goog-authuser": "0"},
    )

    ytmusic_client.run_setup(from_file=headers_file)

    saved = json.loads(isolated_auth_file.read_text())
    assert "authorization" in saved
    assert "SAPISIDHASH" in saved["authorization"]

    yt = YTMusic(str(isolated_auth_file))
    assert yt.auth_type == AuthType.BROWSER


def test_run_setup_aborts_when_cookie_or_authuser_missing(tmp_path, isolated_auth_file):
    headers_file = _fake_headers_file(tmp_path, {"cookie": "SID=fake"})  # no x-goog-authuser

    with pytest.raises(SystemExit):
        ytmusic_client.run_setup(from_file=headers_file)

    assert not isolated_auth_file.exists()


def test_get_client_wraps_a_stale_pre_fix_auth_file_in_a_clear_error(isolated_auth_file):
    """An auth file saved before the `_SAPISIDHASH_MARKER` fix (cookie + x-goog-authuser only,
    no `authorization`) must fail with an actionable message, not a raw ytmusicapi traceback."""
    isolated_auth_file.write_text(json.dumps({"cookie": "SID=fake", "x-goog-authuser": "0"}))

    with pytest.raises(RuntimeError, match="Re-run"):
        ytmusic_client.get_client(authenticated=True)


def test_get_client_requires_setup_first(isolated_auth_file):
    with pytest.raises(RuntimeError, match="Not authenticated"):
        ytmusic_client.get_client(authenticated=True)


def test_get_client_unauthenticated_never_touches_the_saved_auth_file(isolated_auth_file):
    yt = ytmusic_client.get_client(authenticated=False)
    assert yt.auth_type == AuthType.UNAUTHORIZED


class _FakePlaylistsClient:
    """Stands in for a `YTMusic` client for playlist create/lookup/add/remove, with no
    network calls — records what would have been sent instead of hitting a real account."""

    def __init__(
        self,
        existing: list[dict] | None = None,
        create_result: str | dict = "new-playlist-id",
        playlist_tracks: list[dict] | None = None,
        add_result: str | dict | None = None,
    ):
        self.existing = existing or []
        self.create_result = create_result
        self.playlist_tracks = playlist_tracks or []
        self.add_result = add_result if add_result is not None else {"status": "STATUS_SUCCEEDED"}
        self.created: list[tuple[str, str]] = []
        self.added: list[tuple[str, list[str]]] = []
        self.add_duplicates_flags: list[bool] = []
        self.removed: list[tuple[str, list[dict]]] = []

    def get_library_playlists(self, limit: int = 200) -> list[dict]:
        return self.existing

    def create_playlist(self, name: str, description: str = "") -> str | dict:
        self.created.append((name, description))
        return self.create_result

    def add_playlist_items(self, playlist_id: str, video_ids: list[str], duplicates: bool = False) -> str | dict:
        self.added.append((playlist_id, video_ids))
        self.add_duplicates_flags.append(duplicates)
        return self.add_result

    def get_playlist(self, playlist_id: str, limit: int | None = 100) -> dict:
        return {"id": playlist_id, "tracks": self.playlist_tracks}

    def remove_playlist_items(self, playlist_id: str, videos: list[dict]) -> None:
        self.removed.append((playlist_id, videos))


def test_find_playlist_returns_the_id_of_a_matching_playlist_by_name():
    yt = _FakePlaylistsClient(existing=[{"title": "Discogs - My Favorites", "playlistId": "existing-id"}])

    assert ytmusic_client.find_playlist(yt, "Discogs - My Favorites") == "existing-id"  # type: ignore[arg-type]


def test_find_playlist_returns_none_when_no_playlist_matches():
    yt = _FakePlaylistsClient(existing=[{"title": "Some Other Playlist", "playlistId": "other-id"}])

    assert ytmusic_client.find_playlist(yt, "Discogs - My Favorites") is None  # type: ignore[arg-type]


def test_get_or_create_playlist_reuses_an_existing_playlist_by_name():
    yt = _FakePlaylistsClient(existing=[{"title": "Discogs - My Favorites", "playlistId": "existing-id"}])

    result = ytmusic_client.get_or_create_playlist(yt, "Discogs - My Favorites")  # type: ignore[arg-type]

    assert result == ("existing-id", False)
    assert yt.created == []  # never called create_playlist


def test_get_or_create_playlist_creates_when_no_existing_playlist_matches():
    yt = _FakePlaylistsClient(existing=[], create_result="brand-new-id")

    result = ytmusic_client.get_or_create_playlist(yt, "Discogs - New One", description="desc")  # type: ignore[arg-type]

    assert result == ("brand-new-id", True)
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


def test_add_tracks_dedupes_before_sending():
    """Sending the same video id twice in one call is meaningless — dedupe first."""
    yt = _FakePlaylistsClient()

    ytmusic_client.add_tracks(yt, "playlist-id", ["v1", "v2", "v1", "v3"])  # type: ignore[arg-type]

    assert yt.added == [("playlist-id", ["v1", "v2", "v3"])]


def test_add_tracks_passes_duplicates_true_to_avoid_the_confirm_dialog_failure():
    """Confirmed against a real account: with duplicates=False, a video already on the playlist
    (even one our own diff didn't think was there yet, e.g. a stale read right after a previous
    add) makes YT Music reject the *whole* request with STATUS_FAILED and an interactive
    'Duplicates' confirm-dialog payload — dropping every other track in the same chunk, not just
    the one that was already there. duplicates=True skips that server-side check."""
    yt = _FakePlaylistsClient()

    ytmusic_client.add_tracks(yt, "playlist-id", ["v1"])  # type: ignore[arg-type]

    assert yt.add_duplicates_flags == [True]


def test_add_tracks_raises_when_ytmusic_reports_failure():
    """ytmusicapi returns an error response here instead of raising, on failure."""
    yt = _FakePlaylistsClient(add_result={"error": "SERVER_ERROR"})

    with pytest.raises(YTMusicError, match="rejected adding tracks"):
        ytmusic_client.add_tracks(yt, "playlist-id", ["v1"])  # type: ignore[arg-type]


def test_add_tracks_raises_on_the_real_duplicates_confirm_dialog_shape():
    """Regression test for the exact STATUS_FAILED/'Duplicates' response YT Music sends back for
    an already-present video with duplicates=False — should this ever regress to that flag, it
    must still be surfaced as a failure rather than silently dropping the whole chunk."""
    duplicates_confirm_dialog = {
        "status": "STATUS_FAILED",
        "actions": [
            {
                "confirmDialogEndpoint": {
                    "content": {
                        "confirmDialogRenderer": {
                            "title": {"runs": [{"text": "Duplicates"}]},
                            "dialogMessages": [
                                {"runs": [{"text": "One or more of the tracks are already in your playlist"}]}
                            ],
                        }
                    }
                }
            }
        ],
    }
    yt = _FakePlaylistsClient(add_result=duplicates_confirm_dialog)

    with pytest.raises(YTMusicError, match="rejected adding tracks"):
        ytmusic_client.add_tracks(yt, "playlist-id", ["P3OpfXS-iYg", "-7zh_xXVdAs"])  # type: ignore[arg-type]


def test_get_playlist_tracks_returns_the_tracks_key_with_no_page_limit():
    tracks = [{"videoId": "v1", "setVideoId": "s1"}, {"videoId": "v2", "setVideoId": "s2"}]
    yt = _FakePlaylistsClient(playlist_tracks=tracks)

    result = ytmusic_client.get_playlist_tracks(yt, "playlist-id")  # type: ignore[arg-type]

    assert result == tracks


def test_remove_tracks_chunks_large_lists_to_stay_under_the_request_size_limit():
    yt = _FakePlaylistsClient()
    tracks = [{"videoId": f"v{i}", "setVideoId": f"s{i}"} for i in range(120)]

    ytmusic_client.remove_tracks(yt, "playlist-id", tracks)  # type: ignore[arg-type]

    assert [len(chunk) for _pid, chunk in yt.removed] == [50, 50, 20]
    assert all(pid == "playlist-id" for pid, _chunk in yt.removed)


def test_remove_tracks_is_a_noop_for_an_empty_list():
    yt = _FakePlaylistsClient()

    ytmusic_client.remove_tracks(yt, "playlist-id", [])  # type: ignore[arg-type]

    assert yt.removed == []


def test_run_setup_raises_systemexit_when_ytmusicapi_rejects_the_headers(tmp_path, isolated_auth_file, monkeypatch):
    def _blow_up(*a, **k):
        raise YTMusicUserError("nope")

    monkeypatch.setattr(ytmusic_client, "setup", _blow_up)
    headers_file = _fake_headers_file(tmp_path, {"cookie": "__Secure-3PAPISID=deadbeef", "x-goog-authuser": "0"})

    with pytest.raises(SystemExit):
        ytmusic_client.run_setup(from_file=headers_file)


def test_save_auth_headers_saves_headers_ytmusicapi_accepts_as_browser_auth(isolated_auth_file):
    """`save_auth_headers` is the shared helper behind both `run_setup` (CLI) and the
    Streamlit UI's auth form — same regression coverage as run_setup's version above."""
    ytmusic_client.save_auth_headers("__Secure-3PAPISID=deadbeef; SID=fake", "0")

    saved = json.loads(isolated_auth_file.read_text())
    assert "authorization" in saved
    assert "SAPISIDHASH" in saved["authorization"]

    yt = YTMusic(str(isolated_auth_file))
    assert yt.auth_type == AuthType.BROWSER


def test_save_auth_headers_raises_value_error_when_a_value_is_missing(isolated_auth_file):
    with pytest.raises(ValueError, match="required"):
        ytmusic_client.save_auth_headers("SID=fake", "")

    assert not isolated_auth_file.exists()


def test_save_auth_headers_raises_auth_error_when_ytmusicapi_rejects_the_headers(isolated_auth_file, monkeypatch):
    def _blow_up(*a, **k):
        raise YTMusicUserError("nope")

    monkeypatch.setattr(ytmusic_client, "setup", _blow_up)

    with pytest.raises(ytmusic_client.YTMusicAuthError, match="nope"):
        ytmusic_client.save_auth_headers("SID=fake", "0")


def test_parse_headers_block_reads_cookie_and_authuser_from_a_full_header_dump():
    text = "cookie: SID=fake\nx-goog-authuser: 1\nuser-agent: Mozilla/5.0\n"

    cookie, authuser = ytmusic_client.parse_headers_block(text)

    assert cookie == "SID=fake"
    assert authuser == "1"


def test_parse_headers_block_returns_empty_strings_when_keys_are_absent():
    assert ytmusic_client.parse_headers_block("user-agent: Mozilla/5.0") == ("", "")
