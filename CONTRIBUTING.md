# Contributing

## Setup

```bash
uv sync
uv run pre-commit install   # optional but recommended: lint/format/type-check on every commit
```

## Before opening a PR

```bash
uv run ruff check .
uv run ruff format .
uv run mypy src
uv run pytest
```

All four also run in CI (`.github/workflows/ci.yml`) on every push/PR.

## Branching

Branch off `main`, one topic per branch/PR (`feature/...`, `fix/...`,
`chore/...`). Tests run entirely offline (see `README.md#testing`) — no
Discogs token or YT Music login needed to develop or run the suite.

See [`CLAUDE.md`](CLAUDE.md) for an architecture overview and coding
conventions.
