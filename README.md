# Skald

Minimal Radarr/Sonarr replacement: manual search via Jackett/Prowlarr,
send to qBittorrent, wait for download, organize into a Jellyfin library.

## Local development

    uv sync --all-groups
    cp .env.example .env   # edit with your Jackett/qBittorrent details
    uv run uvicorn skald.main:app --reload

Open http://127.0.0.1:8000/search

## Tests

    uv run pytest -v

## Docker

    docker build -t skald .
    docker run -d \
      --env-file .env \
      -p 8000:8000 \
      -v /path/to/downloads:/downloads \
      -v /path/to/library:/library \
      skald

Set `MOVIES_LIBRARY_PATH=/library/movies` and `TV_LIBRARY_PATH=/library/tv`
in `.env` to match the mounted volume, and point `QBIT_HOST` at your
qBittorrent instance's Web UI address (reachable from the container).

## Authentication

Set both `AUTH_USERNAME` and `AUTH_PASSWORD` to enable HTTP Basic Auth for
the whole app except `/static`. Leave them empty (the default) to disable
authentication entirely.

## Docker Compose (full local stack for testing)

`docker-compose.yml` runs skald alongside qBittorrent and Jackett so you
can test the whole flow end-to-end without any existing infrastructure.

    docker compose up -d --build

First-run setup:

1. qBittorrent WebUI: http://localhost:8080 — the linuxserver image
   generates a random temporary admin password on first boot; find it with
   `docker compose logs qbittorrent | grep -i password`. Log in and either
   change the password to match `QBIT_PASS` below, or set `QBIT_PASS` to
   the generated one.
2. In qBittorrent, create two download categories: `skald-movie` and
   `skald-tv`, both saving under `/downloads` (the container path, shared
   with skald).
3. Jackett WebUI: http://localhost:9117 — add at least one indexer, then
   copy the API key shown at the top of the page.
4. Create a `.env` file in the repo root (docker compose reads it
   automatically) with:

       JACKETT_API_KEY=<key from step 3>
       QBIT_USER=admin
       QBIT_PASS=<password from step 1>

5. Restart skald to pick up the new `.env` values:

       docker compose up -d skald

6. Open http://localhost:8000/search and try a search.

Downloaded/organized files land under `./data/downloads` and
`./data/library` on the host. Since `/downloads` and `/library` are
separate mounts, skald falls back to copying files instead of
hardlinking them (see `src/skald/organizer.py`) — expected for this
local test setup, not a bug.
