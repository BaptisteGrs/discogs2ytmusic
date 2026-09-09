from __future__ import annotations

import json
from dataclasses import dataclass
from getpass import getpass
from urllib.parse import parse_qs, urlparse

from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.auth.oauth import OAuthToken
from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient
from ytmusicapi.auth.oauth.token import Token
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from .config import YTMUSIC_AUTH_FILE, Config, ensure_dirs, write_private_file

OAUTH_SETUP_STEPS: tuple[str, ...] = (
    "Go to console.cloud.google.com and create a project (or reuse one you already have)",
    "APIs & Services > Library: enable the 'YouTube Data API v3'",
    "APIs & Services > Credentials > Create Credentials > OAuth client ID, type 'TVs and Limited Input devices'",
    "Copy the resulting Client ID and Client Secret",
)

# The CLI's interactive prompt prints this as one paragraph; the Streamlit UI renders
# OAUTH_SETUP_STEPS itself as a numbered list — both read from the one list of steps.
OAUTH_SETUP_INSTRUCTIONS = (
    "To authenticate:\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(OAUTH_SETUP_STEPS, 1)) + "\n"
)


class YTMusicAuthError(Exception):
    """Raised when Google rejects an OAuth client, device code, or sign-in attempt."""


@dataclass(frozen=True)
class DeviceCode:
    """A pending device-code sign-in: show `verification_url`/`user_code` to the user.

    `device_code` is opaque (not shown to the user) — hand it back to `complete_oauth_flow`
    once they've confirmed access in their browser.
    """

    verification_url: str
    user_code: str
    device_code: str


