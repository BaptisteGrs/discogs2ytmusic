# discogs2ytmusic

Sync your Discogs collection to YouTube Music playlists.
Runs entirely locally as a CLI.

## Setup

```bash
uv sync
```

### 1. Connect Discogs

Get a personal access token from https://www.discogs.com/settings/developers
(no app registration needed, just click "Generate new token").

```bash
uv run discogs2ytmusic auth discogs --token YOUR_TOKEN
```

### 2. Connect YouTube Music

No Google Cloud project needed — this reuses your logged-in browser session
by pulling two values out of a request YT Music's own web app already makes:

1. Open https://music.youtube.com in your browser and make sure you're logged in.
2. Open DevTools → **Network** tab.
3. Click any request to `music.youtube.com` in the list (e.g. reload the page,
   then click the request named `browse`).
4. In its **Headers** panel, scroll to **Request Headers** and find the rows
   for `cookie` (a long string of `name=value;` pairs) and `x-goog-authuser`
   (usually just `0`).
5. Run the command below and paste each value when prompted:

```bash
uv run discogs2ytmusic auth ytmusic
```

Pasting a long cookie value into a terminal prompt can be fiddly. Instead you
can save the two values to a text file and point the command at it:

```
cookie: SID=...; HSID=...; ...
x-goog-authuser: 0
```

```bash
uv run discogs2ytmusic auth ytmusic --from-file /path/to/that/file.txt
```

The file is only read once during setup — delete it afterwards (the tool
will remind you to).

## Usage

### Scan your collection

Fetches your full Discogs collection + tracklists into a local cache and
shows a breakdown of how many tracks fall under each style tag. Safe to
re-run — cached releases/tracklists aren't re-fetched unless you pass
`--refresh`.

```bash
uv run discogs2ytmusic scan
```

### Preview the sync (no changes made)

Matches every track to a YouTube video ID and shows how many matched per
style. Nothing is written to YT Music yet.

```bash
uv run discogs2ytmusic sync
```

Limit to specific styles:

```bash
uv run discogs2ytmusic sync --style "Deep House" --style "Dub Techno"
```

### Actually create/update the playlists

```bash
uv run discogs2ytmusic sync --apply
```

Playlists are named `Discogs - <style>` and are safe to re-run: existing
playlists are reused (not duplicated), and matched tracks are cached so
re-syncing only searches for new tracks.

## Testing

Tests run entirely offline against a small dummy library instead of your real
Discogs/YT Music accounts.

```bash
uv run pytest
```

`tests/fixtures/dummy_library.json` holds 15 tracks across 6 sub-genres
(House, Techno, Deep House, Acid, Breakbeat, Trance), sampled from a real
scanned collection so the data shapes match what Discogs actually returns.
`tests/conftest.py` exposes it as the `dummy_library` fixture and wires up
two test doubles used throughout the suite:

- `FakeDiscogsClient` — implements the same `iter_collection_basic` /
  `get_release_tracklist` interface as `DiscogsClient`, backed by the fixture,
  so `scan` can be exercised end-to-end (via Typer's `CliRunner`) without a
  token or network access.
- `isolated_cache` — redirects the sqlite cache to a temp file per test, so
  test runs never touch your real local cache.

`sync` tests monkeypatch `matcher.find_match` to return deterministic
matches instead of calling YT Music/yt-dlp, so match-rate and style-grouping
logic can be verified without live search results.

To sample a fresh (or larger) dummy library from your own real cache, adapt
the pattern in `tests/fixtures/dummy_library.json`: pick a `release_id`,
`artist`, `title`, `styles`, `genres`, and one or more `tracklist` entries per
release, drawn from `~/Library/Caches/discogs2ytmusic/cache.sqlite3` (or the
equivalent cache path on your OS).

## Notes

- All state (credentials, collection cache, search-match cache) lives outside
  the project in your OS's standard config/cache directories, not in this repo.
- Track matching quality varies for very obscure records — check the `sync`
  preview table before running `--apply`, and consider narrowing with
  `--style` to review results in smaller batches first.
