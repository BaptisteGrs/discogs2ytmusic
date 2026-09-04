from __future__ import annotations

from discogs2ytmusic import report


def test_write_missing_tracks_groups_by_style_and_sorts_within_style(tmp_path):
    path = tmp_path / "missing_tracks.md"
    missing = {
        "House": [("Robert Hood", "Minus"), ("Anthony Rother", "Party People")],
        "Techno": [("Jeff Mills", "The Bells")],
    }

    report.write_missing_tracks(missing, path)

    content = path.read_text()
    assert "3 track(s)" in content
    assert "## House (2)" in content
    assert "## Techno (1)" in content
    # sorted alphabetically within a style
    assert content.index("Anthony Rother") < content.index("Robert Hood")


def test_write_missing_tracks_skips_styles_with_nothing_missing(tmp_path):
    path = tmp_path / "missing_tracks.md"
    missing = {"House": [("Robert Hood", "Minus")], "Techno": []}

    report.write_missing_tracks(missing, path)

    content = path.read_text()
    assert "Techno" not in content


def test_write_missing_tracks_removes_file_when_nothing_missing(tmp_path):
    path = tmp_path / "missing_tracks.md"
    path.write_text("stale report from a previous run")

    report.write_missing_tracks({"House": []}, path)

    assert not path.exists()


def test_write_missing_tracks_noop_when_nothing_missing_and_no_prior_file(tmp_path):
    path = tmp_path / "nested" / "missing_tracks.md"

    report.write_missing_tracks({}, path)

    assert not path.exists()
