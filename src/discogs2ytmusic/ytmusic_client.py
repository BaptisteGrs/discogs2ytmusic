from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicError, YTMusicUserError

from .config import YTMUSIC_AUTH_FILE, ensure_dirs

SETUP_STEPS: tuple[str, ...] = (
    "Open music.youtube.com in your browser, logged in",
    "Open DevTools > Network",
    "Click a request to a music.youtube.com API (e.g. 'browse')",
    "Find 'cookie' and 'x-goog-authuser' under its Request Headers",
)

# The CLI's interactive prompt prints this as one paragraph; the Streamlit UI renders
# SETUP_STEPS itself as a numbered list — both read from the one list of steps.
SETUP_INSTRUCTIONS = "To authenticate:\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(SETUP_STEPS, 1)) + "\n"

# ytmusicapi only classifies saved headers as browser/cookie auth (as opposed to defaulting
# to expecting an OAuth token, and raising) if an `authorization` header containing this
# marker is already present at load time — see ytmusicapi.auth.auth_parse.determine_auth_type.
# The actual value doesn't matter: for real requests it recomputes a fresh SAPISIDHASH from
# the cookie/origin on every call (ytmusicapi.YTMusicBase.headers), so this placeholder is
# never sent anywhere — it only exists to make `determine_auth_type` pick the right branch.
_SAPISIDHASH_MARKER = "SAPISIDHASH 0_0"


class YTMusicAuthError(Exception):
    """Raised when ytmusicapi rejects the cookie/x-goog-authuser headers being saved."""


def is_authenticated() -> bool:
    """Whether YT Music auth headers have already been saved."""
    return YTMUSIC_AUTH_FILE.exists()


def save_auth_headers(cookie: str, authuser: str) -> None:
    """Validate and persist YT Music `cookie`/`x-goog-authuser` header values.

    Shared by the CLI's `auth ytmusic` command and the Streamlit UI's auth form so both
    write `YTMUSIC_AUTH_FILE` the same way, including the `_SAPISIDHASH_MARKER` needed for
    ytmusicapi to recognize the saved file as browser/cookie auth (see the comment above).

    Args:
        cookie: The `cookie` request header value copied from a music.youtube.com request.
        authuser: The `x-goog-authuser` request header value copied from the same request.

    Raises:
        ValueError: if either value is empty.
        YTMusicAuthError: if ytmusicapi rejects the resulting headers.
    """
    if not cookie or not authuser:
        raise ValueError("Both 'cookie' and 'x-goog-authuser' are required.")

    ensure_dirs()
    headers_raw = f"cookie: {cookie}\nx-goog-authuser: {authuser}\nauthorization: {_SAPISIDHASH_MARKER}"
    try:
        setup(filepath=str(YTMUSIC_AUTH_FILE), headers_raw=headers_raw)
    except YTMusicUserError as e:
        raise YTMusicAuthError(str(e)) from e
    YTMUSIC_AUTH_FILE.chmod(0o600)  # contains a live session cookie — owner-read/write only


def run_setup(from_file: Path | None = None) -> None:
    """One-time CLI setup: provide just the two request-header values that matter.

    ytmusicapi's own setup wants a full raw header block pasted in, but only
    `cookie` and `x-goog-authuser` are actually required (everything else it
    fills in with sane defaults) — so we only need those two, which is a much
    smaller/easier thing to copy out of DevTools. They can be typed at an
    interactive prompt, or read from a file (handy since pasting a long
    cookie value into a terminal prompt is fiddly). The Streamlit UI offers
    an equivalent form backed by the same `save_auth_headers`.
    """
    if from_file is not None:
        text = from_file.read_text()
        cookie, authuser = parse_headers_block(text)
    else:
        print(SETUP_INSTRUCTIONS)
        cookie = input("cookie: ").strip()
        authuser = input("x-goog-authuser: ").strip()

    try:
        save_auth_headers(cookie, authuser)
    except ValueError as e:
        print(
            "Could not find both 'cookie' and 'x-goog-authuser' values"
            + (f" in {from_file}" if from_file else "")
            + " — aborting."
        )
        raise SystemExit(1) from e
    except YTMusicAuthError as e:
        print(f"Could not authenticate: {e}")
        raise SystemExit(1) from e

    print(f"Saved YT Music auth to {YTMUSIC_AUTH_FILE}")
    if from_file is not None:
        print(f"You can now delete {from_file} — its contents were only needed for this one-time setup.")


