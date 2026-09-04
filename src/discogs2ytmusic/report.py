from __future__ import annotations

from pathlib import Path


def write_missing_tracks(missing: dict[str, list[tuple[str, str]]], path: Path) -> None:
    """Write (or clear) a Markdown report of tracks with no confident match on YT Music/YouTube.

    `missing` maps style -> [(artist, title), ...]. If nothing is missing, any
    stale report from a previous run is removed instead of left around.
    """
    total = sum(len(tracks) for tracks in missing.values())
    if total == 0:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Missing tracks",
        "",
        f"{total} track(s) had no confident match on YT Music or YouTube during the last `sync`.",
        "Re-run `sync` after a match improves (e.g. a retitled upload) to clear an entry.",
        "",
    ]
    for style in sorted(missing):
        tracks = missing[style]
        if not tracks:
            continue
        lines.append(f"## {style} ({len(tracks)})")
        lines.append("")
        for artist, title in sorted(tracks):
            lines.append(f"- {artist} — {title}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n")
