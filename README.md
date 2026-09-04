# discogs2ytmusic

## To do 

- [ ] Add `YT Channel` column. Filter by group of YT channels (possible to mark favorites).
- [ ] Wantlist tab
- [ ] To review filter for low confidence matches
- [ ] If Untitled in a release, use side tags (A2,...) to search for the song
- [ ] Use the song links already in Discogs if they are already present. Check the confidence on these links if possible. 


Wrapper for your Discogs collection. 
Map YT Music links automatically.
Create playlists easily and push them on your YT account. 
Dig people's collection or label catalogue. 

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

### Review matches before creating anything

`sync` (even in dry-run mode) caches every YouTube match it finds, hit or
miss. Export that cache to a CSV to sanity-check match quality on a subset
before trusting `--apply`, or to find tracks with no YouTube match at all:

```bash
uv run discogs2ytmusic sync --style "Deep House"     # populate the cache, no changes made
uv run discogs2ytmusic export --style "Deep House" --output deep_house.csv
```

Columns: `match_id, track_id, style, artist, title, discogs_url, matched,
video_id, youtube_url, video_title, source, score, searched_at`. Filter/sort
on the `matched` column (yes/no) in your spreadsheet tool to isolate tracks
with no confident match — candidates for ripping/uploading yourself.
`discogs_url` links back to the release on Discogs. `match_id`/`track_id` are
the ids to pass to `correct`/`fix-artist` below (see "artist" note there:
this column shows the artist actually used for the search, which may already
reflect a `fix-artist` override).

This only reads the local cache — it never hits YouTube itself, so it's
cheap to re-run as you narrow things down. There's no interactive browser
for the cache yet, just CSV export for now.

### Fix a wrong match or a bad search query by hand

Two commands, addressed by the ids from `export`, for the two different
things that can go wrong:

**`correct <match_id>`** — the search picked the wrong video (or none), but
the query itself was fine. Give it the right video yourself, mark it as
genuinely having no match, or throw the cached result away so `sync` tries
again:

```bash
uv run discogs2ytmusic correct 42 --video-id https://music.youtube.com/watch?v=XXXXXXXXXXX
uv run discogs2ytmusic correct 42 --reject   # confirmed no match exists — won't be re-searched
uv run discogs2ytmusic correct 42 --clear    # forget it, re-search on next sync
```

**`fix-artist <track_id>`** — the query itself was the problem. Discogs
credits a release to every artist on it joined with commas (e.g. `"Cesare
Muraca, Aymeric"`), and that full string is used to search *every* track on
the release even when a given track is really just one of them. The extra
name(s) dilute the fuzzy match — sometimes enough to miss entirely.
`fix-artist` overrides the artist used for one track's search:

```bash
uv run discogs2ytmusic fix-artist 9 --artist "Aymeric"
uv run discogs2ytmusic sync --style Acid   # re-searches under the corrected query
uv run discogs2ytmusic fix-artist 9 --clear   # revert to the release's artist
```

The override lives on the track row, so it survives normal re-runs of
`sync`/`export` — but is lost if that release's tracklist is later replaced
via `scan --refresh` (tracks are fully deleted and re-inserted). Re-apply it
if that happens.

Neither command touches YouTube — both just edit the local cache.

### Actually create/update the playlists

```bash
uv run discogs2ytmusic sync --apply
```

Playlists are named `Discogs - <style>` and are safe to re-run: existing
playlists are reused (not duplicated), and matched tracks are cached so
re-syncing only searches for new tracks.

### Try any command against a small test collection instead of your own

Every command accepts a `--library dummy` flag (before the subcommand) that
points the cache at a separate file (`dummy_cache.sqlite3`, never your real
`cache.sqlite3`) and, for `scan`, seeds it straight from the bundled test
fixture instead of calling the Discogs API — no Discogs token needed:

```bash
uv run discogs2ytmusic --library dummy scan
uv run discogs2ytmusic --library dummy sync            # still needs YT Music reachable for real searches
uv run discogs2ytmusic --library dummy export -o dummy_matches.csv
```

Handy for sanity-checking a change to the matcher/export logic, or just
seeing the whole `scan` → `sync` → `export` flow end-to-end in seconds. This
only works from a full repo checkout (it reads `tests/fixtures/dummy_library.json`
directly, it isn't packaged) — omit the flag, or pass `--library real`
(the default), to use your actual collection.

## Testing

Tests run entirely offline against a small dummy library instead of your real
Discogs/YT Music accounts.

```bash
uv run pytest
```

`tests/fixtures/dummy_library.json` holds 15 tracks across 6 sub-genres
(House, Techno, Deep House, Acid, Breakbeat, Trance), sampled from a real
scanned collection so the data shapes match what Discogs actually returns —
it's the same file `--library dummy` (above) reads. `tests/conftest.py`
exposes it as the `dummy_library` fixture and wires up two test doubles used
throughout the suite:

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
