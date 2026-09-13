"""The sqlite cache: schema, migrations, and every read/write query.

This is the source of truth for everything scanned from Discogs and matched against
YouTube — nothing else in the codebase opens the database directly. Besides plain CRUD,
it owns the "effective" value resolution logic (manual override > Discogs per-track
credit > heuristic split > release value, see `effective_track_queries` and friends)
that both `filters.py` (the UI's live Collection view) and the CLI's `fix-*` commands
build on. Schema changes here must stay additive (see CLAUDE.md's Gotchas) so an
existing user's cache upgrades in place instead of losing data.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager

from .config import CACHE_DB, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    release_id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    styles TEXT NOT NULL,   -- json list
    genres TEXT NOT NULL,   -- json list
    year INTEGER,
    labels TEXT NOT NULL DEFAULT '[]',   -- json list
    artist_override TEXT,   -- manual correction; NULL falls back to `artist`
    title_override TEXT,    -- manual correction; NULL falls back to `title`
    styles_override TEXT,   -- manual correction; json list, NULL falls back to `styles`
    genres_override TEXT,   -- manual correction; json list, NULL falls back to `genres`
    -- json list of {"uri", "title", "duration"} — Discogs' own embedded YouTube links
    videos TEXT NOT NULL DEFAULT '[]',
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    position TEXT NOT NULL,
    title TEXT NOT NULL,
    duration TEXT,
    -- manual override for the artist used to search YouTube; NULL falls back to
    -- discogs_artist/releases.artist
    search_artist TEXT,
    -- Discogs' own per-track artist credit (compilations/VA releases only; NULL
    -- when it's just the release artist)
    discogs_artist TEXT,
    -- manual per-track corrections; json list, NULL falls back to the release's
    -- effective styles/genres (Discogs only reports these per-release, so a
    -- multi-style release has no way to say which track is which without this)
    styles_override TEXT,
    genres_override TEXT,
    FOREIGN KEY (release_id) REFERENCES releases(release_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_release_id ON tracks(release_id);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_key TEXT NOT NULL UNIQUE,   -- "artist||title"
    video_id TEXT,                -- NULL means "searched, no confident match"
    video_title TEXT,
    source TEXT,                  -- 'ytmusic' | 'ytdlp' | 'discogs' | 'manual' | 'none'
    score REAL,
    channel TEXT,                 -- uploader/channel name of the matched video, when known
    searched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    -- {"tags": [...], "labels": [...], "year_min": int|null, "year_max": int|null,
    --  "matched_only": bool}
    filter_json TEXT NOT NULL,
    ytmusic_playlist_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ytmusic_playlist_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,   -- bumped by content edits only (add/remove tracks), not by pushing
    pushed_at REAL,             -- last successful push to YT Music; NULL if never pushed
    -- optional grouping folder; a playlist belongs to at most one folder, or none (no nesting)
    folder_id INTEGER REFERENCES playlist_folders(id),
    -- which Discogs source this playlist was built from ('collection' for "My Discogs
    -- Collection", else matches an other_sources row) — a playlist may only ever contain
    -- tracks from this one source (see add_tracks_to_playlist)
    source_type TEXT NOT NULL DEFAULT 'collection',
    source_key TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    position INTEGER NOT NULL,   -- ordering within the playlist
    added_at REAL NOT NULL,
    PRIMARY KEY (playlist_id, track_id),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id),
    FOREIGN KEY (track_id) REFERENCES tracks(id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist_id ON playlist_tracks(playlist_id);

-- Registry of "Other sources" pages the user has added (see app.py's sidebar) — one row
-- per pasted Discogs collection/wantlist/label/seller link. "My Discogs Collection" itself
-- is not in here: it's the implicit default source, always present.
CREATE TABLE IF NOT EXISTS other_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,      -- 'user_collection' | 'wantlist' | 'label' | 'seller'
    source_key TEXT NOT NULL,       -- username, or label id as text
    display_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    -- json {"styles": [...], "formats": [...], "year_min": int|null, "year_max": int|null}
    -- pre-filter applied on every scan (see scan_engine.ImportFilter) so only matching
    -- releases are ever imported under this source; '{}' means "import everything"
    filter_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_type, source_key)
);

-- Many-to-many: which Discogs source(s) each cached release was scanned from. A release
-- can belong to more than one source at once (e.g. it's in both your collection and a
-- label's catalogue) — see issue #13's own recommendation. 'collection' (source_key '')
-- is the implicit "my own collection" tag; other rows mirror an other_sources entry.
CREATE TABLE IF NOT EXISTS release_sources (
    release_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL,
    PRIMARY KEY (release_id, source_type, source_key),
    FOREIGN KEY (release_id) REFERENCES releases(release_id)
);
CREATE INDEX IF NOT EXISTS idx_release_sources_type_key ON release_sources(source_type, source_key);

-- Format name/description tokens (e.g. "Vinyl", "12\"", "Album") ever seen on a scanned
-- release. Discogs exposes no API endpoint or dataset enumerating its format vocabulary
-- (unlike genres/styles — see discogs_taxonomy.py), so the Add-source page's Format
-- picklist is instead grown organically from real scan results (`scan_engine`).
CREATE TABLE IF NOT EXISTS known_formats (
    name TEXT PRIMARY KEY
);
"""

