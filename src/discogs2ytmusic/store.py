from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
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

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ytmusic_playlist_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
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
"""


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
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def match_key(artist: str, title: str) -> str:
    """Build the normalized `matches.query_key` for an (artist, title) pair."""
    return f"{artist.strip().lower()}||{title.strip().lower()}"


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
) -> None:
    """Insert or refresh a release's Discogs-sourced fields.

    `videos` is Discogs' own embedded YouTube links for the release (each a
    dict with "uri"/"title"/"duration"), used by the sync matcher before it
    falls back to searching YouTube itself. Pass `None` (the default) to leave
    whatever's already cached untouched — `scan`'s basic-collection pass calls
    this for every release on every run, but only the (rarer) full tracklist
    fetch actually has fresh video data to offer.

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
    """Look up a cached match by its surrogate id (the id shown by `export`)."""
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
    """Look up a track by its surrogate id (the id shown by `export`)."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


def set_track_search_artist(conn: sqlite3.Connection, track_id: int, artist: str | None) -> None:
    """Set (or, with `artist=None`, clear) the manual search-artist override for a track."""
    conn.execute("UPDATE tracks SET search_artist = ? WHERE id = ?", (artist, track_id))


def get_playlist_def_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Look up a saved playlist definition by its name."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_defs WHERE name = ?", (name,)).fetchone()


def get_playlist_def(conn: sqlite3.Connection, def_id: int) -> sqlite3.Row | None:
    """Look up a saved playlist definition by its id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_defs WHERE id = ?", (def_id,)).fetchone()


def list_playlist_defs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All saved playlist definitions, alphabetical by name."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_defs ORDER BY name").fetchall()


def upsert_playlist_def(conn: sqlite3.Connection, name: str, filter_json: str) -> int:
    """Create a playlist def, or update its filter if the name already exists. Returns its id."""
    now = time.time()
    conn.execute(
        """INSERT INTO playlist_defs (name, filter_json, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET filter_json=excluded.filter_json, updated_at=excluded.updated_at""",
        (name, filter_json, now, now),
    )
    row = get_playlist_def_by_name(conn, name)
    assert row is not None  # just upserted above
    return row["id"]


def set_playlist_def_ytmusic_id(conn: sqlite3.Connection, def_id: int, ytmusic_playlist_id: str) -> None:
    """Record the YT Music playlist id created for a playlist definition."""
    conn.execute(
        "UPDATE playlist_defs SET ytmusic_playlist_id = ?, updated_at = ? WHERE id = ?",
        (ytmusic_playlist_id, time.time(), def_id),
    )


def delete_playlist_def(conn: sqlite3.Connection, def_id: int) -> None:
    """Delete a saved playlist definition (does not touch the YT Music playlist itself)."""
    conn.execute("DELETE FROM playlist_defs WHERE id = ?", (def_id,))


def get_release(conn: sqlite3.Connection, release_id: int) -> sqlite3.Row | None:
    """Look up a cached release by its Discogs release_id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()


def create_playlist(conn: sqlite3.Connection, name: str) -> int:
    """Create a new, empty curated playlist.

    Raises:
        sqlite3.IntegrityError: if the name is already taken.
    """
    now = time.time()
    conn.execute("INSERT INTO playlists (name, created_at, updated_at) VALUES (?, ?, ?)", (name, now, now))
    return conn.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()[0]


def get_playlist(conn: sqlite3.Connection, playlist_id: int) -> sqlite3.Row | None:
    """Look up a curated playlist by its id."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()


def get_playlist_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Look up a curated playlist by its name."""
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlists WHERE name = ?", (name,)).fetchone()


def list_playlists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every curated playlist with its track count, ordered by name."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """SELECT p.*, COUNT(pt.track_id) AS track_count
           FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
           GROUP BY p.id ORDER BY p.name"""
    ).fetchall()


def delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    """Delete a curated playlist and its track links (does not touch a linked YT Music playlist)."""
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


def set_playlist_ytmusic_id(conn: sqlite3.Connection, playlist_id: int, ytmusic_playlist_id: str) -> None:
    """Record the YT Music playlist id created for a curated playlist."""
    conn.execute(
        "UPDATE playlists SET ytmusic_playlist_id = ?, updated_at = ? WHERE id = ?",
        (ytmusic_playlist_id, time.time(), playlist_id),
    )


def list_playlist_track_ids(conn: sqlite3.Connection, playlist_id: int) -> list[int]:
    """track_ids in a curated playlist, in playlist order."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,)
        )
    ]


def add_tracks_to_playlist(conn: sqlite3.Connection, playlist_id: int, track_ids: list[int]) -> int:
    """Append tracks to the end of a playlist, in order, skipping any already present.

    Returns:
        How many tracks were actually added.
    """
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
) -> Iterator[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Yield (release_row, [track_rows]) for everything cached."""
    conn.row_factory = sqlite3.Row
    releases = conn.execute("SELECT * FROM releases").fetchall()
    for r in releases:
        tracks = conn.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (r["release_id"],)).fetchall()
        yield r, tracks
