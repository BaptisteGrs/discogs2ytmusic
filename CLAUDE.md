# CLAUDE.md

Guidance for Claude Code (and other agents/contributors) working in this repo.

## What this is

A local CLI (+ optional Streamlit UI) that syncs a Discogs collection to
YouTube Music: it fetches your collection and tracklists from Discogs,
matches each track to a YouTube video, and can push the results as YT Music
playlists. Everything runs locally — no server, no external database. See
`README.md` for user-facing usage; this file is about working on the code.

## Architecture

Data flows in one direction, through a local sqlite cache that both the CLI
and the Streamlit UI read and write:

```
Discogs API  →  sqlite cache (store.py)  →  matcher.py  →  YT Music API
 (discogs.py)      ↑            ↓                           (ytmusic_client.py)
                cli.py / app.py (Streamlit)
```

- **`discogs.py`** — thin, rate-limited Discogs API client (`DiscogsClient`).
- **`store.py`** — the sqlite cache: schema, migrations, and every read/write
  query. This is the source of truth; nothing else touches the database
  directly. Also owns the "effective" artist/title resolution logic (manual
  overrides > Discogs per-track credit > heuristic split > release artist).
- **`matcher.py`** — finds a YouTube video for a (artist, title) query: tries
  Discogs' own embedded videos first, then YT Music search, then a plain
  yt-dlp YouTube search as a last resort.
- **`sync_engine.py`** — orchestrates `matcher` + `store` to make sure every
  track across a set of releases has a cached match; shared by the CLI's
  `sync`/`rematch` and reusable from the UI.
- **`ytmusic_client.py`** — thin wrapper around `ytmusicapi` (auth setup,
  playlist create/lookup, adding tracks).
- **`filters.py`** — `PlaylistFilter` (ad-hoc Collection-tab filter criteria)
  and `resolve_rows`/`resolve_playlist_rows`, which flatten the cache into one
  `TrackRow` per track (the whole collection, or one curated playlist's
  tracks in playlist order) for the UI to render.
- **`views.py`** — the older one-row-per-(style, track) shape used by the
  legacy `export`/`push-style-playlists` CLI commands.
- **`collection_edits.py`** — diffs the Streamlit data editor's before/after
  DataFrames and persists changes as manual corrections via `store`.
- **`cli.py`** — the Typer app; thin command layer over the modules above.
- **`app.py`** — the Streamlit UI; thin view layer over `filters`/`store`.
  Collection tab (browse/edit/select tracks) + Playlists tab (curated
  playlists — see below).
- **`config.py`** — credential/cache file locations (via `platformdirs`) and
  the `Config` dataclass for saved Discogs credentials.
- **`dummy_library.py`** — loads `tests/fixtures/dummy_library.json` for the
  `--library dummy` dev/testing flow.

**Manual corrections and "locking".** A user can override a track's search
artist (`fix-artist` / editing "Track Artist" in the UI) or hand-pick a
YouTube link (`correct` / editing the "YouTube link" column). Both are stored
on the existing row (`tracks.search_artist`, or `matches.source = 'manual'`)
rather than a separate table, and both make `TrackRow.locked = True`
(`filters.py`). `store.upsert_release`/`replace_tracks` are written to never
clobber these on a re-scan, and `rematch` preserves `source='manual'` matches
unless `--include-manual` is passed. If you touch scan/rematch/upsert logic,
preserve this invariant — silently discarding a manual correction is the
main failure mode to avoid here.

## Conventions

- **Typing**: fully type-hinted, checked with `mypy` (`disallow_untyped_defs`
  — see `[tool.mypy]` in `pyproject.toml`). `sqlite3.Row` is used as the type
  for a single cache row; there are no ORM models.
- **Docstrings**: Google style (`Args:`/`Returns:`/`Raises:`), required on
  public classes/functions/methods (ruff `D101`/`D102`/`D103`/`D107` — see
  `[tool.ruff.lint]`). Private helpers (leading `_`) and everything under
  `tests/` are exempt — a docstring restating an obvious one-liner isn't
  worth it, and a descriptive test name is its own spec.
- **Comments** explain *why*, not *what* — a non-obvious constraint, an
  invariant, a workaround. Don't add a comment a reader wouldn't need.
- **No premature abstraction.** This is a ~2.5k line single-maintainer tool;
  prefer a few similar lines over a new layer of indirection.
- Line length 120 (`ruff format`/`ruff check` are the formatter and linter of
  record — don't hand-format).

## Commands

```bash
uv sync                    # installs runtime + dev deps (ruff, mypy, pytest, pre-commit)
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src            # type-check (src/ only — tests/ has a relaxed mypy override)
uv run pytest              # test, with coverage report
uv run pre-commit install  # optional: run the three above automatically on commit
```

Tests run fully offline against `tests/fixtures/dummy_library.json` via
`FakeDiscogsClient` and an isolated sqlite file (see `tests/conftest.py`) —
never against a real Discogs/YT Music account. The same fixture backs
`--library dummy` for manual end-to-end poking:

```bash
uv run discogs2ytmusic --library dummy scan
uv run discogs2ytmusic --library dummy sync
```

## Gotchas

- **Two unrelated "playlist" concepts coexist — don't conflate them.**
  `playlist_defs` (+ `PlaylistFilter.to_json`/`from_json`) is the older
  filter-based persistence used only by the legacy `push-style-playlists` CLI
  command (one playlist per Discogs style tag, auto-built from a filter).
  `playlists`/`playlist_tracks` is the newer hand-curated playlist feature
  behind the UI's Playlists tab (`store.create_playlist`/`add_tracks_to_playlist`/
  etc.) — an explicit, ordered list of tracks a user assembled themselves, no
  filter involved. They don't share rows or code paths. The table name
  `playlists` was reused for the new feature, which is exactly what made
  `_migrate_playlists_to_playlist_defs` (below) need a real column check
  instead of a bare existence check — a bug that shipped once already.
- **Schema changes are additive migrations, not destructive ones.** `store.py`
  guards every `ALTER TABLE` with a `PRAGMA table_info` check (see
  `_migrate_*_table`) so an existing user's cache upgrades in place. Follow
  that pattern for new columns; never assume a fresh schema.
- **`--library dummy` mutates `store.CACHE_DB` at import-adjacent runtime**
  (in the `cli_options` callback in `cli.py`) to point at a sibling
  `dummy_cache.sqlite3` file. It's dev/testing-only and only works from a
  full repo checkout (it reads `tests/fixtures/dummy_library.json` directly).
- **yt-dlp searches are throttled and retried** (`matcher.YTDLP_MIN_INTERVAL`/
  `YTDLP_MAX_RETRIES`) because back-to-back requests trip YouTube's bot
  detection. Don't remove the throttle to "speed things up."
- All persistent state (credentials, collection cache, match cache) lives
  outside the repo in OS-standard config/cache dirs (`config.py`), not in any
  file here.