DEFAULT_SOURCE_TYPE = "collection"
DEFAULT_SOURCE_KEY = ""


def _migrate_matches_table(conn: sqlite3.Connection) -> None:
    """One-time upgrades for caches created before `matches` had a surrogate id
    column, and/or before it had a `channel` column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
    if not cols:
        return
    if "id" not in cols:
        conn.executescript(
            """
            ALTER TABLE matches RENAME TO matches_old;
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key TEXT NOT NULL UNIQUE,
                video_id TEXT,
                video_title TEXT,
                source TEXT,
                score REAL,
                channel TEXT,
                searched_at REAL NOT NULL
            );
            INSERT INTO matches (query_key, video_id, video_title, source, score, searched_at)
                SELECT query_key, video_id, video_title, source, score, searched_at FROM matches_old;
            DROP TABLE matches_old;
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
    if "channel" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN channel TEXT")
    conn.commit()


def _migrate_tracks_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for caches created before `tracks` had search_artist/discogs_artist/
    styles_override/genres_override columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    if not cols:
        return
    if "search_artist" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN search_artist TEXT")
    if "discogs_artist" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN discogs_artist TEXT")
    if "styles_override" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN styles_override TEXT")
    if "genres_override" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN genres_override TEXT")
    conn.commit()


def _migrate_releases_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for caches created before `releases` had year/labels/overrides/videos columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(releases)")}
    if not cols:
        return
    if "year" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN year INTEGER")
    if "labels" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN labels TEXT NOT NULL DEFAULT '[]'")
    if "artist_override" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN artist_override TEXT")
    if "title_override" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN title_override TEXT")
    if "videos" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN videos TEXT NOT NULL DEFAULT '[]'")
    if "styles_override" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN styles_override TEXT")
    if "genres_override" not in cols:
        conn.execute("ALTER TABLE releases ADD COLUMN genres_override TEXT")
    conn.commit()


