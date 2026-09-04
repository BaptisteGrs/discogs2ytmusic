from __future__ import annotations

import json
from pathlib import Path

# Only present in a full repo checkout (it's test fixture data, not packaged) —
# `--library dummy` is a dev/testing convenience, not something a pip-installed
# copy of this tool can rely on.
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "dummy_library.json"


class DummyLibraryUnavailable(RuntimeError):
    pass


def load_releases() -> list[dict]:
    if not FIXTURE_PATH.exists():
        raise DummyLibraryUnavailable(
            f"Dummy library fixture not found at {FIXTURE_PATH}. "
            "--library dummy only works from a full repo checkout (it reads tests/fixtures/dummy_library.json)."
        )
    return json.loads(FIXTURE_PATH.read_text())["releases"]
