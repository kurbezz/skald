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