def _migrate_playlists_to_playlist_defs(conn: sqlite3.Connection) -> None:
    """One-time upgrade: fold the old style->playlist_id table into playlist_defs.

    Each style becomes its own filter-based playlist def (tags=[style]), preserving
    the already-created YT Music playlist id so re-syncing doesn't create duplicates.

    `playlists` is also the name of the current curated-playlists table (see SCHEMA) —
    check for the legacy table's actual columns, not just table existence, so this
    doesn't misfire against that unrelated table.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(playlists)")}
    if not {"style", "playlist_id", "created_at"} <= cols:
        # No legacy table, or a `playlists` table from some other (unrelated) shape —
        # either way there's nothing in the expected old shape to migrate.
        return
    rows = conn.execute("SELECT style, playlist_id, created_at FROM playlists").fetchall()
    now = time.time()
    for style, playlist_id, created_at in rows:
        conn.execute(
            """INSERT INTO playlist_defs (name, filter_json, ytmusic_playlist_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET ytmusic_playlist_id=excluded.ytmusic_playlist_id""",
            (f"Discogs - {style}", json.dumps({"tags": [style]}), playlist_id, created_at, now),
        )
    conn.execute("DROP TABLE playlists")
    conn.commit()


def _migrate_playlists_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for curated-playlist caches created before `playlists` had a
    `pushed_at` column (last successful push to YT Music, separate from `updated_at`, which
    tracks content edits only) and/or a `folder_id` column (optional grouping folder).

    Only touches the table when it actually looks like the curated-playlists shape — not the
    legacy style->playlist_id shape (handled, and possibly dropped, by
    `_migrate_playlists_to_playlist_defs` above) or some unrelated table that happens to be
    named `playlists`.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(playlists)")}
    if not {"id", "name", "ytmusic_playlist_id", "created_at", "updated_at"} <= cols:
        return
    if "pushed_at" not in cols:
        conn.execute("ALTER TABLE playlists ADD COLUMN pushed_at REAL")
    if "folder_id" not in cols:
        conn.execute("ALTER TABLE playlists ADD COLUMN folder_id INTEGER REFERENCES playlist_folders(id)")
    if "source_type" not in cols:
        conn.execute("ALTER TABLE playlists ADD COLUMN source_type TEXT NOT NULL DEFAULT 'collection'")
    if "source_key" not in cols:
        conn.execute("ALTER TABLE playlists ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _migrate_other_sources_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for `other_sources` rows created before `filter_json` existed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(other_sources)")}
    if not cols:
        return
    if "filter_json" not in cols:
        conn.execute("ALTER TABLE other_sources ADD COLUMN filter_json TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def _migrate_release_sources_backfill(conn: sqlite3.Connection) -> None:
    """Tag every pre-existing release as collection-sourced.

    Before this feature, the only scan path was the user's own collection — so a release
    with no `release_sources` row yet (an upgrade from an older cache) belongs there, not
    nowhere. Additive and idempotent: only inserts for releases missing every source tag.
    """
    conn.execute(
        """INSERT OR IGNORE INTO release_sources (release_id, source_type, source_key, added_at)
           SELECT release_id, ?, ?, ? FROM releases
           WHERE release_id NOT IN (SELECT release_id FROM release_sources)""",
        (DEFAULT_SOURCE_TYPE, DEFAULT_SOURCE_KEY, time.time()),
    )
    conn.commit()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open the sqlite cache, applying schema/migrations first, and commit on clean exit."""
    ensure_dirs()
    conn = sqlite3.connect(CACHE_DB)
    conn.executescript(SCHEMA)
    _migrate_matches_table(conn)
    _migrate_tracks_table(conn)
    _migrate_releases_table(conn)
    _migrate_playlists_to_playlist_defs(conn)
    _migrate_playlists_table(conn)
    _migrate_other_sources_table(conn)
    _migrate_release_sources_backfill(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def match_key(artist: str, title: str) -> str:
    """Build the normalized `matches.query_key` for an (artist, title) pair."""
    return f"{artist.strip().lower()}||{title.strip().lower()}"


def record_release_source(conn: sqlite3.Connection, release_id: int, source_type: str, source_key: str) -> None:
    """Tag a release as belonging to a given source (many-to-many — see `release_sources`).

    A no-op if the release is already tagged with this exact (source_type, source_key).
    """
    conn.execute(
        "INSERT OR IGNORE INTO release_sources (release_id, source_type, source_key, added_at) VALUES (?, ?, ?, ?)",
        (release_id, source_type, source_key, time.time()),
    )


def prune_release_source_tags(
    conn: sqlite3.Connection, source_type: str, source_key: str, keep_release_ids: set[int]
) -> int:
    """Untag every release currently linked to this source whose id isn't in `keep_release_ids`.

    Used after re-scanning a *filtered* Other-source (see `scan_engine.ImportFilter`) to
    keep the source's tags an exact reflection of "what currently matches the filter" —
    a release that no longer matches (or was removed upstream) stops showing up under this
    source, without touching its cached `releases`/`tracks`/`matches` rows (it may still be
    tagged under another source, or referenced by a playlist). Never called for an
    unfiltered scan (the collection/wantlist/label default), which has never pruned stale
    releases and shouldn't start now.

    Returns:
        How many release_sources tags were removed.
    """
    existing_ids = {
        row[0]
        for row in conn.execute(
            "SELECT release_id FROM release_sources WHERE source_type = ? AND source_key = ?",
            (source_type, source_key),
        )
    }
    stale_ids = existing_ids - keep_release_ids
    if stale_ids:
        conn.executemany(
            "DELETE FROM release_sources WHERE release_id = ? AND source_type = ? AND source_key = ?",
            [(rid, source_type, source_key) for rid in stale_ids],
        )
    return len(stale_ids)


def record_known_formats(conn: sqlite3.Connection, names: Iterable[str]) -> None:
    """Remember format name/description tokens seen on a scanned release (e.g. "Vinyl",
    "12\"", "Album"), growing the Add-source page's Format picklist organically — see
    `known_formats`'s schema comment for why this exists instead of a static list.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO known_formats (name) VALUES (?)",
        [(n.strip(),) for n in names if n and n.strip()],
    )


def list_known_formats(conn: sqlite3.Connection) -> list[str]:
    """Every format name/description token seen so far across any scan, sorted."""
    return [row[0] for row in conn.execute("SELECT name FROM known_formats ORDER BY name")]


def upsert_release(
    conn: sqlite3.Connection,
    release_id: int,
    artist: str,
    title: str,
    styles: list[str],
    genres: list[str],
    year: int | None = None,
    labels: list[str] | None = None,
    videos: list[dict] | None = None,
    source_type: str = DEFAULT_SOURCE_TYPE,
    source_key: str = DEFAULT_SOURCE_KEY,
) -> None:
    """Insert or refresh a release's Discogs-sourced fields, and tag it with the given source.

    `videos` is Discogs' own embedded YouTube links for the release (each a
    dict with "uri"/"title"/"duration"), used by the sync matcher before it
    falls back to searching YouTube itself. Pass `None` (the default) to leave
    whatever's already cached untouched — `scan`'s basic-collection pass calls
    this for every release on every run, but only the (rarer) full tracklist
    fetch actually has fresh video data to offer.

    `source_type`/`source_key` record which Discogs source this release was scanned from
    (see `release_sources`) — defaults to "my own collection", so every existing call site
    (a plain collection scan) keeps tagging releases exactly as before. Scanning a label
    catalogue, another user's collection, or a wantlist passes its own source_type/key;
    the tag is additive (`record_release_source`), so a release already known from one
    source doesn't lose that tag by also turning up in another.

    Deliberately does not touch artist_override/title_override/styles_override/
    genres_override — a re-scan (e.g. `scan --refresh`) must not wipe out manual
    corrections made via the browsable UI.
    """
    videos_json = json.dumps(videos) if videos is not None else None
    conn.execute(
        """INSERT INTO releases (release_id, artist, title, styles, genres, year, labels, videos, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(release_id) DO UPDATE SET
             artist=excluded.artist, title=excluded.title,
             styles=excluded.styles, genres=excluded.genres,
             year=excluded.year, labels=excluded.labels,
             videos=COALESCE(?, videos), fetched_at=excluded.fetched_at""",
        (
            release_id,
            artist,
            title,
            json.dumps(styles),
            json.dumps(genres),
            year,
            json.dumps(labels or []),
            videos_json if videos_json is not None else "[]",
            time.time(),
            videos_json,
        ),
    )
    record_release_source(conn, release_id, source_type, source_key)


def set_release_artist_override(conn: sqlite3.Connection, release_id: int, artist: str | None) -> None:
    """Set (or, with `artist=None`, clear) the manual artist override for a release."""
    conn.execute("UPDATE releases SET artist_override = ? WHERE release_id = ?", (artist, release_id))


def set_release_title_override(conn: sqlite3.Connection, release_id: int, title: str | None) -> None:
    """Set (or, with `title=None`, clear) the manual title override for a release."""
    conn.execute("UPDATE releases SET title_override = ? WHERE release_id = ?", (title, release_id))


def effective_release_artist(release: sqlite3.Row) -> str:
    """The artist to use for a release: its manual override if set, else Discogs' own."""
    return release["artist_override"] or release["artist"]


def effective_release_title(release: sqlite3.Row) -> str:
    """The title to use for a release: its manual override if set, else Discogs' own."""
    return release["title_override"] or release["title"]


def set_release_styles_override(conn: sqlite3.Connection, release_id: int, styles: list[str] | None) -> None:
    """Set (or, with `styles=None`, clear) the manual styles override for a release.

    Used for the no-tracklist fallback row (no individual tracks to attach a per-track
    override to) — see `set_track_styles_override` for the normal, per-track path.
    """
    conn.execute(
        "UPDATE releases SET styles_override = ? WHERE release_id = ?",
        (json.dumps(styles) if styles is not None else None, release_id),
    )


def set_release_genres_override(conn: sqlite3.Connection, release_id: int, genres: list[str] | None) -> None:
    """Set (or, with `genres=None`, clear) the manual genres override for a release.

    Used for the no-tracklist fallback row (no individual tracks to attach a per-track
    override to) — see `set_track_genres_override` for the normal, per-track path.
    """
    conn.execute(
        "UPDATE releases SET genres_override = ? WHERE release_id = ?",
        (json.dumps(genres) if genres is not None else None, release_id),
    )


def effective_release_styles(release: sqlite3.Row) -> list[str]:
    """The styles to use for a release absent any per-track override: its own manual
    override if set, else Discogs' own. See `effective_track_styles` for the version
    that also accounts for a per-track correction."""
    override = release["styles_override"]
    return json.loads(override) if override is not None else (json.loads(release["styles"]) or [])


def effective_release_genres(release: sqlite3.Row) -> list[str]:
    """The genres to use for a release absent any per-track override: its own manual
    override if set, else Discogs' own. See `effective_track_genres` for the version
    that also accounts for a per-track correction."""
    override = release["genres_override"]
    return json.loads(override) if override is not None else (json.loads(release["genres"]) or [])


def set_track_styles_override(conn: sqlite3.Connection, track_id: int, styles: list[str] | None) -> None:
    """Set (or, with `styles=None`, clear) the manual styles override for one track.

    Discogs only reports styles per-release, so a release tagged e.g. "Tech House,
    Downtempo, Breaks" gives no way to know which track is which — this lets a user
    correct that per track instead of only for the whole release.
    """
    conn.execute(
        "UPDATE tracks SET styles_override = ? WHERE id = ?",
        (json.dumps(styles) if styles is not None else None, track_id),
    )


def set_track_genres_override(conn: sqlite3.Connection, track_id: int, genres: list[str] | None) -> None:
    """Set (or, with `genres=None`, clear) the manual genres override for one track."""
    conn.execute(
        "UPDATE tracks SET genres_override = ? WHERE id = ?",
        (json.dumps(genres) if genres is not None else None, track_id),
    )


def effective_track_styles(track: sqlite3.Row | None, release: sqlite3.Row) -> list[str]:
    """The styles to use for a track: its own manual override if set, else the release's
    effective styles (`effective_release_styles`). `track=None` (the no-tracklist
    fallback row) always falls back to the release."""
    if track is not None and track["styles_override"] is not None:
        return json.loads(track["styles_override"])
    return effective_release_styles(release)


def effective_track_genres(track: sqlite3.Row | None, release: sqlite3.Row) -> list[str]:
    """The genres to use for a track: its own manual override if set, else the release's
    effective genres (`effective_release_genres`). `track=None` (the no-tracklist
    fallback row) always falls back to the release."""
    if track is not None and track["genres_override"] is not None:
        return json.loads(track["genres_override"])
    return effective_release_genres(release)


def replace_tracks(
    conn: sqlite3.Connection, release_id: int, tracks: list[tuple[str, str, str | None, str | None]]
) -> None:
    """`tracks` is (position, title, duration, discogs_artist) — the last is Discogs' own
    per-track artist credit (see the `tracks.discogs_artist` column comment), or None.

    Upserts by matching each new (position, title) pair against an existing track rather
    than deleting and reinserting everything, so `search_artist`/`styles_override`/
    `genres_override` — manual per-track corrections — survive a `scan --refresh` instead
    of being silently wiped. A track whose
    (position, title) no longer appears in the new tracklist is removed; a genuinely new one
    is inserted. Ties among duplicate (position, title) pairs on the same release (rare, but
    seen in real Discogs data) are broken by original order.
    """
    conn.row_factory = sqlite3.Row
    existing = conn.execute(
        "SELECT id, position, title FROM tracks WHERE release_id = ? ORDER BY id", (release_id,)
    ).fetchall()
    remaining_by_key: dict[tuple[str, str], list[int]] = {}
    for row in existing:
        remaining_by_key.setdefault((row["position"], row["title"]), []).append(row["id"])

    keep_ids: set[int] = set()
    for pos, title, duration, discogs_artist in tracks:
        candidates = remaining_by_key.get((pos, title))
        if candidates:
            track_id = candidates.pop(0)
            conn.execute(
                "UPDATE tracks SET duration = ?, discogs_artist = ? WHERE id = ?",
                (duration, discogs_artist, track_id),
            )
            keep_ids.add(track_id)
        else:
            cur = conn.execute(
                "INSERT INTO tracks (release_id, position, title, duration, discogs_artist) VALUES (?, ?, ?, ?, ?)",
                (release_id, pos, title, duration, discogs_artist),
            )
            assert cur.lastrowid is not None  # row was just inserted above
            keep_ids.add(cur.lastrowid)

    stale_ids = [row["id"] for row in existing if row["id"] not in keep_ids]
    if stale_ids:
        conn.executemany("DELETE FROM tracks WHERE id = ?", [(i,) for i in stale_ids])


def has_tracks(conn: sqlite3.Connection, release_id: int) -> bool:
    """Whether a release already has at least one cached tracklist row."""
    row = conn.execute("SELECT 1 FROM tracks WHERE release_id = ? LIMIT 1", (release_id,)).fetchone()
    return row is not None


def get_match(conn: sqlite3.Connection, artist: str, title: str) -> sqlite3.Row | None:
    """Look up the cached YouTube match for an (artist, title) query, if any."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM matches WHERE query_key = ?", (match_key(artist, title),)).fetchone()


def save_match(
    conn: sqlite3.Connection,
    artist: str,
    title: str,
    video_id: str | None,
    video_title: str | None,
    source: str,
    score: float | None,
    channel: str | None = None,
) -> None:
    """Insert or overwrite the cached match for an (artist, title) query."""
    conn.execute(
        """INSERT INTO matches (query_key, video_id, video_title, source, score, channel, searched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(query_key) DO UPDATE SET
             video_id=excluded.video_id, video_title=excluded.video_title,
             source=excluded.source, score=excluded.score, channel=excluded.channel,
             searched_at=excluded.searched_at""",
        (match_key(artist, title), video_id, video_title, source, score, channel, time.time()),
    )


def get_match_by_id(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    """Look up a cached match by its surrogate id (as used by `correct`)."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def update_match(
    conn: sqlite3.Connection, match_id: int, video_id: str | None, video_title: str | None, source: str
) -> None:
    """Overwrite a match with a manually-supplied result. Score and channel are cleared —
    a human pick has no fuzzy score, and we don't look up the channel for a manual entry."""
    conn.execute(
        "UPDATE matches SET video_id = ?, video_title = ?, source = ?, score = NULL, "
        "channel = NULL, searched_at = ? WHERE id = ?",
        (video_id, video_title, source, time.time(), match_id),
    )


def delete_match(conn: sqlite3.Connection, match_id: int) -> None:
    """Forget a cached match entirely so the next sync searches for it again."""
    conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))