def parse_headers_block(text: str) -> tuple[str, str]:
    """Pull cookie/x-goog-authuser values out of pasted header text.

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
            raise RuntimeError(
                "Not authenticated with YT Music yet. Run: discogs2ytmusic auth ytmusic "
                "(or use the app's YT Music page)."
            )
        try:
            return YTMusic(str(YTMUSIC_AUTH_FILE))
        except YTMusicUserError as e:
            raise RuntimeError(
                "Saved YT Music auth is missing or malformed (an older version of this tool could "
                "save auth headers ytmusicapi can't use for writes). Re-run: discogs2ytmusic auth ytmusic "
                "(or use the app's YT Music page)."
            ) from e
    return YTMusic()


def find_playlist(yt: YTMusic, name: str) -> str | None:
    """Return the id of the library playlist named `name`, or None if no such playlist exists."""
    existing = yt.get_library_playlists(limit=200)
    for pl in existing:
        if pl.get("title") == name:
            return str(pl["playlistId"])
    return None


def get_or_create_playlist(yt: YTMusic, name: str, description: str = "") -> tuple[str, bool]:
    """Return (playlist id, created) for the playlist named `name`, creating it if none exists.

    `created` is False when an existing playlist matching `name` was reused instead of a new one
    being made. Callers that treat the local track set as ground truth (see the Streamlit UI's
    sync confirmation) use this to warn before removing tracks from a pre-existing remote
    playlist that happens to share the generated name.
    """
    found = find_playlist(yt, name)
    if found is not None:
        return found, False
    result = yt.create_playlist(name, description)
    if not isinstance(result, str):
        # ytmusicapi returns an error dict here instead of raising, on failure.
        raise RuntimeError(f"Failed to create playlist {name!r}: {result}")
    return result, True


def add_tracks(yt: YTMusic, playlist_id: str, video_ids: list[str]) -> None:
    """Add videos to a playlist, chunked to stay under YT Music's request size limits.

    Raises:
        YTMusicError: If YT Music rejects a chunk (e.g. `add_playlist_items` returns an error
            response instead of raising) — surfaced instead of silently dropping tracks.
    """
    if not video_ids:
        return
    # With duplicates=False, ytmusicapi's own docs say a duplicate anywhere in the request
    # makes YT Music reject the *whole* chunk rather than just skip that one video — so a video
    # matched twice locally (e.g. two Discogs tracks resolved to the same YouTube video) would
    # silently block every other track in its chunk from being added. Dedupe first: sending the
    # same video twice in one call is meaningless anyway.
    deduped = list(dict.fromkeys(video_ids))
    CHUNK = 50
    for i in range(0, len(deduped), CHUNK):
        chunk = deduped[i : i + CHUNK]
        result = yt.add_playlist_items(playlist_id, chunk, duplicates=False)
        status = result.get("status", "") if isinstance(result, dict) else ""
        if "SUCCEEDED" not in status:
            raise YTMusicError(f"YT Music rejected adding tracks to playlist {playlist_id!r}: {result}")


def get_playlist_tracks(yt: YTMusic, playlist_id: str) -> list[dict[str, Any]]:
    """Return every track currently on a YT Music playlist (all pages, no limit).

    Each item includes `videoId` and `setVideoId` — `remove_tracks` needs both to identify
    which occurrence of a track to remove.
    """
    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks: list[dict[str, Any]] = playlist.get("tracks", [])
    return tracks


def remove_tracks(yt: YTMusic, playlist_id: str, tracks: list[dict[str, Any]]) -> None:
    """Remove tracks from a playlist, chunked to stay under YT Music's request size limits.

    Args:
        yt: Authenticated YTMusic client.
        playlist_id: The playlist to remove tracks from.
        tracks: Track items as returned by `get_playlist_tracks` — each must include `videoId`
            and `setVideoId` (YT Music needs both to identify the exact occurrence to remove).
    """
    if not tracks:
        return
    CHUNK = 50
    for i in range(0, len(tracks), CHUNK):
        chunk = tracks[i : i + CHUNK]
        yt.remove_playlist_items(playlist_id, chunk)