def is_authenticated() -> bool:
    """Whether a complete OAuth token has already been saved.

    Checks the file actually holds every field an OAuth token needs (rather than just
    existing), so a leftover file from the old cookie-based auth this replaced doesn't
    read as "connected".
    """
    if not YTMUSIC_AUTH_FILE.exists():
        return False
    try:
        data = json.loads(YTMUSIC_AUTH_FILE.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and all(key in data for key in Token.members())


def save_oauth_client(client_id: str, client_secret: str) -> None:
    """Persist the user's own Google OAuth client id/secret.

    These identify the Google Cloud project the user created for this tool (see
    `OAUTH_SETUP_STEPS`) — they're needed on every `get_client()` call, not just at initial
    setup, because refreshing an expiring access token requires them.

    Raises:
        ValueError: if either value is empty.
    """
    if not client_id or not client_secret:
        raise ValueError("Both 'client_id' and 'client_secret' are required.")
    cfg = Config.load()
    cfg.ytmusic_oauth_client_id = client_id
    cfg.ytmusic_oauth_client_secret = client_secret
    cfg.save()


def load_oauth_client() -> tuple[str, str] | None:
    """Return the saved `(client_id, client_secret)`, or None if none has been saved yet."""
    cfg = Config.load()
    if not cfg.ytmusic_oauth_client_id or not cfg.ytmusic_oauth_client_secret:
        return None
    return cfg.ytmusic_oauth_client_id, cfg.ytmusic_oauth_client_secret


def begin_oauth_flow(client_id: str, client_secret: str) -> DeviceCode:
    """Start the OAuth device-code flow: request a code for the user to confirm in a browser.

    Raises:
        YTMusicAuthError: if `client_id`/`client_secret` are rejected or the request fails.
    """
    credentials = OAuthCredentials(client_id, client_secret)
    try:
        code = credentials.get_code()
    except (BadOAuthClient, UnauthorizedOAuthClient, YTMusicServerError) as e:
        raise YTMusicAuthError(str(e)) from e
    return DeviceCode(
        verification_url=code["verification_url"],
        user_code=code["user_code"],
        device_code=code["device_code"],
    )


def complete_oauth_flow(client_id: str, client_secret: str, device_code: str) -> None:
    """Finish the device-code flow after the user has confirmed access in their browser.

    Raises:
        YTMusicAuthError: if the user hasn't finished signing in yet (retry after they have),
            the code expired, access was denied, or the request otherwise failed.
    """
    ensure_dirs()
    credentials = OAuthCredentials(client_id, client_secret)
    try:
        raw = credentials.token_from_code(device_code)
    except (BadOAuthClient, UnauthorizedOAuthClient, YTMusicServerError) as e:
        raise YTMusicAuthError(str(e)) from e

    error = raw.get("error")
    if error == "authorization_pending":
        raise YTMusicAuthError("Not finished yet — complete sign-in in the browser tab, then try again.")
    if error:
        raise YTMusicAuthError(f"Google rejected the sign-in: {error}")

    token = OAuthToken(
        scope=raw["scope"],
        token_type=raw["token_type"],
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        expires_in=raw.get("refresh_token_expires_in", raw["expires_in"]),
    )
    token.update(raw)  # sets expires_at from expires_in relative to now
    write_private_file(YTMUSIC_AUTH_FILE, token.as_json())


def run_setup(client_id: str | None = None, client_secret: str | None = None) -> None:
    """One-time (or repeat) CLI device-code sign-in.

    `client_id`/`client_secret` identify the user's own Google Cloud OAuth client (see
    `OAUTH_SETUP_STEPS`); once saved they're reused on subsequent calls so re-authenticating
    (e.g. after revoking access at https://myaccount.google.com/permissions) only needs
    `discogs2ytmusic auth ytmusic` with no arguments. The Streamlit UI offers an equivalent
    flow, split across two button clicks (`begin_oauth_flow`/`complete_oauth_flow`) since it
    can't block on terminal input like this can.
    """
    if client_id and client_secret:
        save_oauth_client(client_id, client_secret)
    else:
        saved = load_oauth_client()
        if saved is None:
            print(OAUTH_SETUP_INSTRUCTIONS)
            client_id = input("client_id: ").strip()
            client_secret = getpass("client_secret: ").strip()
            if not client_id or not client_secret:
                print("Both 'client_id' and 'client_secret' are required — aborting.")
                raise SystemExit(1)
            save_oauth_client(client_id, client_secret)
        else:
            client_id, client_secret = saved

    try:
        code = begin_oauth_flow(client_id, client_secret)
    except YTMusicAuthError as e:
        print(f"Could not start sign-in: {e}")
        raise SystemExit(1) from e

    print(f"Go to {code.verification_url} and enter this code: {code.user_code}")
    input("Press Enter once you've finished signing in there (Ctrl-C to abort)...")

    try:
        complete_oauth_flow(client_id, client_secret, code.device_code)
    except YTMusicAuthError as e:
        print(f"Could not authenticate: {e}")
        raise SystemExit(1) from e

    print(f"Saved YT Music auth to {YTMUSIC_AUTH_FILE}")


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


def _secure_auth_file() -> None:
    """Re-apply owner-only permissions to the saved auth file.

    `ytmusicapi`'s `RefreshingToken` silently rewrites this file (via plain `open(path, "w")`,
    no chmod) whenever it refreshes an expiring access token — which can happen on any
    authenticated request, not just at `get_client()`. Called after every operation that
    might trigger that, so a refresh never leaves the file world-readable for longer than
    the gap until the next call here.
    """
    if YTMUSIC_AUTH_FILE.exists():
        YTMUSIC_AUTH_FILE.chmod(0o600)


def get_client(authenticated: bool = True) -> YTMusic:
    """Build a YTMusic client.

    Args:
        authenticated: If True (needed to create/modify playlists), require and use the
            saved OAuth token. If False, use an anonymous client — enough for search.

    Raises:
        RuntimeError: if `authenticated` is True but no OAuth token/client is saved yet, or
            the saved token is missing/malformed and needs to be regenerated.
    """
    if not authenticated:
        return YTMusic()

    if not is_authenticated():
        raise RuntimeError(
            "Not authenticated with YT Music yet. Run: discogs2ytmusic auth ytmusic (or use the app's YT Music page)."
        )
    client = load_oauth_client()
    if client is None:
        raise RuntimeError(
            "Saved YT Music token is missing its OAuth client id/secret. Re-run: "
            "discogs2ytmusic auth ytmusic (or use the app's YT Music page)."
        )
    client_id, client_secret = client
    try:
        yt = YTMusic(str(YTMUSIC_AUTH_FILE), oauth_credentials=OAuthCredentials(client_id, client_secret))
    except YTMusicUserError as e:
        raise RuntimeError(
            "Saved YT Music auth is missing or malformed. Re-run: discogs2ytmusic auth ytmusic "
            "(or use the app's YT Music page)."
        ) from e
    _secure_auth_file()
    return yt


def get_or_create_playlist(yt: YTMusic, name: str, description: str = "") -> str:
    """Return the id of the existing playlist named `name`, creating it if none exists."""
    existing = yt.get_library_playlists(limit=200)
    _secure_auth_file()
    for pl in existing:
        if pl.get("title") == name:
            return pl["playlistId"]
    result = yt.create_playlist(name, description)
    _secure_auth_file()
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
        _secure_auth_file()