def count_matches(conn: sqlite3.Connection) -> int:
    """Total number of cached matches (searched or manually corrected)."""
    return conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]


def count_manual_matches(conn: sqlite3.Connection) -> int:
    """Number of cached matches that were manually corrected via `correct`."""
    return conn.execute("SELECT COUNT(*) FROM matches WHERE source = 'manual'").fetchone()[0]


def clear_all_matches(conn: sqlite3.Connection, include_manual: bool = False) -> int:
    """Forget cached matches so the next sync searches (and re-matches) them from scratch.
    Used to backfill fields added to a match after it was already cached (e.g. `channel`),
    since a normal sync skips anything already in the cache.

    Manually-corrected matches (`source == 'manual'`) are preserved by default — a human
    already resolved those, and a bulk resync shouldn't silently discard that. Pass
    `include_manual=True` to clear those too.
    """
    if include_manual:
        n = count_matches(conn)
        conn.execute("DELETE FROM matches")
        return n
    n = conn.execute("SELECT COUNT(*) FROM matches WHERE source != 'manual'").fetchone()[0]
    conn.execute("DELETE FROM matches WHERE source != 'manual'")
    return n


def get_track(conn: sqlite3.Connection, track_id: int) -> sqlite3.Row | None:
    """Look up a track by its surrogate id (as used by `fix-artist`/`fix-style`/`fix-genre`)."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


def get_release_tracks(conn: sqlite3.Connection, release_id: int) -> list[sqlite3.Row]:
    """All tracks for one release, in tracklist order. See `iter_releases_with_tracks` for
    the whole-collection equivalent this mirrors."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (release_id,)).fetchall()


