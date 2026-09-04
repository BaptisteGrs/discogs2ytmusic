from __future__ import annotations

from pathlib import Path

from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicUserError

from .config import YTMUSIC_AUTH_FILE, ensure_dirs

SETUP_INSTRUCTIONS = (
    "To authenticate, open music.youtube.com in your browser while logged in,\n"
    "open DevTools > Network, click a request to a *music.youtube.com* API\n"
    "(e.g. 'browse'), open its Headers panel, and find these two values under\n"
    "'Request Headers': cookie, and x-goog-authuser.\n"
)


def is_authenticated() -> bool:
    return YTMUSIC_AUTH_FILE.exists()


def run_setup(from_file: Path | None = None) -> None:
    """One-time setup: provide just the two request-header values that matter.

    ytmusicapi's own setup wants a full raw header block pasted in, but only
    `cookie` and `x-goog-authuser` are actually required (everything else it
    fills in with sane defaults) — so we only need those two, which is a much
    smaller/easier thing to copy out of DevTools. They can be typed at an
    interactive prompt, or read from a file (handy since pasting a long
    cookie value into a terminal prompt is fiddly).
    """
    ensure_dirs()

    if from_file is not None:
        text = from_file.read_text()
        cookie, authuser = _parse_headers_file(text)
    else:
        print(SETUP_INSTRUCTIONS)
        cookie = input("cookie: ").strip()
        authuser = input("x-goog-authuser: ").strip()

    if not cookie or not authuser:
        print(
            "Could not find both 'cookie' and 'x-goog-authuser' values"
            + (f" in {from_file}" if from_file else "")
            + " — aborting."
        )
        raise SystemExit(1)

    headers_raw = f"cookie: {cookie}\nx-goog-authuser: {authuser}"
    try:
        setup(filepath=str(YTMUSIC_AUTH_FILE), headers_raw=headers_raw)
    except YTMusicUserError as e:
        print(f"Could not authenticate: {e}")
        raise SystemExit(1) from e
    YTMUSIC_AUTH_FILE.chmod(0o600)  # contains a live session cookie — owner-read/write only
    print(f"Saved YT Music auth to {YTMUSIC_AUTH_FILE}")
    if from_file is not None:
        print(f"You can now delete {from_file} — its contents were only needed for this one-time setup.")


def _parse_headers_file(text: str) -> tuple[str, str]:
    """Pull cookie/x-goog-authuser values out of a file.

    Accepts either just the two lines we ask for (`cookie: ...` and
    `x-goog-authuser: ...`), or a full raw header block copy-pasted from
    DevTools — only those two keys are read, everything else is ignored.
    """
    cookie = ""
    authuser = ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "cookie":
            cookie = value
        elif key == "x-goog-authuser":
            authuser = value
    return cookie, authuser


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
