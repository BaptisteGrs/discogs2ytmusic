# Architecture

This document explains how `discogs2ytmusic` is put together: the pipeline it
runs, the on-disk data it keeps, and what each module is responsible for.
It's aimed at anyone extending the tool or debugging unexpected behavior —
for install/usage instructions see the [README](../README.md).

## Pipeline overview

The tool has two phases, run as two commands:

```
discogs2ytmusic scan          discogs2ytmusic sync [--apply]
┌─────────────┐   basic info   ┌──────────────┐   video IDs   ┌──────────────┐
│   Discogs    │───────────────▶│    SQLite    │───────────────▶│  YT Music    │
│     API      │   tracklists   │    cache     │   playlists   │     API      │
└─────────────┘               └──────────────┘               └──────────────┘
```

1. **`scan`** pulls your Discogs collection (every release, with its style/
   genre tags) and each release's tracklist, and stores it in a local SQLite
   cache. This is the only command that talks to Discogs.
2. **`sync`** reads releases + tracks back out of that cache, groups them by
   style tag, and for each track searches YouTube/YT Music for a matching
   video. In dry-run mode (the default) it only prints a match-rate table.
   With `--apply` it also creates/updates one YT Music playlist per style and
   adds the matched videos to it.

Both commands are idempotent and resumable: cached data is reused unless you
force a refresh, and every unit of work (one release's tracklist, one
track's search match, one playlist) is committed to SQLite as soon as it
completes, so an interrupted run picks up roughly where it left off on the
next invocation.

## Module reference

| Module | Responsibility |
|---|---|
| `cli.py` | Typer app; defines the `auth discogs`, `auth ytmusic`, `scan`, and `sync` commands. Orchestrates the other modules — contains no API or DB logic of its own. |
| `config.py` | Resolves OS-standard config/cache directories (via `platformdirs`) and loads/saves `config.json` (Discogs token + username). |
| `discogs.py` | `DiscogsClient` — thin wrapper around the Discogs REST API: identity lookup, paginated collection listing, per-release tracklist fetch. Rate-limits itself to stay under Discogs' 60 req/min cap and retries on `429`. |
| `store.py` | All SQLite access — schema creation and every read/write query used by `cli.py`. See [Data model](#data-model) below. |
| `matcher.py` | Turns an `(artist, title)` pair into a YouTube video ID: tries a YT Music search first, falls back to a general YouTube search via `yt-dlp`, and scores candidates with fuzzy string matching. |
| `ytmusic_client.py` | Wraps `ytmusicapi`: interactive/`--from-file` credential setup, playlist lookup/creation, and chunked track insertion. |

## Data model

Everything scanned or matched is cached in a single SQLite database
(`cache.sqlite3`, location below). Schema, from `store.py`:

- **`releases`** — one row per Discogs release: id, artist, title, and
  `styles`/`genres` as JSON-encoded lists (a release can carry multiple style
  tags, which is why the sync step groups by style rather than by release).
- **`tracks`** — one row per track, foreign-keyed to `releases`. Auto-increment
  `id` preserves Discogs tracklist order (position/side info isn't reliable
  enough to sort by on its own).
- **`matches`** — a cache of `(artist, title) → video_id` search results,
  keyed by a normalized `"artist||title"` string so the same track (which may
  appear on several releases, or under several style tags) is only searched
  for once. A row with `video_id = NULL` means "searched, no confident match
  found" — that's still cached, so a bad match isn't retried every run.
- **`playlists`** — maps each style tag to the YT Music playlist ID created
  for it, so re-running `sync --apply` updates the existing playlist instead
  of creating a duplicate.

Rows are committed incrementally (per release in `scan`, per track match and
per playlist in `sync`) rather than in one transaction at the end, precisely
so a crash or Ctrl-C partway through doesn't discard already-completed work.

## Matching algorithm

For each `(artist, title)` pair, `matcher.find_match`:

1. Searches YT Music itself (`filter="songs"` and `filter="videos"`, top 5
   results each), scores every candidate against the query with
   `rapidfuzz.fuzz.token_set_ratio` on `"{artist} {title}"`, and accepts the
   best result if its score is ≥ `YTMUSIC_THRESHOLD` (65).
2. If nothing cleared that bar, falls back to a plain YouTube search via
   `yt-dlp` (`ytsearch5:`), scored the same way against a lower
   `YTDLP_THRESHOLD` (55) — YT Music's own catalog is smaller, so this catches
   B-sides, DJ rips, and other tracks that only exist as regular YouTube
   uploads.
3. Returns `MatchResult(video_id=None, source="none")` if neither search
   clears its threshold. That "no match" result is still cached (see above).

`yt-dlp` searches are throttled to one per 1.5s and retried up to 3 times
with backoff — hitting YouTube's search endpoint back-to-back tends to
trigger transient `403 Forbidden` responses.

## Where state lives

Nothing is stored inside the repo. `config.py` resolves OS-standard
directories via `platformdirs`:

| File | Location | Contents |
|---|---|---|
| `config.json` | user config dir | Discogs token + username (mode `0600`) |
| `ytmusic_auth.json` | user config dir | YT Music session headers (mode `0600`) |
| `cache.sqlite3` | user cache dir | Releases, tracks, search-match cache, playlist IDs |

On macOS that's `~/Library/Application Support/discogs2ytmusic/` for config
and `~/Library/Caches/discogs2ytmusic/` for the cache; on Linux, XDG
equivalents (`~/.config/discogs2ytmusic/`, `~/.cache/discogs2ytmusic/`).
Deleting the cache file is always safe — everything in it is re-derivable
from `scan`/`sync`, just at the cost of re-fetching/re-matching.

## Extending the tool

- **New matching source**: add a `search_*` function to `matcher.py`
  following the `search_ytmusic`/`search_ytdlp` shape (returns
  `MatchResult | None`, scores with `_score`), then chain it into
  `find_match`.
- **New grouping besides style tags**: `sync` in `cli.py` builds
  `by_style: dict[str, list[(artist, title)]]` directly from
  `store.iter_releases_with_tracks` — swap the grouping key there (e.g.
  `release["genres"]`) and the rest of the sync/match/playlist logic is
  unchanged.
- **Schema changes**: edit the `SCHEMA` string in `store.py`. There's no
  migration system — `CREATE TABLE IF NOT EXISTS` means existing caches keep
  old columns, so a real schema change currently requires deleting
  `cache.sqlite3` (or hand-writing a migration).
