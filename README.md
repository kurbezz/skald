# Skald

Minimal Radarr/Sonarr replacement: manual search via Jackett/Prowlarr,
send to qBittorrent, wait for download, organize into a Jellyfin library.

## Local development

    uv sync --all-groups
    cp .env.example .env   # edit with your Jackett/qBittorrent details
    uv run uvicorn skald.main:app --reload

Open http://127.0.0.1:8000/search

## Subscriptions

To search the TMDB catalog, create a TMDB API Read Access Token and add it to
your `.env`:

    TMDB_READ_ACCESS_TOKEN=<your TMDB API Read Access Token>

Subscriptions check for releases every 6 hours by default. Set
`SUBSCRIPTION_CHECK_INTERVAL_SECONDS` to use a different interval. By default
they record matching releases and events. The existing per-subscription
auto-download option remains available for matching movies and scoped TV
subscriptions; it is separate from quality upgrade proposals.

### Quality profiles, events, and notifications

Skald has two global, independently editable quality profiles: one for movies
and one for TV. Both default to allowing `1080p` and `2160p`, requiring at
least five seeders, and excluding `CAM`, `TS`, and `TeleSync`. Parsed
resolution, audio, and HDR values that are absent, ambiguous, malformed, or
unsupported are recorded as `unknown`. An `unknown` value is allowed when its
dimension is unrestricted, but does not pass a configured hard restriction for
that dimension.

Eligible new releases create events. A strictly better release than the
quality baseline recorded after a successful, source-backed organization also
creates an upgrade proposal. Proposals are review-only: they never
automatically replace or delete media, and never automatically download the
upgrade. Use the normal Search/Grab flow if you choose to acquire one. Existing
auto-download behavior is otherwise unchanged.

Baselines are recorded only for successfully organized, source-backed
subscription downloads. Historical, manual, and unscoped media are not
backfilled or inferred into baselines.

Telegram and SMTP email are optional server-side provider settings. Configure
only the channel(s) you want; a channel with incomplete configuration is
skipped. Delivery is best effort: each event/channel is attempted once, with
no retry and no guaranteed delivery.

    TELEGRAM_BOT_TOKEN=<bot token>
    TELEGRAM_CHAT_ID=<chat ID>
    SMTP_HOST=<SMTP host>
    SMTP_PORT=587
    SMTP_USERNAME=<SMTP username>
    SMTP_PASSWORD=<SMTP password>
    SMTP_FROM=<sender@example.com>
    SMTP_TO=<recipient@example.com>

The required TMDB attribution is displayed in the subscriptions page: “This
product uses the TMDB API but is not endorsed or certified by TMDB.”

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

   Optional Telegram/SMTP provider settings use the same eight variables
   listed above. Keep credentials in the top-level `.env`; Compose passes them
   through to the Skald service without storing secrets in the compose file.

5. Restart skald to pick up the new `.env` values:

       docker compose up -d skald

6. Open http://localhost:8000/search and try a search.

Downloaded/organized files land under `./data/downloads` and
`./data/library` on the host. Since `/downloads` and `/library` are
separate mounts, skald falls back to copying files instead of
hardlinking them (see `src/skald/organizer.py`) — expected for this
local test setup, not a bug.