def set_track_search_artist(conn: sqlite3.Connection, track_id: int, artist: str | None) -> None:
    """Set (or, with `artist=None`, clear) the manual search-artist override for a track."""
    conn.execute("UPDATE tracks SET search_artist = ? WHERE id = ?", (artist, track_id))


def get_release(conn: sqlite3.Connection, release_id: int) -> sqlite3.Row | None:
    """Look up a cached release by its Discogs release_id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()


def create_playlist(
    conn: sqlite3.Connection, name: str, source_type: str = DEFAULT_SOURCE_TYPE, source_key: str = DEFAULT_SOURCE_KEY
) -> int:
    """Create a new, empty curated playlist, tagged with the Discogs source it's built from.

    A playlist may only ever hold tracks from this one source (see `add_tracks_to_playlist`)
    — `source_type`/`source_key` default to "my own collection", matching every playlist
    created before this concept existed.

    Raises:
        sqlite3.IntegrityError: if the name is already taken.
    """
    now = time.time()
    conn.execute(
        "INSERT INTO playlists (name, source_type, source_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, source_type, source_key, now, now),
    )
    return conn.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()[0]


def get_playlist(conn: sqlite3.Connection, playlist_id: int) -> sqlite3.Row | None:
    """Look up a curated playlist by its id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()


