from __future__ import annotations

import time
from dataclasses import dataclass

from rapidfuzz import fuzz
from ytmusicapi import YTMusic

YTMUSIC_THRESHOLD = 65.0
YTDLP_THRESHOLD = 55.0

# Firing yt-dlp searches back-to-back with no gap tends to trip YouTube's bot
# detection (surfaces as sporadic "HTTP Error 403: Forbidden"). Throttle and
# retry through transient blips rather than just giving up on the first one.
YTDLP_MIN_INTERVAL = 1.5
YTDLP_MAX_RETRIES = 3
_last_ytdlp_call = 0.0


class _SilentLogger:
    """Swallows yt-dlp's own stderr logging — failures are handled/counted by us."""

    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


@dataclass
class MatchResult:
    video_id: str | None
    video_title: str | None
    source: str  # 'ytmusic' | 'ytdlp' | 'none'
    score: float


def _query_string(artist: str, title: str) -> str:
    return f"{artist} {title}"


def _score(query: str, candidate: str) -> float:
    return fuzz.token_set_ratio(query.lower(), candidate.lower())


def search_ytmusic(yt: YTMusic, artist: str, title: str) -> MatchResult | None:
    query = _query_string(artist, title)
    best: MatchResult | None = None
    try:
        results = yt.search(query, filter="songs", limit=5) or []
        results += yt.search(query, filter="videos", limit=5) or []
    except Exception:
        results = []
    for r in results:
        video_id = r.get("videoId")
        if not video_id:
            continue
        artists = ", ".join(a.get("name", "") for a in r.get("artists", []) or [])
        candidate_title = f"{artists} {r.get('title', '')}"
        score = _score(query, candidate_title)
        if best is None or score > best.score:
            best = MatchResult(video_id, r.get("title"), "ytmusic", score)
    if best and best.score >= YTMUSIC_THRESHOLD:
        return best
    return None


def search_ytdlp(artist: str, title: str) -> MatchResult | None:
    import yt_dlp

    global _last_ytdlp_call

    query = _query_string(artist, title)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch5",
        "noplaylist": True,
        "logger": _SilentLogger(),
    }

    entries = None
    for attempt in range(YTDLP_MAX_RETRIES):
        elapsed = time.monotonic() - _last_ytdlp_call
        if elapsed < YTDLP_MIN_INTERVAL:
            time.sleep(YTDLP_MIN_INTERVAL - elapsed)
        _last_ytdlp_call = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                entries = info.get("entries", []) if info else []
            break
        except Exception:
            if attempt == YTDLP_MAX_RETRIES - 1:
                return None
            time.sleep(2 * (attempt + 1))  # back off harder on repeated failures

    best: MatchResult | None = None
    for e in entries or []:
        if not e or not e.get("id"):
            continue
        score = _score(query, e.get("title", ""))
        if best is None or score > best.score:
            best = MatchResult(e["id"], e.get("title"), "ytdlp", score)
    if best and best.score >= YTDLP_THRESHOLD:
        return best
    return None


def find_match(yt: YTMusic, artist: str, title: str) -> MatchResult:
    result = search_ytmusic(yt, artist, title)
    if result:
        return result
    result = search_ytdlp(artist, title)
    if result:
        return result
    return MatchResult(None, None, "none", 0.0)
