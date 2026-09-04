from __future__ import annotations

import time
from dataclasses import dataclass

import requests
from rapidfuzz import fuzz
from ytmusicapi import YTMusic

from .ytmusic_client import parse_video_id

YTMUSIC_THRESHOLD = 65.0
YTDLP_THRESHOLD = 55.0
DISCOGS_VIDEO_THRESHOLD = 65.0  # same scale/threshold as YTMUSIC_THRESHOLD — same scoring function either way

# Firing yt-dlp searches/lookups back-to-back with no gap tends to trip YouTube's
# bot detection (surfaces as sporadic "HTTP Error 403: Forbidden"). Throttle and
# retry through transient blips rather than just giving up on the first one.
YTDLP_MIN_INTERVAL = 1.5
YTDLP_MAX_RETRIES = 3
_last_ytdlp_call = 0.0

OEMBED_URL = "https://www.youtube.com/oembed"
OEMBED_TIMEOUT = 5


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
    source: str  # 'ytmusic' | 'ytdlp' | 'discogs' | 'none'
    score: float
    channel: str | None = None


def _query_string(artist: str, title: str) -> str:
    return f"{artist} {title}"


def _score(query: str, candidate: str) -> float:
    return fuzz.token_set_ratio(query.lower(), candidate.lower())


def _throttle_ytdlp() -> None:
    global _last_ytdlp_call
    elapsed = time.monotonic() - _last_ytdlp_call
    if elapsed < YTDLP_MIN_INTERVAL:
        time.sleep(YTDLP_MIN_INTERVAL - elapsed)
    _last_ytdlp_call = time.monotonic()


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
            channel = (r.get("artists") or [{}])[0].get("name")
            best = MatchResult(video_id, r.get("title"), "ytmusic", score, channel=channel)
    if best and best.score >= YTMUSIC_THRESHOLD:
        return best
    return None


def search_ytdlp(artist: str, title: str) -> MatchResult | None:
    import yt_dlp

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
        _throttle_ytdlp()
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
            channel = e.get("channel") or e.get("uploader")
            best = MatchResult(e["id"], e.get("title"), "ytdlp", score, channel=channel)
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


def match_against_discogs_videos(
    videos: list[dict], track_queries: list[tuple[int | None, str, str]]
) -> dict[int, MatchResult]:
    """Match a release's Discogs-embedded videos (each {"uri", "title", "duration"})
    against its resolved (track_id, artist, title) queries.

    Scores every (track, video) pair the same way a YouTube search result would be
    scored, then greedily assigns highest-scoring pairs first — each track and each
    video used at most once. This is what keeps a release with more videos than
    tracks (bonus remixes, live sets, etc.) from grabbing the wrong one: an
    unrelated video simply won't score above the threshold against any track, and
    a video that's ambiguous between two tracks only gets handed to whichever one
    it matches best.

    Returns a dict of track_id -> MatchResult (source='discogs') for confident
    matches only; a track absent from the result should fall back to a search.
    """
    if not videos or not track_queries:
        return {}

    candidates = []
    for track_id, artist, title in track_queries:
        if track_id is None:
            continue
        query = _query_string(artist, title)
        for video_index, video in enumerate(videos):
            score = _score(query, video.get("title", ""))
            if score >= DISCOGS_VIDEO_THRESHOLD:
                candidates.append((score, track_id, video_index))
    candidates.sort(key=lambda c: -c[0])

    used_tracks: set[int] = set()
    used_videos: set[int] = set()
    results: dict[int, MatchResult] = {}
    for score, track_id, video_index in candidates:
        if track_id in used_tracks or video_index in used_videos:
            continue
        video = videos[video_index]
        try:
            video_id = parse_video_id(video["uri"])
        except (ValueError, KeyError):
            continue
        used_tracks.add(track_id)
        used_videos.add(video_index)
        results[track_id] = MatchResult(video_id, video.get("title"), "discogs", score)
    return results


def resolve_channel(video_id: str) -> str | None:
    """Best-effort channel lookup for a video whose id we already have (used to
    backfill the Channel column for a Discogs-sourced match, which — unlike a
    search result — doesn't already carry this).

    Uses YouTube's public oEmbed endpoint rather than a full yt-dlp extraction:
    it's a small dedicated metadata lookup, not page scraping, so it doesn't
    trip the same bot detection yt-dlp search/extract does and needs none of
    that throttling — much faster when called once per matched track. Never
    raises; a failed lookup just leaves the channel blank rather than blocking
    the whole sync."""
    try:
        resp = requests.get(
            OEMBED_URL,
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=OEMBED_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("author_name") or None
    except Exception:
        return None