def get_playlist_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Look up a curated playlist by its name."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlists WHERE name = ?", (name,)).fetchone()


def list_playlists(
    conn: sqlite3.Connection, source_type: str | None = None, source_key: str | None = None
) -> list[sqlite3.Row]:
    """Curated playlists with their track count, ordered by name.

    Pass `source_type`/`source_key` to narrow to playlists built from that one Discogs
    source (used by the add-to-playlist picker, so a different source's playlists never
    even appear as a target — see `add_tracks_to_playlist`). Omit both (the sidebar's use)
    to list every playlist regardless of source.
    """
    conn.row_factory = sqlite3.Row
    query = """SELECT p.*, COUNT(pt.track_id) AS track_count
               FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id"""
    params: tuple[str, ...] = ()
    if source_type is not None:
        query += " WHERE p.source_type = ? AND p.source_key = ?"
        params = (source_type, source_key or "")
    query += " GROUP BY p.id ORDER BY p.name"
    return conn.execute(query, params).fetchall()


def delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    """Delete a curated playlist and its track links (does not touch a linked YT Music playlist)."""
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


def set_playlist_ytmusic_id(conn: sqlite3.Connection, playlist_id: int, ytmusic_playlist_id: str) -> None:
    """Record the YT Music playlist id created for a curated playlist.

    Deliberately leaves `updated_at` untouched — that column tracks content edits (see
    `add_tracks_to_playlist`/`remove_tracks_from_playlist`), not linking. Use
    `set_playlist_pushed_at` to record that a push happened.
    """
    conn.execute(
        "UPDATE playlists SET ytmusic_playlist_id = ? WHERE id = ?",
        (ytmusic_playlist_id, playlist_id),
    )


def set_playlist_pushed_at(conn: sqlite3.Connection, playlist_id: int) -> None:
    """Record that a curated playlist was just successfully pushed to YT Music.

    Called on every successful push — both the first time a playlist is linked and every
    subsequent re-sync — so the UI can show an accurate "last pushed" time distinct from
    `updated_at` (content edits only).
    """
    conn.execute("UPDATE playlists SET pushed_at = ? WHERE id = ?", (time.time(), playlist_id))


def add_other_source(
    conn: sqlite3.Connection,
    source_type: str,
    source_key: str,
    display_name: str,
    filter_json: dict | None = None,
) -> int:
    """Register a new "Other sources" sidebar page, or return the existing one's id.

    `filter_json` is the source's pre-import Style/Format/Year filter (see
    `scan_engine.ImportFilter`), applied on every scan so only matching releases are ever
    imported under this source; omit/`None` for "import everything".

    Idempotent (`ON CONFLICT ... DO NOTHING`) so the "add source" UI flow can call this
    unconditionally for a pasted link — pasting the same link twice just navigates back
    to the same page instead of erroring or renaming/re-filtering it.
    """
    conn.execute(
        """INSERT INTO other_sources (source_type, source_key, display_name, filter_json, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_type, source_key) DO NOTHING""",
        (source_type, source_key, display_name, json.dumps(filter_json or {}), time.time()),
    )
    return conn.execute(
        "SELECT id FROM other_sources WHERE source_type = ? AND source_key = ?", (source_type, source_key)
    ).fetchone()[0]


