from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicUserError

from .config import YTMUSIC_AUTH_FILE, ensure_dirs

SETUP_INSTRUCTIONS = (
    "To authenticate, open music.youtube.com in your browser while logged in,\n"
    "open DevTools > Network, click a request to a *music.youtube.com* API\n"
    "(e.g. 'browse'), open its Headers panel, and find these two values under\n"
    "'Request Headers': cookie, and x-goog-authuser.\n"
)

# ytmusicapi only classifies saved headers as browser/cookie auth (as opposed to defaulting
# to expecting an OAuth token, and raising) if an `authorization` header containing this
# marker is already present at load time — see ytmusicapi.auth.auth_parse.determine_auth_type.
# The actual value doesn't matter: for real requests it recomputes a fresh SAPISIDHASH from
# the cookie/origin on every call (ytmusicapi.YTMusicBase.headers), so this placeholder is
# never sent anywhere — it only exists to make `determine_auth_type` pick the right branch.
_SAPISIDHASH_MARKER = "SAPISIDHASH 0_0"


def is_authenticated() -> bool:
    """Whether `run_setup` has already saved YT Music auth headers."""
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

    headers_raw = f"cookie: {cookie}\nx-goog-authuser: {authuser}\nauthorization: {_SAPISIDHASH_MARKER}"
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


def parse_video_id(value: str) -> str:
    """Accept either a bare YouTube video id or a full watch URL (youtube.com, music.youtube.com, youtu.be)."""
    value = value.strip()
    if not value.startswith("http://") and not value.startswith("https://"):
        return value
    parsed = urlparse(value)
    if parsed.hostname and "youtu.be" in parsed.hostname:
        return parsed.path.strip("/")
    video_id = parse_qs(parsed.query).get("v", [None])[0]
    if not video_id:
        raise ValueError(f"Could not find a video id in URL: {value}")
    return video_id


def get_client(authenticated: bool = True) -> YTMusic:
    """Build a YTMusic client.

    Args:
        authenticated: If True (needed to create/modify playlists), require and use the
            saved auth headers. If False, use an anonymous client — enough for search.

    Raises:
        RuntimeError: if `authenticated` is True but `run_setup` hasn't been run yet, or the
            saved auth file predates the `_SAPISIDHASH_MARKER` fix and needs to be regenerated.
    """
    if authenticated:
        if not is_authenticated():
            raise RuntimeError("Not authenticated with YT Music yet. Run: discogs2ytmusic auth ytmusic")
        try:
            return YTMusic(str(YTMUSIC_AUTH_FILE))
        except YTMusicUserError as e:
            raise RuntimeError(
                "Saved YT Music auth is missing or malformed (an older version of this tool could "
                "save auth headers ytmusicapi can't use for writes). Re-run: discogs2ytmusic auth ytmusic"
            ) from e
    return YTMusic()


def get_or_create_playlist(yt: YTMusic, name: str, description: str = "") -> str:
    """Return the id of the existing playlist named `name`, creating it if none exists."""
    existing = yt.get_library_playlists(limit=200)
    for pl in existing:
        if pl.get("title") == name:
            return pl["playlistId"]
    result = yt.create_playlist(name, description)
    if not isinstance(result, str):
        # ytmusicapi returns an error dict here instead of raising, on failure.
        raise RuntimeError(f"Failed to create playlist {name!r}: {result}")
    return result


def add_tracks(yt: YTMusic, playlist_id: str, video_ids: list[str]) -> None:
    """Add videos to a playlist, chunked to stay under YT Music's request size limits."""
    if not video_ids:
        return
    # YT Music silently ignores duplicates already in the playlist, but chunk
    # to stay well under request size limits for large collections.
    CHUNK = 50
    for i in range(0, len(video_ids), CHUNK):
        chunk = video_ids[i : i + CHUNK]
        yt.add_playlist_items(playlist_id, chunk, duplicates=False)
