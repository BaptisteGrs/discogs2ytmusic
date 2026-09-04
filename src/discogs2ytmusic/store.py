from __future__ import annotations

import json
import sqlite3
import time
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
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    position TEXT NOT NULL,
    title TEXT NOT NULL,
    duration TEXT,
    search_artist TEXT,   -- optional override for the artist used to search YouTube; NULL falls back to releases.artist
    FOREIGN KEY (release_id) REFERENCES releases(release_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_release_id ON tracks(release_id);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_key TEXT NOT NULL UNIQUE,   -- "artist||title"
    video_id TEXT,                -- NULL means "searched, no confident match"
    video_title TEXT,
    source TEXT,                  -- 'ytmusic' | 'ytdlp' | 'none'
    score REAL,
    searched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    filter_json TEXT NOT NULL,   -- {"tags": [...], "labels": [...], "year_min": int|null, "year_max": int|null, "matched_only": bool}
    ytmusic_playlist_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _migrate_matches_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for caches created before `matches` had a surrogate id column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
    if not cols or "id" in cols:
        return
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
            searched_at REAL NOT NULL
        );
        INSERT INTO matches (query_key, video_id, video_title, source, score, searched_at)
            SELECT query_key, video_id, video_title, source, score, searched_at FROM matches_old;
        DROP TABLE matches_old;
        """
    )
    conn.commit()


def _migrate_tracks_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for caches created before `tracks` had a search_artist override column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    if cols and "search_artist" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN search_artist TEXT")
        conn.commit()


def _migrate_releases_table(conn: sqlite3.Connection) -> None:
    """One-time upgrade for caches created before `releases` had year/labels/overrides columns."""
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
    conn.commit()


def _migrate_playlists_to_playlist_defs(conn: sqlite3.Connection) -> None:
    """One-time upgrade: fold the old style->playlist_id table into playlist_defs.

    Each style becomes its own filter-based playlist def (tags=[style]), preserving
    the already-created YT Music playlist id so re-syncing doesn't create duplicates.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(playlists)")}
    if not cols:
        return  # no legacy table — nothing to migrate
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
def connect():
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
    return f"{artist.strip().lower()}||{title.strip().lower()}"


def upsert_release(
    conn,
    release_id: int,
    artist: str,
    title: str,
    styles: list[str],
    genres: list[str],
    year: int | None = None,
    labels: list[str] | None = None,
) -> None:
    """Insert or refresh a release's Discogs-sourced fields.

    Deliberately does not touch artist_override/title_override — a re-scan
    (e.g. `scan --refresh`) must not wipe out manual corrections made via the
    browsable UI.
    """
    conn.execute(
        """INSERT INTO releases (release_id, artist, title, styles, genres, year, labels, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(release_id) DO UPDATE SET
             artist=excluded.artist, title=excluded.title,
             styles=excluded.styles, genres=excluded.genres,
             year=excluded.year, labels=excluded.labels, fetched_at=excluded.fetched_at""",
        (release_id, artist, title, json.dumps(styles), json.dumps(genres), year, json.dumps(labels or []), time.time()),
    )


def set_release_artist_override(conn, release_id: int, artist: str | None) -> None:
    conn.execute("UPDATE releases SET artist_override = ? WHERE release_id = ?", (artist, release_id))


def set_release_title_override(conn, release_id: int, title: str | None) -> None:
    conn.execute("UPDATE releases SET title_override = ? WHERE release_id = ?", (title, release_id))


def effective_release_artist(release) -> str:
    return release["artist_override"] or release["artist"]


def effective_release_title(release) -> str:
    return release["title_override"] or release["title"]


def replace_tracks(conn, release_id: int, tracks: list[tuple[str, str, str | None]]) -> None:
    conn.execute("DELETE FROM tracks WHERE release_id = ?", (release_id,))
    conn.executemany(
        "INSERT INTO tracks (release_id, position, title, duration) VALUES (?, ?, ?, ?)",
        [(release_id, pos, title, dur) for pos, title, dur in tracks],
    )


def has_tracks(conn, release_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM tracks WHERE release_id = ? LIMIT 1", (release_id,)).fetchone()
    return row is not None


def get_match(conn, artist: str, title: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM matches WHERE query_key = ?", (match_key(artist, title),)
    ).fetchone()


def save_match(conn, artist: str, title: str, video_id: str | None, video_title: str | None, source: str, score: float) -> None:
    conn.execute(
        """INSERT INTO matches (query_key, video_id, video_title, source, score, searched_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(query_key) DO UPDATE SET
             video_id=excluded.video_id, video_title=excluded.video_title,
             source=excluded.source, score=excluded.score, searched_at=excluded.searched_at""",
        (match_key(artist, title), video_id, video_title, source, score, time.time()),
    )


def get_match_by_id(conn, match_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def update_match(conn, match_id: int, video_id: str | None, video_title: str | None, source: str) -> None:
    """Overwrite a match with a manually-supplied result. Score is cleared — a human pick has no fuzzy score."""
    conn.execute(
        "UPDATE matches SET video_id = ?, video_title = ?, source = ?, score = NULL, searched_at = ? WHERE id = ?",
        (video_id, video_title, source, time.time(), match_id),
    )


def delete_match(conn, match_id: int) -> None:
    conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))


def get_track(conn, track_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


def set_track_search_artist(conn, track_id: int, artist: str | None) -> None:
    conn.execute("UPDATE tracks SET search_artist = ? WHERE id = ?", (artist, track_id))


def get_playlist_def_by_name(conn, name: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_defs WHERE name = ?", (name,)).fetchone()


def get_playlist_def(conn, def_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_defs WHERE id = ?", (def_id,)).fetchone()


def list_playlist_defs(conn) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM playlist_defs ORDER BY name").fetchall()


def upsert_playlist_def(conn, name: str, filter_json: str) -> int:
    """Create a playlist def, or update its filter if the name already exists. Returns its id."""
    now = time.time()
    conn.execute(
        """INSERT INTO playlist_defs (name, filter_json, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET filter_json=excluded.filter_json, updated_at=excluded.updated_at""",
        (name, filter_json, now, now),
    )
    return get_playlist_def_by_name(conn, name)["id"]


def set_playlist_def_ytmusic_id(conn, def_id: int, ytmusic_playlist_id: str) -> None:
    conn.execute(
        "UPDATE playlist_defs SET ytmusic_playlist_id = ?, updated_at = ? WHERE id = ?",
        (ytmusic_playlist_id, time.time(), def_id),
    )


def delete_playlist_def(conn, def_id: int) -> None:
    conn.execute("DELETE FROM playlist_defs WHERE id = ?", (def_id,))


def effective_track_queries(release, tracks) -> list[tuple[int | None, str, str]]:
    """(track_id, artist, title) triples to search/display for a release.

    Honors a per-track `search_artist` override (see `set_track_search_artist`)
    over the release's own (possibly overridden, possibly multi-credit) artist string.
    """
    base_artist = effective_release_artist(release)
    if not tracks:
        return [(None, base_artist, effective_release_title(release))]  # no tracklist on file — fall back to the release itself
    return [(t["id"], t["search_artist"] or base_artist, t["title"]) for t in tracks]


def iter_releases_with_tracks(conn):
    """Yield (release_row, [track_rows]) for everything cached."""
    conn.row_factory = sqlite3.Row
    releases = conn.execute("SELECT * FROM releases").fetchall()
    for r in releases:
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (r["release_id"],)
        ).fetchall()
        yield r, tracks