def update_other_source_filter(conn: sqlite3.Connection, source_id: int, filter_json: dict) -> None:
    """Replace an existing "Other sources" page's pre-import filter in place.

    Lets a user revisit the Add-source page for a link they already added and change its
    Style/Format/Year pre-filter — without this, once a source exists, pasting its link
    again could only navigate to it, never change what it was set up to import.
    """
    conn.execute("UPDATE other_sources SET filter_json = ? WHERE id = ?", (json.dumps(filter_json), source_id))


def list_other_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every registered "Other sources" page, ordered by display name."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM other_sources ORDER BY display_name").fetchall()


def get_other_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    """Look up an "Other sources" page by its id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM other_sources WHERE id = ?", (source_id,)).fetchone()


def get_other_source_by_key(conn: sqlite3.Connection, source_type: str, source_key: str) -> sqlite3.Row | None:
    """Look up an "Other sources" page by its (source_type, source_key) — used to display
    a playlist's origin without needing to know the page's surrogate id."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM other_sources WHERE source_type = ? AND source_key = ?", (source_type, source_key)
    ).fetchone()


def delete_other_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Remove an "Other sources" page and forget which releases came from it.

    Leaves `releases`/`tracks`/`matches` rows untouched (same spirit as `delete_playlist`
    not touching track rows) — a release also tagged under another source, or referenced
    by a playlist, stays intact; one that was only ever tagged under this source simply
    stops appearing anywhere.
    """
    row = get_other_source(conn, source_id)
    if row is None:
        return
    conn.execute(
        "DELETE FROM release_sources WHERE source_type = ? AND source_key = ?",
        (row["source_type"], row["source_key"]),
    )
    conn.execute("DELETE FROM other_sources WHERE id = ?", (source_id,))


def create_playlist_folder(conn: sqlite3.Connection, name: str) -> int:
    """Create a new, empty playlist folder for organizing curated playlists.

    Args:
        conn: Open cache connection.
        name: Folder name; must be unique.

    Returns:
        The new folder's id.

    Raises:
        sqlite3.IntegrityError: if the name is already taken.
    """
    now = time.time()
    conn.execute("INSERT INTO playlist_folders (name, created_at, updated_at) VALUES (?, ?, ?)", (name, now, now))
    return conn.execute("SELECT id FROM playlist_folders WHERE name = ?", (name,)).fetchone()[0]


def get_playlist_folder(conn: sqlite3.Connection, folder_id: int) -> sqlite3.Row | None:
    """Look up a playlist folder by its id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_folders WHERE id = ?", (folder_id,)).fetchone()


def list_playlist_folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every playlist folder, ordered by name."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_folders ORDER BY name").fetchall()


def rename_playlist_folder(conn: sqlite3.Connection, folder_id: int, name: str) -> None:
    """Rename a playlist folder.

    Raises:
        sqlite3.IntegrityError: if the new name is already taken by another folder.
    """
    conn.execute("UPDATE playlist_folders SET name = ?, updated_at = ? WHERE id = ?", (name, time.time(), folder_id))


def delete_playlist_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    """Delete a playlist folder.

    Playlists inside the folder are not deleted — they're unassigned (`folder_id` set to
    NULL), consistent with a playlist belonging to at most one folder, or none.
    """
    conn.execute("UPDATE playlists SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
    conn.execute("DELETE FROM playlist_folders WHERE id = ?", (folder_id,))


def set_playlist_folder(conn: sqlite3.Connection, playlist_id: int, folder_id: int | None) -> None:
    """Move a curated playlist into a folder, or back to ungrouped.

    Args:
        conn: Open cache connection.
        playlist_id: The playlist to (re)assign.
        folder_id: The destination folder's id, or None to unassign it.
    """
    conn.execute("UPDATE playlists SET folder_id = ? WHERE id = ?", (folder_id, playlist_id))


def list_playlist_track_ids(conn: sqlite3.Connection, playlist_id: int) -> list[int]:
    """track_ids in a curated playlist, in playlist order."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,)
        )
    ]


def _track_belongs_to_source(conn: sqlite3.Connection, track_id: int, source_type: str, source_key: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM tracks t JOIN release_sources rs ON rs.release_id = t.release_id
           WHERE t.id = ? AND rs.source_type = ? AND rs.source_key = ? LIMIT 1""",
        (track_id, source_type, source_key),
    ).fetchone()
    return row is not None


