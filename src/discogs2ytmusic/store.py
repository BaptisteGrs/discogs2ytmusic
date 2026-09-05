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
    videos TEXT NOT NULL DEFAULT '[]',   -- json list of {"uri", "title", "duration"} — Discogs' own embedded YouTube links
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL,
    position TEXT NOT NULL,
    title TEXT NOT NULL,
    duration TEXT,
    search_artist TEXT,   -- manual override for the artist used to search YouTube; NULL falls back to discogs_artist/releases.artist
    discogs_artist TEXT,  -- Discogs' own per-track artist credit (compilations/VA releases only; NULL when it's just the release artist)
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
    filter_json TEXT NOT NULL,   -- {"tags": [...], "labels": [...], "year_min": int|null, "year_max": int|null, "matched_only": bool}
    ytmusic_playlist_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
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
    """One-time upgrade for caches created before `tracks` had search_artist/discogs_artist columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    if not cols:
        return
    if "search_artist" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN search_artist TEXT")
    if "discogs_artist" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN discogs_artist TEXT")
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
    videos: list[dict] | None = None,
) -> None:
    """Insert or refresh a release's Discogs-sourced fields.

    `videos` is Discogs' own embedded YouTube links for the release (each a
    dict with "uri"/"title"/"duration"), used by the sync matcher before it
    falls back to searching YouTube itself. Pass `None` (the default) to leave
    whatever's already cached untouched — `scan`'s basic-collection pass calls
    this for every release on every run, but only the (rarer) full tracklist
    fetch actually has fresh video data to offer.

    Deliberately does not touch artist_override/title_override — a re-scan
    (e.g. `scan --refresh`) must not wipe out manual corrections made via the
    browsable UI.
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
            release_id, artist, title, json.dumps(styles), json.dumps(genres),
            year, json.dumps(labels or []), videos_json if videos_json is not None else "[]", time.time(),
            videos_json,
        ),
    )


def set_release_artist_override(conn, release_id: int, artist: str | None) -> None:
    conn.execute("UPDATE releases SET artist_override = ? WHERE release_id = ?", (artist, release_id))


def set_release_title_override(conn, release_id: int, title: str | None) -> None:
    conn.execute("UPDATE releases SET title_override = ? WHERE release_id = ?", (title, release_id))


def effective_release_artist(release) -> str:
    return release["artist_override"] or release["artist"]


def effective_release_title(release) -> str:
    return release["title_override"] or release["title"]


def replace_tracks(conn, release_id: int, tracks: list[tuple[str, str, str | None, str | None]]) -> None:
    """`tracks` is (position, title, duration, discogs_artist) — the last is Discogs' own
    per-track artist credit (see the `tracks.discogs_artist` column comment), or None.

    Upserts by matching each new (position, title) pair against an existing track rather
    than deleting and reinserting everything, so `search_artist` — a manual per-track
    correction — survives a `scan --refresh` instead of being silently wiped. A track whose
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
            keep_ids.add(cur.lastrowid)

    stale_ids = [row["id"] for row in existing if row["id"] not in keep_ids]
    if stale_ids:
        conn.executemany("DELETE FROM tracks WHERE id = ?", [(i,) for i in stale_ids])


def has_tracks(conn, release_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM tracks WHERE release_id = ? LIMIT 1", (release_id,)).fetchone()
    return row is not None


def get_match(conn, artist: str, title: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM matches WHERE query_key = ?", (match_key(artist, title),)
    ).fetchone()


def save_match(
    conn, artist: str, title: str, video_id: str | None, video_title: str | None,
    source: str, score: float | None, channel: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO matches (query_key, video_id, video_title, source, score, channel, searched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(query_key) DO UPDATE SET
             video_id=excluded.video_id, video_title=excluded.video_title,
             source=excluded.source, score=excluded.score, channel=excluded.channel,
             searched_at=excluded.searched_at""",
        (match_key(artist, title), video_id, video_title, source, score, channel, time.time()),
    )


def get_match_by_id(conn, match_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def update_match(conn, match_id: int, video_id: str | None, video_title: str | None, source: str) -> None:
    """Overwrite a match with a manually-supplied result. Score and channel are cleared —
    a human pick has no fuzzy score, and we don't look up the channel for a manual entry."""
    conn.execute(
        "UPDATE matches SET video_id = ?, video_title = ?, source = ?, score = NULL, channel = NULL, searched_at = ? WHERE id = ?",
        (video_id, video_title, source, time.time(), match_id),
    )


def delete_match(conn, match_id: int) -> None:
    conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))


def count_matches(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]


def count_manual_matches(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM matches WHERE source = 'manual'").fetchone()[0]


def clear_all_matches(conn, include_manual: bool = False) -> int:
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


def _resolve_untitled(release, position: str, title: str) -> str:
    """An "Untitled" track has nothing useful to search on — fall back to the release
    title plus the track's own vinyl position (e.g. "Yoyaku Barcelona 2025 A2"), which is
    closer to how such tracks tend to actually get uploaded/labeled on YouTube."""
    if not _is_untitled(title):
        return title
    return f"{effective_release_title(release)} {position}".strip()


def effective_track_queries(release, tracks) -> list[tuple[int | None, str, str]]:
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
        return [(None, base_artist, effective_release_title(release))]  # no tracklist on file — fall back to the release itself

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


def iter_releases_with_tracks(conn):
    """Yield (release_row, [track_rows]) for everything cached."""
    conn.row_factory = sqlite3.Row
    releases = conn.execute("SELECT * FROM releases").fetchall()
    for r in releases:
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (r["release_id"],)
        ).fetchall()
        yield r, tracks
