from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from . import matcher, store, ytmusic_client
from .config import Config
from .discogs import DiscogsClient, DiscogsError

app = typer.Typer(no_args_is_help=True, add_completion=False)
auth_app = typer.Typer(no_args_is_help=True, help="Manage credentials.")
app.add_typer(auth_app, name="auth")

console = Console()


@auth_app.command("discogs")
def auth_discogs(
    token: str = typer.Option(..., prompt=True, hide_input=True, help="Discogs personal access token"),
    username: Optional[str] = typer.Option(None, help="Discogs username (auto-detected from token if omitted)"),
):
    """Save Discogs credentials and verify them."""
    client = DiscogsClient(token)
    try:
        identity = client.identity()
    except DiscogsError as e:
        console.print(f"[red]Could not verify token:[/red] {e}")
        raise typer.Exit(1)
    resolved_username = username or identity.get("username")
    cfg = Config(discogs_token=token, discogs_username=resolved_username)
    cfg.save()
    console.print(f"[green]Authenticated as {resolved_username}.[/green]")


@auth_app.command("ytmusic")
def auth_ytmusic(
    from_file: Optional[Path] = typer.Option(
        None,
        "--from-file",
        exists=True,
        dir_okay=False,
        help="Read 'cookie' and 'x-goog-authuser' from a text file instead of an interactive prompt "
        "(a file with lines like 'cookie: ...' and 'x-goog-authuser: 0').",
    ),
):
    """Link your YT Music account (two values copied from a browser DevTools request)."""
    ytmusic_client.run_setup(from_file=from_file)


def _load_discogs_client() -> tuple[DiscogsClient, str]:
    cfg = Config.load()
    if not cfg.discogs_token or not cfg.discogs_username:
        console.print("[red]Not authenticated with Discogs.[/red] Run: discogs2ytmusic auth discogs")
        raise typer.Exit(1)
    return DiscogsClient(cfg.discogs_token), cfg.discogs_username


@app.command()
def scan(
    refresh: bool = typer.Option(False, "--refresh", help="Re-fetch collection and tracklists from Discogs instead of using the cache"),
):
    """Fetch your Discogs collection (with styles + tracklists) into the local cache and show a breakdown by style."""
    client, username = _load_discogs_client()

    with store.connect() as conn, Progress(console=console) as progress:
        task = progress.add_task("Fetching collection...", total=None)
        basics = []
        for item in client.iter_collection_basic(username):
            basics.append(item)
            progress.update(task, description=f"Fetching collection... ({len(basics)} releases)")
        progress.update(task, total=len(basics), completed=len(basics))

        task2 = progress.add_task("Fetching tracklists...", total=len(basics))
        for item in basics:
            info = item["basic_information"]
            release_id = info["id"]
            artist = ", ".join(a["name"] for a in info.get("artists", []))
            artist = re.sub(r"\s*\(\d+\)$", "", artist)  # strip Discogs disambiguation suffixes e.g. "Rush (2)"
            title = info.get("title", "")
            styles = info.get("styles", []) or []
            genres = info.get("genres", []) or []
            store.upsert_release(conn, release_id, artist, title, styles, genres)

            if refresh or not store.has_tracks(conn, release_id):
                tracks = client.get_release_tracklist(release_id)
                store.replace_tracks(
                    conn, release_id, [(t.position, t.title, t.duration) for t in tracks]
                )
            conn.commit()  # commit per-release so a crash/interrupt doesn't lose earlier progress
            progress.advance(task2)

    _print_style_breakdown()


def _print_style_breakdown() -> None:
    with store.connect() as conn:
        style_counts: dict[str, int] = defaultdict(int)
        for release, tracks in store.iter_releases_with_tracks(conn):
            import json as _json

            for style in _json.loads(release["styles"]) or ["(no style tag)"]:
                style_counts[style] += max(len(tracks), 1)

    table = Table(title="Releases by style (track count)")
    table.add_column("Style")
    table.add_column("Tracks", justify="right")
    for style, count in sorted(style_counts.items(), key=lambda kv: -kv[1]):
        table.add_row(style, str(count))
    console.print(table)


