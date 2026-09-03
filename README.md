# discogs2ytmusic

Sync your Discogs collection to YouTube Music playlists.
Runs entirely locally as a CLI.

## Setup

```bash
uv sync
```

### 1. Connect Discogs

Get a personal access token from https://www.discogs.com/settings/developers
(no app registration needed, just click "Generate new token").

```bash
uv run discogs2ytmusic auth discogs --token YOUR_TOKEN
```

### 2. Connect YouTube Music

No Google Cloud project needed — this uses browser-copied request headers:

1. Open https://music.youtube.com in your browser and make sure you're logged in.
2. Open DevTools → Network tab.
3. Click any request to `music.youtube.com` (e.g. search for anything, then
   click the `search` or `browse` request in the Network panel).
4. Right-click it → Copy → Copy request headers.
5. Run the command below and paste when prompted:

```bash
uv run discogs2ytmusic auth ytmusic
```

## Usage

### Scan your collection

Fetches your full Discogs collection + tracklists into a local cache and
shows a breakdown of how many tracks fall under each style tag. Safe to
re-run — cached releases/tracklists aren't re-fetched unless you pass
`--refresh`.

```bash
uv run discogs2ytmusic scan
```

### Preview the sync (no changes made)

Matches every track to a YouTube video ID and shows how many matched per
style. Nothing is written to YT Music yet.

```bash
uv run discogs2ytmusic sync
```

Limit to specific styles:

```bash
uv run discogs2ytmusic sync --style "Deep House" --style "Dub Techno"
```

### Actually create/update the playlists

```bash
uv run discogs2ytmusic sync --apply
```

Playlists are named `Discogs - <style>` and are safe to re-run: existing
playlists are reused (not duplicated), and matched tracks are cached so
re-syncing only searches for new tracks.

## Notes

- All state (credentials, collection cache, search-match cache) lives outside
  the project in your OS's standard config/cache directories, not in this repo.
- Track matching quality varies for very obscure records — check the `sync`
  preview table before running `--apply`, and consider narrowing with
  `--style` to review results in smaller batches first.
