from __future__ import annotations

import json
from pathlib import Path

# Only present in a full repo checkout (it's test fixture data, not packaged) —
# `--library dummy` is a dev/testing convenience, not something a pip-installed
# copy of this tool can rely on.
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "dummy_library.json"


class DummyLibraryUnavailable(RuntimeError):
    """Raised when `--library dummy` is used outside a full repo checkout."""


def load_releases() -> list[dict]:
    """Load the bundled test fixture in the same shape `DiscogsClient` returns.

    Raises:
        DummyLibraryUnavailable: if the fixture file isn't present (e.g. a pip-installed copy).
    """
    if not FIXTURE_PATH.exists():
        raise DummyLibraryUnavailable(
            f"Dummy library fixture not found at {FIXTURE_PATH}. "
            "--library dummy only works from a full repo checkout (it reads tests/fixtures/dummy_library.json)."
        )
    return json.loads(FIXTURE_PATH.read_text())["releases"]
