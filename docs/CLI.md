# CLI reference

All commands are run as `uv run discogs2ytmusic <command>` (or plain
`discogs2ytmusic <command>` if the virtualenv is activated / the package is
installed). Run any command with `--help` for the same information from the
terminal.

## `auth discogs`

Save and verify a Discogs personal access token.

```bash
discogs2ytmusic auth discogs --token YOUR_TOKEN
```

| Option | Default | Description |
|---|---|---|
| `--token` | *prompted if omitted* | Discogs personal access token. Hidden input if typed interactively. |
| `--username` | auto-detected | Discogs username. Only needed if token→identity lookup doesn't resolve it. |

Verifies the token against `/oauth/identity` before saving; exits with an
error and nothing is written if the token is invalid.

## `auth ytmusic`

Link a YouTube Music account by pulling two values out of a browser request
(see the [README setup section](../README.md#2-connect-youtube-music) for
where to find them).

```bash
discogs2ytmusic auth ytmusic
discogs2ytmusic auth ytmusic --from-file /path/to/headers.txt
```

| Option | Default | Description |
|---|---|---|
| `--from-file` | none (interactive prompt) | Read `cookie` and `x-goog-authuser` from a text file instead of prompting. File may contain just those two `key: value` lines, or a full raw DevTools header dump (extra lines are ignored). |

## `scan`

Fetch your Discogs collection (releases + tracklists) into the local cache
and print a track-count breakdown by style tag.

```bash
discogs2ytmusic scan
discogs2ytmusic scan --refresh
```

| Option | Default | Description |
|---|---|---|
| `--refresh` | off | Re-fetch every release's tracklist from Discogs even if it's already cached. Release metadata (artist/title/styles/genres) is always re-fetched regardless. |

Safe to re-run without `--refresh` — already-cached tracklists are reused, so
re-running after adding a few new records to your collection only fetches
the new ones.

## `sync`

Match cached tracks to YouTube videos and, with `--apply`, create/update one
YT Music playlist per style tag.

```bash
discogs2ytmusic sync                              # dry run, all styles
discogs2ytmusic sync --style "Deep House"          # dry run, one style
discogs2ytmusic sync --apply                       # actually sync
```

| Option | Default | Description |
|---|---|---|
| `--dry-run` / `--apply` | `--dry-run` | Preview matches without writing to YT Music, or actually create/update playlists. |
| `--style` | none (all styles) | Limit to one or more style tags. Repeatable: `--style "Deep House" --style "Dub Techno"`. |
| `--refresh` | off | Run `scan --refresh` first, then sync. Equivalent to running `scan --refresh` followed by `sync`. |

Behavior notes:

- Playlists are named `Discogs - <style>`. Re-running `--apply` reuses the
  existing playlist for a style (looked up by name on first creation, then
  cached by ID) rather than creating duplicates.
- Track searches are cached by `(artist, title)`, so re-syncing after adding
  new releases only searches YouTube for the new tracks — previously matched
  (or previously unmatched) tracks aren't re-searched.
- A release with no tracklist cached falls back to using the release title
  itself as a single "track" to search for.
- Requires YT Music auth (`auth ytmusic`) only when using `--apply`; dry runs
  work with an unauthenticated `YTMusic()` client since they only search.
