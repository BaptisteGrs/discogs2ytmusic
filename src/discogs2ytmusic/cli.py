"""The Typer app: a thin command layer over `scan_engine`/`sync_engine`/`store`/`ytmusic_client`.

Each command wires those modules together for one step of the scan → cache → match → push
pipeline (see CLAUDE.md's Architecture section) and does its own console output/confirmation
prompts; the actual logic lives in the modules it calls, so the same logic is reusable from
`app.py`. `ui` launches the Streamlit app as a subprocess.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table
from ytmusicapi import YTMusic

from . import dummy_library, scan_engine, store, sync_engine, ytmusic_client
from .config import Config
from .discogs import DiscogsClient, DiscogsError

app = typer.Typer(no_args_is_help=True, add_completion=False)
auth_app = typer.Typer(no_args_is_help=True, help="Manage credentials.")
app.add_typer(auth_app, name="auth")

console = Console()


class Library(enum.StrEnum):
    """Which cache to point commands at (see `--library` in the app callback)."""

    real = "real"
    dummy = "dummy"


_active_library = Library.real  # set by the --library app callback below


@app.callback()
def cli_options(
    library: Library = typer.Option(
        Library.real,
        "--library",
        help="'real' uses your Discogs collection (default). 'dummy' uses a small bundled "
        "15-track test collection in a separate cache — no Discogs auth needed for `scan`, "
        "and it never touches your real cache. Only works from a full repo checkout.",
    ),
) -> None:
    """Sync your Discogs collection to YouTube Music playlists."""
    global _active_library
    _active_library = library
    if library == Library.dummy:
        # Keep it a sibling of whatever cache file is active, so this also respects a
        # cache location overridden for tests rather than always pointing at the real one.
        store.CACHE_DB = store.CACHE_DB.parent / "dummy_cache.sqlite3"


@auth_app.command("discogs")
def auth_discogs(
    token: str = typer.Option(..., prompt=True, hide_input=True, help="Discogs personal access token"),
    username: str | None = typer.Option(None, help="Discogs username (auto-detected from token if omitted)"),
) -> None:
    """Save Discogs credentials and verify them."""
    client = DiscogsClient(token)
    try:
        identity = client.identity()
    except DiscogsError as e:
        console.print(f"[red]Could not verify token:[/red] {e}")
        raise typer.Exit(1) from e
    resolved_username = username or identity.get("username")
    cfg = Config(discogs_token=token, discogs_username=resolved_username)
    cfg.save()
    console.print(f"[green]Authenticated as {resolved_username}.[/green]")


@auth_app.command("ytmusic")
def auth_ytmusic(
    from_file: Path | None = typer.Option(
        None,
        "--from-file",
        exists=True,
        dir_okay=False,
        help="Read 'cookie' and 'x-goog-authuser' from a text file instead of an interactive prompt "
        "(a file with lines like 'cookie: ...' and 'x-goog-authuser: 0').",
    ),
) -> None:
    """Link your YT Music account (two values copied from a browser DevTools request).

    Also available in the Streamlit app's sidebar, for a UI-only workflow.
    """
    ytmusic_client.run_setup(from_file=from_file)


@app.command(name="set-playlist-prefix")
def set_playlist_prefix(
    prefix: str = typer.Argument(..., help="Prefix used when naming playlists pushed to YT Music."),
) -> None:
    """Set the prefix used when naming playlists pushed to YT Music (default: "Discogs -").

    Also available in the Streamlit app, on the YT Music connection page.
    """
    cfg = Config.load()
    cfg.playlist_name_prefix = prefix
    cfg.save()
    console.print(f"[green]Playlist name prefix set to '{prefix}'.[/green]")


def _load_discogs_client() -> tuple[DiscogsClient, str]:
    cfg = Config.load()
    if not cfg.discogs_token or not cfg.discogs_username:
        console.print("[red]Not authenticated with Discogs.[/red] Run: discogs2ytmusic auth discogs")
        raise typer.Exit(1)
    return DiscogsClient(cfg.discogs_token), cfg.discogs_username


def _scan_dummy() -> None:
    try:
        releases = dummy_library.load_releases()
    except dummy_library.DummyLibraryUnavailable as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    with store.connect() as conn:
        for r in releases:
            store.upsert_release(
                conn,
                r["release_id"],
                r["artist"],
                r["title"],
                r["styles"],
                r["genres"],
                year=r.get("year"),
                labels=r.get("labels", []),
                videos=r.get("videos", []),
            )
            store.replace_tracks(
                conn,
                r["release_id"],
                [(t["position"], t["title"], t["duration"], t.get("discogs_artist")) for t in r["tracklist"]],
            )
    console.print(f"[green]Loaded dummy library ({len(releases)} releases) into {store.CACHE_DB}[/green]")


@app.command()
def scan(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch collection and tracklists from Discogs instead of using the cache"
    ),
) -> None:
    """Fetch your Discogs collection (with styles + tracklists) into the local cache and show a breakdown by style."""
    if _active_library == Library.dummy:
        _scan_dummy()
        _print_style_breakdown()
        return

    client, username = _load_discogs_client()

    with store.connect() as conn, Progress(console=console) as progress:
        task = progress.add_task("Fetching collection...", total=None)
        basics = []
        for item in client.iter_collection_basic(username):
            basics.append(item)
            progress.update(task, description=f"Fetching collection... ({len(basics)} releases)")
        progress.update(task, total=len(basics), completed=len(basics))

        task2 = progress.add_task("Fetching tracklists...", total=len(basics))
        failed = 0
        for item in basics:
            try:
                scan_engine.scan_release(conn, client, item, refresh)
            except DiscogsError as e:
                # A release Discogs can't return anymore (404) — merged into another release
                # id, or pulled from the database entirely. Skip it rather than aborting the
                # whole scan and losing every release already committed before it.
                failed += 1
                console.print(f"[yellow]Skipping a release Discogs couldn't return: {e}[/yellow]")
            progress.advance(task2)

    if failed:
        console.print(f"[yellow]Skipped {failed} release(s) Discogs couldn't return.[/yellow]")
    _print_style_breakdown()


def _print_style_breakdown() -> None:
    with store.connect() as conn:
        style_counts: dict[str, int] = defaultdict(int)
        for release, tracks in store.iter_releases_with_tracks(conn):
            for style in json.loads(release["styles"]) or ["(no style tag)"]:
                style_counts[style] += max(len(tracks), 1)

    table = Table(title="Releases by style (track count)")
    table.add_column("Style")
    table.add_column("Tracks", justify="right")
    for style, count in sorted(style_counts.items(), key=lambda kv: -kv[1]):
        table.add_row(style, str(count))
    console.print(table)


def _match_by_style(conn: sqlite3.Connection, yt: YTMusic, style: list[str] | None) -> dict[str, list[tuple[str, str]]]:
    """Ensure every track is matched, grouped by Discogs style tag. Never touches YT Music playlists."""
    by_style: dict[str, list[tuple[str, str]]] = defaultdict(list)  # style -> [(artist, title)]
    releases_with_tracks = []

    for release, tracks in store.iter_releases_with_tracks(conn):
        styles = json.loads(release["styles"]) or []
        if style:
            styles = [s for s in styles if s in style]
        if not styles:
            continue
        releases_with_tracks.append((release, tracks))
        for s in styles:
            for _track_id, artist, track_title in store.effective_track_queries(release, tracks):
                by_style[s].append((artist, track_title))

    if not by_style:
        return by_style

    # Each release is matched once regardless of how many (filtered) styles it belongs
    # to, so size the progress bar to that — not the by_style totals below, which count
    # a track once per matching style.
    total_tracks = sum(len(store.effective_track_queries(r, t)) for r, t in releases_with_tracks)
    with Progress(console=console) as progress:
        task = progress.add_task("Matching tracks on YouTube...", total=total_tracks)
        sync_engine.ensure_matches(conn, yt, releases_with_tracks, on_track_done=lambda: progress.advance(task))

    return by_style


def _video_ids_by_style(conn: sqlite3.Connection, by_style: dict[str, list[tuple[str, str]]]) -> dict[str, list[str]]:
    style_video_ids: dict[str, list[str]] = {}
    for s, track_list in by_style.items():
        video_ids: list[str] = []
        seen = set()
        for artist, title in track_list:
            if (artist, title) in seen:
                continue
            seen.add((artist, title))
            match = store.get_match(conn, artist, title)
            if match is not None and match["video_id"]:
                video_ids.append(match["video_id"])
        style_video_ids[s] = video_ids
    return style_video_ids


@app.command()
def sync(
    style: list[str] | None = typer.Option(
        None, "--style", help="Limit to specific style tag(s). Repeatable. Defaults to all styles."
    ),
    refresh_collection: bool = typer.Option(
        False, "--refresh", help="Re-fetch the Discogs collection first (equivalent to running `scan --refresh`)."
    ),
) -> None:
    """Match every track to a YouTube video and preview matched/total counts per Discogs style tag.

    This only searches and caches YouTube/YT Music matches — it never creates or modifies
    anything on your YT Music account. Use the Playlists tab to build and review a playlist,
    then push it with its own Sync button.
    """
    if refresh_collection:
        scan(refresh=True)

    yt = ytmusic_client.get_client(authenticated=False)

    with store.connect() as conn:
        by_style = _match_by_style(conn, yt, style)
        if not by_style:
            console.print("[yellow]No releases found for the given style filter. Run `scan` first?[/yellow]")
            raise typer.Exit(0)

        style_video_ids = _video_ids_by_style(conn, by_style)

        table = Table(title="Match preview")
        table.add_column("Style")
        table.add_column("Matched", justify="right")
        table.add_column("Total", justify="right")
        for s, track_list in by_style.items():
            table.add_row(s, str(len(style_video_ids[s])), str(len(track_list)))
        console.print(table)


@app.command()
def rematch(
    style: list[str] | None = typer.Option(
        None, "--style", help="Limit to specific style tag(s). Repeatable. Defaults to all styles."
    ),
    include_manual: bool = typer.Option(
        False, "--include-manual", help="Also clear manually-corrected matches (normally preserved)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt (for scripting)."),
) -> None:
    """Clear cached YouTube matches and re-match everything from scratch.

    Use this to backfill fields added to the match cache after tracks were already matched
    (e.g. Channel) — a normal `sync` skips anything already cached, so it never picks those up
    on its own. Also re-fetches the Discogs collection first (like `scan --refresh`), so
    Discogs-embedded videos are picked up too. Does not touch your real YT Music account —
    no playlists are created or modified.

    Manually-corrected matches (via `correct`) are preserved by default — pass --include-manual
    to clear those too.
    """
    with store.connect() as conn:
        n_matches = store.count_matches(conn)
        n_manual = store.count_manual_matches(conn)
    n_to_clear = n_matches if include_manual else n_matches - n_manual

    warning = (
        f"[bold yellow]Warning:[/bold yellow] this will delete {n_to_clear} cached match(es) and "
        "re-search every track from scratch, re-fetching your Discogs collection first. This can take "
        "a while and makes a lot of YouTube/YT Music requests. It will NOT touch your real YT Music account."
    )
    if not include_manual and n_manual:
        warning += f" {n_manual} manually-corrected match(es) will be kept untouched."
    console.print(warning)
    if not yes and not typer.confirm("Continue?"):
        console.print("Aborted.")
        raise typer.Exit(0)

    with store.connect() as conn:
        n_cleared = store.clear_all_matches(conn, include_manual=include_manual)
        conn.commit()
    kept_note = "" if include_manual else f" ({n_manual} manual correction(s) kept)"
    console.print(f"[green]Cleared {n_cleared} cached match(es){kept_note}.[/green]")

    scan(refresh=True)
    sync(style=style, refresh_collection=False)


def _parse_video_id(value: str) -> str:
    try:
        return ytmusic_client.parse_video_id(value)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@app.command()
def correct(
    match_id: int = typer.Argument(..., help="The match's surrogate id (`matches.id` in the sqlite cache)."),
    video_id: str | None = typer.Option(
        None, "--video-id", help="The correct video id, or a full YouTube/YT Music URL, to use for this match."
    ),
    reject: bool = typer.Option(
        False, "--reject", help="Mark explicitly as no-match — won't be auto-searched again, shown as unmatched."
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Forget this cached result so the next `sync` searches it again from scratch."
    ),
) -> None:
    """Manually fix one cached YouTube match, addressed by its `matches.id` in the sqlite cache.

    Pass exactly one of --video-id, --reject, or --clear. Also available as the "YouTube link"
    column in the Streamlit app, which doesn't require knowing the match id.
    """
    modes_given = sum([video_id is not None, reject, clear])
    if modes_given != 1:
        console.print("[red]Pass exactly one of --video-id, --reject, or --clear.[/red]")
        raise typer.Exit(1)

    with store.connect() as conn:
        row = store.get_match_by_id(conn, match_id)
        if row is None:
            console.print(f"[red]No cached match with id {match_id}.[/red]")
            raise typer.Exit(1)

        if clear:
            store.delete_match(conn, match_id)
            console.print(
                f"[green]Cleared match {match_id} ('{row['query_key']}') — "
                "it will be searched again on the next sync.[/green]"
            )
            return

        if reject:
            store.update_match(conn, match_id, video_id=None, video_title=None, source="manual")
            console.print(f"[green]Marked match {match_id} ('{row['query_key']}') as no-match.[/green]")
            return

        assert video_id is not None  # the only remaining mode, per the modes_given check above
        resolved_id = _parse_video_id(video_id)
        store.update_match(conn, match_id, video_id=resolved_id, video_title=None, source="manual")
        console.print(f"[green]Corrected match {match_id} ('{row['query_key']}') → {resolved_id}[/green]")


@app.command(name="fix-artist")
def fix_artist(
    track_id: int = typer.Argument(..., help="The track's surrogate id (`tracks.id` in the sqlite cache)."),
    artist: str | None = typer.Option(
        None,
        "--artist",
        help=(
            "Artist name to search YouTube with for this track, instead of the release's "
            "(possibly multi-credit) artist string."
        ),
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the override and fall back to the release's artist again."
    ),
) -> None:
    """Override the artist name used to build the YouTube search query for one track.

    Useful when a release is credited to multiple artists (Discogs joins them with
    commas) but a given track is really just one of them — the combined string can
    dilute the fuzzy match enough to miss or pick the wrong video. Pass exactly one
    of --artist or --clear. Takes effect on the next `sync` (it searches under the
    new artist/title pair, which won't be cached yet); it doesn't touch any existing
    cached match for this track — `correct --clear` that separately if needed. Also
    available as the "Track Artist" column in the Streamlit app.

    Note: this override lives on the track row, so it's lost if that release's
    tracklist is later replaced (`scan --refresh`).
    """
    if (artist is not None) == clear:
        console.print("[red]Pass exactly one of --artist or --clear.[/red]")
        raise typer.Exit(1)

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        if track is None:
            console.print(f"[red]No track with id {track_id}.[/red]")
            raise typer.Exit(1)

        store.set_track_search_artist(conn, track_id, artist)

    if clear:
        console.print(f"[green]Cleared artist override for track {track_id} ('{track['title']}').[/green]")
    else:
        console.print(f"[green]Track {track_id} ('{track['title']}') will now be searched as '{artist}'.[/green]")


@app.command(name="fix-style")
def fix_style(
    track_id: int = typer.Argument(..., help="The track's surrogate id (`tracks.id` in the sqlite cache)."),
    style: list[str] | None = typer.Option(
        None, "--style", help="Style tag to use instead of Discogs' own. Repeatable for more than one."
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the override and fall back to the release's styles again."
    ),
) -> None:
    """Override the style tags used for one track, like `fix-artist` does for artist.

    Discogs only reports styles per-release, so a release tagged e.g. "Tech House,
    Downtempo, Breaks" gives no way to know which track is which from the API alone —
    this lets a user correct that per track. Pass exactly one of --style (repeatable)
    or --clear. Also available as the "Styles" column in the Streamlit app.

    Note: this override lives on the track row, so it's lost if that release's
    tracklist is later replaced (`scan --refresh`).
    """
    if bool(style) == clear:
        console.print("[red]Pass --style (one or more times) or --clear.[/red]")
        raise typer.Exit(1)

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        if track is None:
            console.print(f"[red]No track with id {track_id}.[/red]")
            raise typer.Exit(1)

        store.set_track_styles_override(conn, track_id, style)

    if clear:
        console.print(f"[green]Cleared style override for track {track_id} ('{track['title']}').[/green]")
    else:
        assert style is not None  # the only remaining mode, per the modes check above
        console.print(f"[green]Track {track_id} ('{track['title']}') styles set to: {', '.join(style)}[/green]")


@app.command(name="fix-genre")
def fix_genre(
    track_id: int = typer.Argument(..., help="The track's surrogate id (`tracks.id` in the sqlite cache)."),
    genre: list[str] | None = typer.Option(
        None, "--genre", help="Genre tag to use instead of Discogs' own. Repeatable for more than one."
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Remove the override and fall back to the release's genres again."
    ),
) -> None:
    """Override the genre tags used for one track, like `fix-artist` does for artist.

    Pass exactly one of --genre (repeatable) or --clear. Same per-track rationale as
    `fix-style` — see its docstring. Also available as the "Genres" column in the
    Streamlit app.

    Note: this override lives on the track row, so it's lost if that release's
    tracklist is later replaced (`scan --refresh`).
    """
    if bool(genre) == clear:
        console.print("[red]Pass --genre (one or more times) or --clear.[/red]")
        raise typer.Exit(1)

    with store.connect() as conn:
        track = store.get_track(conn, track_id)
        if track is None:
            console.print(f"[red]No track with id {track_id}.[/red]")
            raise typer.Exit(1)

        store.set_track_genres_override(conn, track_id, genre)

    if clear:
        console.print(f"[green]Cleared genre override for track {track_id} ('{track['title']}').[/green]")
    else:
        assert genre is not None  # the only remaining mode, per the modes check above
        console.print(f"[green]Track {track_id} ('{track['title']}') genres set to: {', '.join(genre)}[/green]")


@app.command()
def ui() -> None:
    """Launch the browsable/editable web UI (Streamlit)."""
    app_path = Path(__file__).parent / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path)])


def main() -> None:
    """Entry point registered as the `discogs2ytmusic` console script."""
    app()


if __name__ == "__main__":
    main()
