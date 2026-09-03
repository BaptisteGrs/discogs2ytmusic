from __future__ import annotations

from ytmusicapi import YTMusic, setup

from .config import YTMUSIC_AUTH_FILE, ensure_dirs


def is_authenticated() -> bool:
    return YTMUSIC_AUTH_FILE.exists()


def run_setup() -> None:
    """Interactive one-time setup: paste browser-copied request headers.

    Mirrors ytmusicapi's own CLI (`ytmusicapi browser`), but writes to our
    config dir instead of the current working directory.
    """
    ensure_dirs()
    print(
        "To authenticate, open music.youtube.com in your browser while logged in,\n"
        "open DevTools > Network, click any request to a *music.youtube.com* API\n"
        "(e.g. 'browse'), and copy its request headers.\n"
        "See: https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html\n"
    )
    setup(filepath=str(YTMUSIC_AUTH_FILE), headers_raw=None)
    print(f"Saved YT Music auth to {YTMUSIC_AUTH_FILE}")


def get_client(authenticated: bool = True) -> YTMusic:
    if authenticated:
        if not is_authenticated():
            raise RuntimeError(
                "Not authenticated with YT Music yet. Run: discogs2ytmusic auth ytmusic"
            )
        return YTMusic(str(YTMUSIC_AUTH_FILE))
    return YTMusic()


def get_or_create_playlist(yt: YTMusic, name: str, description: str = "") -> str:
    existing = yt.get_library_playlists(limit=200)
    for pl in existing:
        if pl.get("title") == name:
            return pl["playlistId"]
    return yt.create_playlist(name, description)


def add_tracks(yt: YTMusic, playlist_id: str, video_ids: list[str]) -> None:
    if not video_ids:
        return
    # YT Music silently ignores duplicates already in the playlist, but chunk
    # to stay well under request size limits for large collections.
    CHUNK = 50
    for i in range(0, len(video_ids), CHUNK):
        chunk = video_ids[i : i + CHUNK]
        yt.add_playlist_items(playlist_id, chunk, duplicates=False)
