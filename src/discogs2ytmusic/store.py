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
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    release_id INTEGER NOT NULL,
    position TEXT NOT NULL,
    title TEXT NOT NULL,
    duration TEXT,
    PRIMARY KEY (release_id, position),
    FOREIGN KEY (release_id) REFERENCES releases(release_id)
);

CREATE TABLE IF NOT EXISTS matches (
    query_key TEXT PRIMARY KEY,   -- "artist||title"
    video_id TEXT,                -- NULL means "searched, no confident match"
    video_title TEXT,
    source TEXT,                  -- 'ytmusic' | 'ytdlp' | 'none'
    score REAL,
    searched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    style TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


@contextmanager
def connect():
    ensure_dirs()
    conn = sqlite3.connect(CACHE_DB)
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def match_key(artist: str, title: str) -> str:
    return f"{artist.strip().lower()}||{title.strip().lower()}"


def upsert_release(conn, release_id: int, artist: str, title: str, styles: list[str], genres: list[str]) -> None:
    conn.execute(
        """INSERT INTO releases (release_id, artist, title, styles, genres, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(release_id) DO UPDATE SET
             artist=excluded.artist, title=excluded.title,
             styles=excluded.styles, genres=excluded.genres, fetched_at=excluded.fetched_at""",
        (release_id, artist, title, json.dumps(styles), json.dumps(genres), time.time()),
    )


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


def get_playlist_id(conn, style: str) -> str | None:
    row = conn.execute("SELECT playlist_id FROM playlists WHERE style = ?", (style,)).fetchone()
    return row[0] if row else None


def save_playlist_id(conn, style: str, playlist_id: str) -> None:
    conn.execute(
        """INSERT INTO playlists (style, playlist_id, created_at) VALUES (?, ?, ?)
           ON CONFLICT(style) DO UPDATE SET playlist_id=excluded.playlist_id""",
        (style, playlist_id, time.time()),
    )


def iter_releases_with_tracks(conn):
    """Yield (release_row, [track_rows]) for everything cached."""
    conn.row_factory = sqlite3.Row
    releases = conn.execute("SELECT * FROM releases").fetchall()
    for r in releases:
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY position", (r["release_id"],)
        ).fetchall()
        yield r, tracks