def add_tracks_to_playlist(conn: sqlite3.Connection, playlist_id: int, track_ids: list[int]) -> int:
    """Append tracks to the end of a playlist, in order, skipping any already present.

    Silently drops any track whose release doesn't belong to this playlist's own Discogs
    source (`playlists.source_type`/`source_key`) — a playlist may only ever mix tracks
    from the one source it was created under. The UI never offers a cross-source track as
    a candidate in the first place (see `list_playlists`'s source filter); this is a
    store-level safety net against any other caller, in the same spirit as the
    manual-correction "locking" invariant documented in CLAUDE.md.

    Returns:
        How many tracks were actually added (excludes any dropped for a source mismatch).
    """
    playlist = get_playlist(conn, playlist_id)
    assert playlist is not None  # caller must pass a real playlist id
    track_ids = [
        tid for tid in track_ids if _track_belongs_to_source(conn, tid, playlist["source_type"], playlist["source_key"])
    ]
    existing = {
        row[0] for row in conn.execute("SELECT track_id FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    }
    next_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
    ).fetchone()[0]
    now = time.time()
    added = 0
    for track_id in track_ids:
        if track_id in existing:
            continue
        conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position, added_at) VALUES (?, ?, ?, ?)",
            (playlist_id, track_id, next_pos, now),
        )
        existing.add(track_id)
        next_pos += 1
        added += 1
    if added:
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, playlist_id))
    return added


def remove_tracks_from_playlist(conn: sqlite3.Connection, playlist_id: int, track_ids: list[int]) -> None:
    """Remove tracks from a curated playlist."""
    conn.executemany(
        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
        [(playlist_id, track_id) for track_id in track_ids],
    )
    conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), playlist_id))


def _split_va_track_title(release_artist: str, raw_title: str) -> tuple[str, str] | None:
    """Guess a per-track (artist, title) split for a various-artists release.

    Discogs sometimes credits a release to several artists (joined with commas
    in `release_artist`) without breaking out which artist owns which track —
    instead the track's own title is written as "Artist - Title". When that
    pattern shows up, split on it. Only used as a fallback when there's no
    explicit per-track override.
    """
    if "," not in release_artist or " - " not in raw_title:
        return None
    artist, _, title = raw_title.partition(" - ")
    artist, title = artist.strip(), title.strip()
    return (artist, title) if artist and title else None


def _is_untitled(title: str) -> bool:
    """True for Discogs' own "no title given" placeholder — exact match only (case-insensitive),
    so a real song that happens to be called e.g. "Untitled (How Does It Feel)" isn't caught."""
    return title.strip().lower() == "untitled"


def _resolve_untitled(release: sqlite3.Row, position: str, title: str) -> str:
    """An "Untitled" track has nothing useful to search on — fall back to the release
    title plus the track's own vinyl position (e.g. "Yoyaku Barcelona 2025 A2"), which is
    closer to how such tracks tend to actually get uploaded/labeled on YouTube."""
    if not _is_untitled(title):
        return title
    return f"{effective_release_title(release)} {position}".strip()


def effective_track_queries(release: sqlite3.Row, tracks: Sequence[sqlite3.Row]) -> list[tuple[int | None, str, str]]:
    """(track_id, artist, title) triples to search/display for a release.

    Resolves the artist per track, in priority order:
    1. a manual `search_artist` override (see `set_track_search_artist`) — always wins
    2. `discogs_artist` — Discogs' own structured per-track credit, when the API
       provided one (compilations/VA releases where a track is credited to a
       specific one of the release's several artists)
    3. `_split_va_track_title`'s guess, for releases where Discogs *didn't* give
       a structured per-track credit but the title text still embeds "Artist - Title"
    4. the release's own (possibly overridden, possibly multi-credit) artist string

    Whatever title results is then passed through `_resolve_untitled`, which only
    changes anything when Discogs' own title actually is the "Untitled" placeholder.
    """
    base_artist = effective_release_artist(release)
    if not tracks:
        # No tracklist on file — fall back to the release itself.
        return [(None, base_artist, effective_release_title(release))]

    triples = []
    for t in tracks:
        if t["search_artist"]:
            artist, title = t["search_artist"], t["title"]
        elif t["discogs_artist"]:
            artist, title = t["discogs_artist"], t["title"]
        else:
            guess = _split_va_track_title(base_artist, t["title"])
            artist, title = guess if guess else (base_artist, t["title"])
        title = _resolve_untitled(release, t["position"], title)
        triples.append((t["id"], artist, title))
    return triples


def iter_releases_with_tracks(
    conn: sqlite3.Connection,
    source_type: str = DEFAULT_SOURCE_TYPE,
    source_key: str = DEFAULT_SOURCE_KEY,
) -> Iterator[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Yield (release_row, [track_rows]) for every release tagged under the given source.

    Defaults to "my own collection", matching every call site that existed before the
    "Other sources" feature — pass a different source_type/source_key (an other_sources
    row's own) to browse a label/wantlist/other user's collection instead.
    """
    conn.row_factory = sqlite3.Row
    releases = conn.execute(
        """SELECT r.* FROM releases r
           JOIN release_sources rs ON rs.release_id = r.release_id
           WHERE rs.source_type = ? AND rs.source_key = ?
           ORDER BY r.release_id""",
        (source_type, source_key),
    ).fetchall()
    for r in releases:
        tracks = conn.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (r["release_id"],)).fetchall()
        yield r, tracks
