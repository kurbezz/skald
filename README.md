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

Set both `AUTH_USERNAME` and `AUTH_PASSWORD` to require login for the whole
app except `/static`. Leave them empty (the default) to disable
authentication entirely — in that case `/login` just redirects to `/jobs`.

When enabled, unauthenticated requests are redirected to a `/login` page.
A successful login sets a signed, `httponly` session cookie (`session`)
valid for 30 days; `/logout` clears it.

The cookie is signed with `SECRET_KEY`. If you don't set it, a random key
is generated every time the app starts, which means **every restart logs
everyone out**. For a longer-lived session across restarts/deploys, set
`SECRET_KEY` to a fixed random value (e.g. `python -c "import secrets;
print(secrets.token_hex(32))"`) in your `.env`.

The session cookie is not marked `secure`, since this app is commonly
self-hosted over plain HTTP on a LAN. If you put it behind an HTTPS
reverse proxy, consider adding `secure=True` in `src/skald/routes/auth.py`.

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