@app.command()
def sync(
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview matches without touching YT Music (default). Use --apply to actually create/update playlists."),
    style: Optional[list[str]] = typer.Option(None, "--style", help="Limit to specific style tag(s). Repeatable. Defaults to all styles."),
    refresh_collection: bool = typer.Option(False, "--refresh", help="Re-fetch the Discogs collection first (equivalent to running `scan --refresh`)."),
):
    """Match every track to a YouTube video and create one playlist per Discogs style tag."""
    if refresh_collection:
        scan(refresh=True)

    if not dry_run and not ytmusic_client.is_authenticated():
        console.print("[red]Not authenticated with YT Music.[/red] Run: discogs2ytmusic auth ytmusic")
        raise typer.Exit(1)

    yt = ytmusic_client.get_client(authenticated=not dry_run)

    with store.connect() as conn:
        by_style: dict[str, list[tuple[str, str]]] = defaultdict(list)  # style -> [(artist, title)]
        import json as _json

        for release, tracks in store.iter_releases_with_tracks(conn):
            styles = _json.loads(release["styles"]) or []
            if style:
                styles = [s for s in styles if s in style]
            if not styles:
                continue
            track_titles = [t["title"] for t in tracks] or [release["title"]]  # fall back to release title if no tracklist
            for s in styles:
                for track_title in track_titles:
                    by_style[s].append((release["artist"], track_title))

        if not by_style:
            console.print("[yellow]No releases found for the given style filter. Run `scan` first?[/yellow]")
            raise typer.Exit(0)

        total_tracks = sum(len(v) for v in by_style.values())
        with Progress(console=console) as progress:
            task = progress.add_task("Matching tracks on YouTube...", total=total_tracks)
            style_video_ids: dict[str, list[str]] = {}

            for s, track_list in by_style.items():
                video_ids: list[str] = []
                seen = set()
                for artist, title in track_list:
                    if (artist, title) in seen:
                        progress.advance(task)
                        continue
                    seen.add((artist, title))

                    cached = store.get_match(conn, artist, title)
                    if cached is not None:
                        video_id = cached["video_id"]
                    else:
                        result = matcher.find_match(yt, artist, title)
                        store.save_match(conn, artist, title, result.video_id, result.video_title, result.source, result.score)
                        conn.commit()  # commit per-track so a crash/interrupt doesn't lose earlier matches
                        video_id = result.video_id
                    if video_id:
                        video_ids.append(video_id)
                    progress.advance(task)
                style_video_ids[s] = video_ids

        table = Table(title="Sync preview" if dry_run else "Sync result")
        table.add_column("Style")
        table.add_column("Matched", justify="right")
        table.add_column("Total", justify="right")
        for s, track_list in by_style.items():
            matched = len(style_video_ids[s])
            table.add_row(s, str(matched), str(len(track_list)))
        console.print(table)

        if dry_run:
            console.print("[cyan]Dry run only — no playlists were created. Re-run with --apply to sync to YT Music.[/cyan]")
            return

        for s, video_ids in style_video_ids.items():
            if not video_ids:
                continue
            playlist_name = f"Discogs - {s}"
            existing_id = store.get_playlist_id(conn, s)
            if existing_id:
                playlist_id = existing_id
            else:
                playlist_id = ytmusic_client.get_or_create_playlist(
                    yt, playlist_name, description=f"Auto-generated from Discogs collection (style: {s})"
                )
                store.save_playlist_id(conn, s, playlist_id)
                conn.commit()
            ytmusic_client.add_tracks(yt, playlist_id, video_ids)
            console.print(f"[green]Synced '{playlist_name}': {len(video_ids)} tracks.[/green]")


EXPORT_FIELDNAMES = [
    "style",
    "artist",
    "title",
    "matched",
    "video_id",
    "youtube_url",
    "video_title",
    "source",
    "score",
    "searched_at",
]


@app.command()
def export(
    output: Path = typer.Option(Path("matches.csv"), "--output", "-o", help="CSV file to write."),
    style: Optional[list[str]] = typer.Option(None, "--style", help="Limit to specific style tag(s). Repeatable."),
    only_missing: bool = typer.Option(
        False, "--only-missing", help="Only include tracks with no confident YouTube match."
    ),
):
    """Export cached track-to-YouTube matches to a CSV for manual review.

    Reads whatever is already in the local cache — run `sync` (dry-run is
    fine, it doesn't touch your YT Music account) first to populate it. This
    command never searches YouTube itself.
    """
    import json as _json

    with store.connect() as conn:
        rows = []
        for release, tracks in store.iter_releases_with_tracks(conn):
            styles = _json.loads(release["styles"]) or []
            if style:
                styles = [s for s in styles if s in style]
            if not styles:
                continue
            track_titles = [t["title"] for t in tracks] or [release["title"]]  # fall back to release title if no tracklist
            for s in styles:
                for track_title in track_titles:
                    match = store.get_match(conn, release["artist"], track_title)
                    video_id = match["video_id"] if match else None
                    if only_missing and video_id:
                        continue
                    searched_at = ""
                    if match is not None:
                        searched_at = datetime.fromtimestamp(match["searched_at"]).isoformat(timespec="seconds")
                    rows.append(
                        {
                            "style": s,
                            "artist": release["artist"],
                            "title": track_title,
                            "matched": "yes" if video_id else "no",
                            "video_id": video_id or "",
                            "youtube_url": f"https://music.youtube.com/watch?v={video_id}" if video_id else "",
                            "video_title": (match["video_title"] if match else None) or "",
                            "source": (match["source"] if match else None) or "",
                            "score": match["score"] if match is not None else "",
                            "searched_at": searched_at,
                        }
                    )

    if not rows:
        console.print("[yellow]Nothing to export. Run `scan` and `sync` first.[/yellow]")
        raise typer.Exit(0)

    rows.sort(key=lambda r: (r["style"], r["artist"], r["title"]))

    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    unmatched = sum(1 for r in rows if r["matched"] == "no")
    console.print(f"[green]Wrote {len(rows)} rows to {output}[/green] ({unmatched} unmatched)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
