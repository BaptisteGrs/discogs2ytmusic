# discogs2ytmusic

[![CI](https://github.com/BaptisteGrs/discogs2ytmusic/actions/workflows/ci.yml/badge.svg)](https://github.com/BaptisteGrs/discogs2ytmusic/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Wrapper for your Discogs collection.
Map YT Music links automatically.
Create playlists easily and push them on your YT account.
Dig people's collection or label catalogue.

Runs entirely locally as a CLI, with an optional browsable/editable Streamlit UI.

## Roadmap

- [ ] Filter the Collection tab by channel (favorite a group of channels).
- [x] Wantlist tab — superseded by the broader "Other sources" sidebar section, which also
      covers another user's collection and a label's catalogue.
- [ ] A dedicated filter for low-confidence matches.

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

### Review matches, and fix a wrong one or a bad search query

The [browsable UI](#browsable-web-ui) is the easiest way to do this — its
Collection table shows every cached match (with a Matched checkbox to filter
on), and you fix a wrong match or query by editing a row in place, with no
ids to look up.

The same two corrections are available from the CLI, addressed by a
match/track id from the local sqlite cache (`matches.id`/`tracks.id` — the
UI is the more convenient way to find these):

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
`sync` — but is lost if that release's tracklist is later replaced via
`scan --refresh` (tracks are fully deleted and re-inserted). Re-apply it if
that happens.

Neither command touches YouTube — both just edit the local cache.

### Actually create/update the playlists

`sync` only searches and caches matches — it never touches your YT Music
account. Building and pushing a playlist is a separate, explicit step done
in the [browsable UI](#browsable-web-ui)'s Playlists tab, which lets you
hand-pick exactly which tracks go into each playlist.

### Re-match tracks after a matcher/schema change

```bash
uv run discogs2ytmusic rematch
```

A normal `sync` skips anything already cached, so it never picks up fields
added to the match cache after the fact (e.g. the `channel` column). `rematch`
clears the cache and re-fetches the Discogs collection first, then re-matches
everything from scratch. Manually-corrected matches (via `correct`) are
preserved by default — pass `--include-manual` to clear those too.

### Start over from a clean cache

```bash
uv run discogs2ytmusic reset
```

Wipes the local sqlite cache entirely — releases, tracks, matches, playlists,
playlist folders, and Other Source definitions, including any manual
corrections — so you can rebuild from a clean `scan`. Useful after a bug has
corrupted cached data, or if you just want a fresh start. Asks for
confirmation first (pass `--yes`/`-y` to skip it for scripting); you'll need
to re-add any Other Source pages afterward, since those live in the wiped
cache too. Never touches saved credentials (Discogs token, YT Music auth,
playlist prefix) or your real YT Music account.

### Try any command against a small test collection instead of your own

Every command accepts a `--library dummy` flag (before the subcommand) that
points the cache at a separate file (`dummy_cache.sqlite3`, never your real
`cache.sqlite3`) and, for `scan`, seeds it straight from the bundled test
fixture instead of calling the Discogs API — no Discogs token needed:

```bash
uv run discogs2ytmusic --library dummy scan
uv run discogs2ytmusic --library dummy sync            # still needs YT Music reachable for real searches
```

Handy for sanity-checking a change to the matcher logic, or just seeing the
whole `scan` → `sync` flow end-to-end in seconds. This only works from a
full repo checkout (it reads `tests/fixtures/dummy_library.json` directly,
it isn't packaged) — omit the flag, or pass `--library real` (the default),
to use your actual collection.

## Browsable web UI

```bash
uv run discogs2ytmusic ui
```

Launches a local Streamlit app (`app.py`) over the same cache the CLI uses.

A sidebar drives navigation: **My Discogs Collection** at the top, then a
**Playlists** section listing every hand-curated playlist you've created.

**My Discogs Collection** is a filterable, editable table of every cached
track: filter by style/genre, label, year, or matched-only; edit a track's
artist inline to override the YouTube search query, or paste/clear a YouTube
link directly — both save as the same manual corrections `fix-artist`/`correct`
make from the CLI, and a corrected row is marked **Locked** so it survives the
next `scan --refresh`/`rematch`. Select tracks (the leading checkbox column)
to add them straight to a new or existing playlist.

Three buttons above the table cover the rest of the CLI's scan → match → push
loop, so the whole thing is doable without leaving the app: **Scan** re-fetches
your collection and tracklists from Discogs (`scan --refresh`); **Sync
matches** matches any unmatched tracks against YouTube/YT Music (`sync`) —
it only populates the match cache, it never touches a real YT Music account;
and **Rematch** clears cached matches and re-matches everything from scratch
(`rematch`), preserving manual corrections unless you opt in to clearing
those too. Rematch can take a while and is destructive to the match cache, so
it asks for confirmation first, the same as the Playlists tab's delete/push
buttons.

**Playlists** manages hand-curated playlists — you build these track by
track rather than one playlist per style tag: create a playlist
from the sidebar's "+ New playlist" form, add tracks from the Collection view
or by searching by artist/title within a playlist itself, reorder by removing
and re-adding, and push the result to a real YT Music playlist (named
`Discogs - <name>`) with its own confirmed Sync button — safe to re-run, it
reuses the same YT Music playlist rather than duplicating it. Deleting a
playlist here only forgets it locally; it never deletes the linked YT Music
playlist.

**YT Music** (sidebar) is the connection page — the UI equivalent of
`auth ytmusic`, plus the pushed-playlist name prefix and a **Full reset**
button, both settings-level and account-level rather than tied to any one
source. Full reset is the UI equivalent of `reset` (see above): confirm-gated
the same way as Rematch, its warning names exactly how many releases, cached
matches, playlists, and Other Sources will be deleted before anything
happens.

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

## Development

```bash
uv sync                          # installs dev tools too (ruff, mypy, pytest, pre-commit)
uv run ruff check .              # lint
uv run ruff format .             # format
uv run mypy src                  # type-check
uv run pytest                    # test (with coverage)
uv run pre-commit install        # optional: run the above automatically on every commit
```

CI (`.github/workflows/ci.yml`) runs all four on every push/PR. See
[`CLAUDE.md`](CLAUDE.md) for an architecture overview and conventions.

## Notes

- All state (credentials, collection cache, search-match cache) lives outside
  the project in your OS's standard config/cache directories, not in this repo.
- Track matching quality varies for very obscure records — check the `sync`
  preview table before running `--apply`, and consider narrowing with
  `--style` to review results in smaller batches first.
