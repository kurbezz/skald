# Skald MVP — Design Spec

Date: 2026-09-01
Status: Approved

## Purpose

Skald is a minimal self-hosted replacement for Radarr/Sonarr, scoped to:
manual search for movie/TV releases, sending the chosen release to
qBittorrent, tracking download completion, and organizing finished files
into a Jellyfin-compatible library layout.

Explicitly out of scope for MVP: automatic/scheduled search, RSS
monitoring of new episodes, quality-profile-driven auto-grab, direct
tracker scraping (interface allows adding it later), TMDB/TVDB metadata
lookups (may be added later as an alternative metadata source).

## Stack

- Python 3.12+, FastAPI (API + server-rendered UI via Jinja2 + HTMX)
- `qbittorrent-api` for the qBittorrent Web API client
- `guessit` for parsing release filenames into candidate metadata
- `httpx`/Torznab XML parsing for the indexer client
- SQLite (via `sqlmodel`/`aiosqlite`) for job state
- Deployed as a single Docker container alongside qBittorrent

Rationale: this service is integration/glue code (HTTP APIs, filename
parsing, file moves), not CPU-bound, so Python's library ecosystem
(guessit, qbittorrent-api, mature Torznab/HTTP tooling) gives faster,
lower-risk delivery than Rust, whose equivalent libraries are thin or
nonexistent in this niche.

## Architecture

Single FastAPI process containing:

1. **API/UI layer** — search page, release-selection/confirmation form,
   job dashboard.
2. **Background worker** — an asyncio task loop polling qBittorrent for
   job status and triggering organization on completion.
3. **SQLite** — persisted job state, shared between API layer and worker.

No separate queue/broker (Celery/Redis) — unnecessary complexity for a
single qBittorrent instance and manual-search-only workflow.

## Components

- **IndexerClient** — abstract interface `search(query) -> list[ReleaseResult]`.
  First implementation: Torznab client against Jackett/Prowlarr. The
  abstraction allows adding a direct tracker-scraping implementation
  later without touching the rest of the system.
- **ReleaseParser** — wraps `guessit` to extract title/year/season/episode/
  quality from a release name. Used only to pre-fill the confirmation
  form; never the sole source of truth for organizing.
- **QbittorrentClient** — wraps `qbittorrent-api`:
  - `add_torrent(magnet_or_url, category, ...)`
  - `get_torrent_info(hash) -> status/progress/content_path`
  - Category tags `skald-movie` / `skald-tv` distinguish job types in
    qBittorrent itself.
- **Organizer** — pure functions computing target paths, plus a thin I/O
  layer to hardlink (falling back to copy on cross-device failures)
  finished video files into the library, leaving the original qBittorrent
  content untouched so seeding continues:
  - Movie: `{movies_root}/{Title} ({Year})/{Title} ({Year}).{ext}`
  - TV: `{tv_root}/{Series}/Season {SS}/{Series} - S{SS}E{EE}.{ext}`
  - Only whitelisted video extensions (mkv, mp4, avi, ...) are linked;
    samples/nfo/etc. are ignored.
- **Worker** — asyncio loop, polls every N seconds for jobs in
  `QUEUED`/`DOWNLOADING`/`COMPLETED` state, advances state machine, and
  invokes Organizer on completion.
- **API/UI** — FastAPI + Jinja2 + HTMX:
  - `GET /search?q=...&type=movie|tv` — render Torznab results with
    guessit-derived metadata hints.
  - `POST /grab` — body includes chosen release ref + confirmed/edited
    metadata (title, year, season, episode) → creates `MediaJob`, calls
    `QbittorrentClient.add_torrent`.
  - `GET /jobs` — dashboard of jobs with status/progress.
  - `GET /jobs/{id}` — detail view; allows editing metadata and
    retriggering organize when in `NEEDS_ATTENTION`.

## Data Model

`MediaJob`:
- `id`, `type` (movie/tv), `title`, `year`, `season?`, `episode?`
- `release_title`, `qbit_hash`, `category`
- `status`, `error_message?`, `content_path?`
- `created_at`, `updated_at`

### State machine

```
QUEUED → DOWNLOADING → COMPLETED → ORGANIZING → ORGANIZED
                                        │
                                        └─> NEEDS_ATTENTION (user fixes
                                            metadata, retries organize)
Any step → FAILED (with error_message; user can retry from UI)
```

## Data Flow

1. User submits a search query + content type. Backend calls
   `IndexerClient.search`, returns Torznab results; each result is run
   through `ReleaseParser` for a metadata hint (not persisted yet).
2. User picks a result; UI shows an editable form pre-filled with the
   parsed metadata. User confirms or corrects title/year/season/episode.
3. On submit: backend creates a `MediaJob` (status `QUEUED`) with the
   confirmed metadata and calls
   `QbittorrentClient.add_torrent(..., category=skald-movie|skald-tv)`.
4. Worker polls qBittorrent by hash for all active jobs; updates
   progress/status in SQLite.
5. When qBittorrent reports the torrent complete, job moves to
   `COMPLETED` → Organizer computes the target path and hardlinks (or
   copies) the video file(s) into the library → `ORGANIZED`.
6. Any failure (qBittorrent add error, missing/ambiguous files, path
   collision) sets `FAILED` or `NEEDS_ATTENTION` with an error message
   surfaced in the UI; user manually retries after fixing input.

## Configuration (env vars)

- `JACKETT_URL`, `JACKETT_API_KEY` (or Prowlarr equivalents)
- `QBIT_HOST`, `QBIT_USER`, `QBIT_PASS`
- `MOVIES_LIBRARY_PATH`, `TV_LIBRARY_PATH`
- `DB_PATH`
- qBittorrent category names for movie/tv (defaults `skald-movie`/`skald-tv`)

## Error Handling

- Indexer/qBittorrent HTTP errors surfaced as user-visible messages, job
  stays retryable.
- Organize failures (ambiguous parse, path already exists, unsupported
  file layout) land in `NEEDS_ATTENTION`, never silently overwrite
  existing library files.
- Worker loop failures for one job must not crash the loop or block
  other jobs' polling.

## Testing Strategy

- `ReleaseParser` and `Organizer` path-building: pure-function unit
  tests, table-driven over representative release-name/path edge cases.
- `IndexerClient`/`QbittorrentClient`: tests against mocked HTTP
  (`respx`) using Torznab XML and qBittorrent JSON fixtures.
- Worker state machine: tested against a fake qBittorrent client
  returning canned status sequences, asserting correct state
  transitions including failure paths.

## Deployment

Single Docker container running alongside qBittorrent, sharing a volume
for the download directory and Jellyfin library paths. SQLite database
file stored on a persistent volume.
