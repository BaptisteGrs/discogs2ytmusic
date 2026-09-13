"""Fetch a Discogs collection into the sqlite cache.

Factored out of `cli.py`'s `scan` command so the same per-release cache-write logic is
reusable from the Streamlit UI (`app.py`'s Scan button) without going through the CLI —
each caller drives its own progress display (`rich.Progress` for the CLI, `st.progress`
for the UI) around `scan_release`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import store
from .discogs import DiscogsClient


def _clean_artist_names(names: list[str]) -> str | None:
    """Join Discogs artist credits into one display string, stripping each name's own
    disambiguation suffix (e.g. "Rush (2)") before joining — joining first and stripping
    only the tail would miss any but the last name."""
    if not names:
        return None
    cleaned = [re.sub(r"\s*\(\d+\)$", "", n).strip() for n in names]
    return ", ".join(n for n in cleaned if n) or None


@dataclass
class ImportFilter:
    """An Other-source page's pre-import Style/Format/Year filter (see `other_sources.filter_json`).

    Applied while scanning so a release that doesn't match is never imported under that
    source at all — re-applied on every scan (not just the first), so a release that no
    longer matches gets pruned (`store.prune_release_source_tags`) instead of lingering.
    An all-empty filter (every field falsy) matches everything, same as no filter at all.

    `styles` is matched against a release's combined styles + genres (mirroring how the
    Collection tab's own "Style" filter already treats the two as one tag space — see
    `filters._tag_options`). `formats` is matched as a case-insensitive substring against
    the release's raw format text (e.g. "Vinyl, LP, Album" or "CD, Mixed") rather than an
    exact set, since Discogs' own format shape differs between a collection/wantlist item
    (a structured list of {name, descriptions}) and a label-listing item (a single joined
    string) — substring matching handles both uniformly.
    """

    styles: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    year_min: int | None = None
    year_max: int | None = None

    def is_empty(self) -> bool:
        """Whether this filter matches everything (no criteria set)."""
        return not (self.styles or self.formats) and self.year_min is None and self.year_max is None

    def matches(self, *, styles: list[str] | None, format_text: str, year: int | None) -> bool:
        """Whether a release passes this filter.

        `styles=None` skips the style check entirely — used by `scan_label_release` for a
        cheap pre-check (format/year are free from the listing item; style would require
        an API call to know) before deciding whether that call is even worth making.
        """
        if self.styles and styles is not None:
            wanted = {s.strip().lower() for s in self.styles}
            got = {s.strip().lower() for s in styles}
            if not (wanted & got):
                return False
        if self.formats:
            text = format_text.lower()
            if not any(f.strip().lower() in text for f in self.formats if f.strip()):
                return False
        if self.year_min is not None and (year is None or year < self.year_min):
            return False
        return not (self.year_max is not None and (year is None or year > self.year_max))

    def to_dict(self) -> dict[str, Any]:
        """Serialize for `other_sources.filter_json` (via `store.add_other_source`)."""
        return {"styles": self.styles, "formats": self.formats, "year_min": self.year_min, "year_max": self.year_max}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportFilter:
        """Rebuild from `other_sources.filter_json` (already `json.loads`'d)."""
        return cls(
            styles=data.get("styles") or [],
            formats=data.get("formats") or [],
            year_min=data.get("year_min"),
            year_max=data.get("year_max"),
        )


def _format_tokens(formats: list[dict[str, Any]]) -> list[str]:
    """Flatten a collection/wantlist item's `basic_information.formats` (a list of
    {"name", "descriptions"} dicts) into individual tokens, e.g. ["Vinyl", "12\"", "Album"]
    — both the atoms `store.record_known_formats` remembers for the Add-source page's
    Format picklist, and (joined with ", ") the text `ImportFilter.matches` checks against.
    A label-listing item's own single joined format string (e.g. "CD, Mixed") is split the
    same way by its caller (`scan_label_release`) so both shapes end up equally granular.
    """
    parts = []
    for f in formats or []:
        name = f.get("name")
        if name:
            parts.append(name)
        parts.extend(f.get("descriptions", []) or [])
    return parts


def scan_release(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    item: dict[str, Any],
    refresh: bool,
    source_type: str = store.DEFAULT_SOURCE_TYPE,
    source_key: str = store.DEFAULT_SOURCE_KEY,
    import_filter: ImportFilter | None = None,
) -> bool:
    """Upsert one Discogs collection/wantlist item (and its tracklist, if needed) into the cache.

    Args:
        conn: Open sqlite connection.
        client: An authenticated Discogs client, used to fetch tracklist detail when needed.
        item: One item as yielded by `DiscogsClient.iter_collection_basic`/`iter_wantlist_basic`
            — both are shaped alike (a `basic_information` dict), so this works for either.
        refresh: Re-fetch and replace the tracklist even if one is already cached.
        source_type: Which Discogs source this item came from (see `store.release_sources`)
            — defaults to "my own collection". Pass "user_collection"/"wantlist" for an
            Other-source page scan.
        source_key: The source's own key (username), paired with `source_type`.
        import_filter: An Other-source page's pre-import Style/Format/Year filter, if any.
            Everything needed to check it (styles/genres/formats/year) is already in
            `basic_information`, so a non-matching item is skipped before any API call or
            cache write — no tracklist fetch, no upsert, not even a source tag. Every
            format token seen is recorded via `store.record_known_formats` regardless of
            whether it matches, growing the Add-source page's Format picklist for next time.

    Returns:
        True if the release was imported/tagged, False if `import_filter` excluded it —
        callers use this to know which releases should survive a filtered re-scan's prune
        (see `store.prune_release_source_tags`).

    Commits immediately, so a caller looping over many releases doesn't lose progress on
    a crash/interrupt.
    """
    info = item["basic_information"]
    release_id = info["id"]
    artist = ", ".join(a["name"] for a in info.get("artists", []))
    artist = re.sub(r"\s*\(\d+\)$", "", artist)  # strip Discogs disambiguation suffixes e.g. "Rush (2)"
    title = info.get("title", "")
    styles = info.get("styles", []) or []
    genres = info.get("genres", []) or []
    year = info.get("year") or None
    labels = [label["name"] for label in info.get("labels", []) or [] if label.get("name")]

    format_tokens = _format_tokens(info.get("formats", []))
    store.record_known_formats(conn, format_tokens)

    if import_filter is not None and not import_filter.matches(
        styles=[*styles, *genres], format_text=", ".join(format_tokens), year=year
    ):
        return False

    videos = None  # None means "don't touch whatever's already cached" (see store.upsert_release)
    if refresh or not store.has_tracks(conn, release_id):
        detail = client.get_release_detail(release_id)
        store.replace_tracks(
            conn,
            release_id,
            [(t.position, t.title, t.duration, _clean_artist_names(t.artists)) for t in detail.tracklist],
        )
        videos = [{"uri": v.uri, "title": v.title, "duration": v.duration} for v in detail.videos]

    store.upsert_release(
        conn,
        release_id,
        artist,
        title,
        styles,
        genres,
        year=year,
        labels=labels,
        videos=videos,
        source_type=source_type,
        source_key=source_key,
    )
    conn.commit()
    return True


def scan_label_release(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    item: dict[str, Any],
    refresh: bool,
    source_key: str,
    source_type: str = "label",
    import_filter: ImportFilter | None = None,
) -> bool:
    """Upsert one label-catalogue or seller-inventory release (and its tracklist, if
    needed) into the cache.

    Args:
        conn: Open sqlite connection.
        client: An authenticated Discogs client.
        item: One item as yielded by `DiscogsClient.iter_label_releases` or
            `iter_seller_inventory` — unlike a collection/wantlist item, this has no
            `basic_information`: no styles/genres/labels at all, just flat
            id/title/artist/year/format fields. So, unlike `scan_release`, core release
            fields always come from a full `get_release_detail` fetch rather than the
            listing item itself.
        refresh: Re-fetch and replace the tracklist/release fields even if already cached.
        source_key: The label id or seller username (as text), paired with `source_type`.
        source_type: Either "label" or "seller" — both list bare releases the same way,
            just from different Discogs endpoints (`iter_label_releases`/
            `iter_seller_inventory`), so they share this same scan path.
        import_filter: An Other-source page's pre-import Style/Format/Year filter, if any.
            Format/year are already on the raw listing item, so those are checked first
            (cheaply, no API call) to skip an obviously-excluded release before ever
            fetching its detail; style isn't known until that fetch happens, so it's
            checked again afterward, once it's known, before the release is cached. The
            listing item's format tokens are recorded via `store.record_known_formats`
            regardless of the filter outcome — see `scan_release`'s `Args:` for why.

    Returns:
        True if the release was imported/tagged, False if `import_filter` excluded it —
        see `scan_release`'s `Returns:` for why callers care.

    The detail fetch (and its API cost) is skipped once a release is already cached and
    `refresh` isn't set — a re-scan of a label's catalogue for newly-added releases still
    needs to (re-)tag already-known ones with this source. In that case the filter is
    checked against the *cached* row's own styles/year (no extra API call needed either).
    """
    release_id = item["id"]
    format_text = item.get("format") or ""
    store.record_known_formats(conn, [t.strip() for t in format_text.split(",")])
    existing = store.get_release(conn, release_id)

    if existing is not None and not refresh:
        if import_filter is not None and not import_filter.matches(
            styles=json.loads(existing["styles"]) + json.loads(existing["genres"]),
            format_text=format_text,
            year=existing["year"],
        ):
            return False
        store.record_release_source(conn, release_id, source_type, source_key)
        conn.commit()
        return True

    if import_filter is not None and not import_filter.matches(
        styles=None, format_text=format_text, year=item.get("year")
    ):
        return False  # excluded by format/year alone — skip the detail fetch entirely

    detail = client.get_release_detail(release_id)
    if import_filter is not None and not import_filter.matches(
        styles=[*detail.styles, *detail.genres], format_text=format_text, year=detail.year
    ):
        return False  # format/year passed, but the release's actual style doesn't

    store.replace_tracks(
        conn,
        release_id,
        [(t.position, t.title, t.duration, _clean_artist_names(t.artists)) for t in detail.tracklist],
    )
    videos = [{"uri": v.uri, "title": v.title, "duration": v.duration} for v in detail.videos]
    artist = _clean_artist_names(detail.artists) or ""
    store.upsert_release(
        conn,
        release_id,
        artist,
        detail.title,
        detail.styles,
        detail.genres,
        year=detail.year,
        labels=detail.labels,
        videos=videos,
        source_type=source_type,
        source_key=source_key,
    )
    conn.commit()
    return True
