# Subscription Search Design

**Date:** 2026-09-03  
**Status:** Ready for review

## Goal

Let a user subscribe to a movie or TV series from a TMDB catalog search. The
application checks active subscriptions through the existing Torznab indexer
every six hours and stores newly discovered releases as in-app notifications.
The feature never adds a torrent to qBittorrent automatically.

## Scope

- Add a TMDB-backed catalog search and a persistent subscription list.
- Support movies and TV series.
- Run searches automatically every six hours per active subscription.
- Persist and display found releases in the application, including releases
  found during the subscription's first scan.
- Allow a subscription to be disabled or deleted.
- Keep manual search and manual Grab as the only way to start a download.

Out of scope: Telegram/email notifications, quality profiles, automatic grabs,
per-subscription schedules, and exact release-to-TMDB matching beyond the
existing release parser and the subscription search query.

## TMDB integration

### Configuration

`Settings` gains a server-only `tmdb_read_access_token` setting. It is read
from the environment and must not be exposed in templates or URLs. TMDB calls
use `https://api.themoviedb.org/3` with:

```
Authorization: Bearer <TMDB_READ_ACCESS_TOKEN>
```

If the token is absent, the subscription page remains available but disables
catalog search and explains how to configure it. Existing search, jobs, and
worker behavior remains unaffected.

### Client

A small HTTPX-based TMDB client is created in the app factory and exposed on
`app.state`, next to the indexer and qBittorrent clients. It calls
`GET /search/multi` with the entered query, `language=en-US`, and
`include_adult=false`; person results are discarded. Only the first results
page is needed for this feature.

Each normalized catalog result contains:

- TMDB ID and media type (`movie` or `tv`)
- display and original title
- year from `release_date` or `first_air_date`, when available
- optional poster URL

Poster URLs are built from cached TMDB image configuration and the `w342`
poster size. A missing image path produces no image rather than a broken URL.
The page includes TMDB's required attribution: “This product uses the TMDB API
but is not endorsed or certified by TMDB.”

Transport timeouts, 429 responses, and non-success HTTP responses are turned
into clear user-facing search errors; they never expose the token. The client
uses bounded retry/backoff only for transient failures and rate limits.

## Persistence

### Subscription

Create a `MediaSubscription` SQLModel table with:

- ID, TMDB ID, `MediaType`, title, original title, year, and optional poster
  URL
- `is_active`
- `created_at`, `last_checked_at`, `next_check_at`, and `last_error`

The `(tmdb_id, media_type)` pair is unique. Creating an existing subscription
is idempotent and returns the existing item. `next_check_at` is durable so a
restart does not cause repeated immediate scans.

### Found release notification

Create a `SubscriptionRelease` SQLModel table. It is both the durable record
of a discovered release and the in-app notification. It contains:

- subscription ID and discovery time
- release title, indexer, size, seeders, leechers, download URL, and optional
  publication date
- a stable fingerprint and optional `read_at`

The fingerprint is a SHA-256 digest of the subscription ID and the normalized
release identity (`indexer`, download URL, title, and size). A unique
constraint on it makes discovery idempotent. This permits the first scan to
save all current results while later scans add only previously unseen releases.

Deleting a subscription deletes its associated release records. Disabling one
preserves its history but prevents further checks. Re-enabling makes it due for
its next normal scan.

## Automatic scan flow

The existing worker continues its short job-poll loop. Subscription scanning is
an independent concern invoked from that loop only for active subscriptions
whose durable `next_check_at` is due; it does not change the job worker's
10-second poll interval.

For each due subscription:

1. Build a conservative Torznab query from its original title (falling back to
   display title) and year when available.
2. Call the existing `IndexerClient.search(query)`.
3. Parse each returned release with the existing parser and retain only its
   matching media type.
4. Insert every new release using the unique fingerprint. Existing entries are
   ignored without error.
5. Record `last_checked_at`, clear `last_error`, and set `next_check_at` to
   six hours after the completed scan.

The match is deliberately query-based for the first version: indexer releases
are not externally resolved against TMDB. Releases returned for the
subscription query are filtered by parser media type, but no stricter
title-validation or quality profile is introduced.

If the indexer or parser fails for one subscription, the worker records a
concise `last_error`, moves that subscription's next check six hours forward,
logs the error, and continues checking other due subscriptions. No scan calls
qBittorrent, creates a `MediaJob`, or changes existing download lifecycle
state.

## Routes and interface

All routes use the existing authentication dependency.

- `GET /subscriptions` renders the page, accepts an optional TMDB catalog
  search query, lists subscriptions, and shows recent found releases.
- `POST /subscriptions` creates a subscription from normalized, server-checked
  TMDB result data. Client-submitted display fields are not trusted as the
  source of identity.
- `POST /subscriptions/{id}/toggle` enables or disables it.
- `POST /subscriptions/{id}/delete` deletes it and its history.
- `POST /subscriptions/{id}/releases/read` marks the subscription's unread
  release records as read.

The base navigation adds a `Subscriptions` destination. The page follows the
current server-rendered, dark design-system patterns:
catalog search results have an explicit Subscribe action; subscriptions show
type, year, enabled state, last-check status, and unread count; release rows
show title, indexer, size, peers, discovery time, and an action that opens the
existing manual Search flow with the release title. It does not add new
JavaScript behavior except where a small progressive enhancement is useful;
forms remain functional without it.

## Error handling

- Missing TMDB configuration disables only TMDB catalog lookup and subscription
  creation, with an actionable message.
- Invalid or failed TMDB responses display an error on the subscriptions page.
- Duplicate subscribe requests are harmless.
- A missing subscription returns the existing application-style 404 response.
- Individual indexer failures are isolated to the affected subscription and
  visible as its latest check error.

## Test strategy

- Settings tests cover absent and environment-loaded TMDB credentials.
- TMDB client tests mock authorization, multi-search normalization, empty dates
  and posters, transient failures, 429, and non-success responses.
- Model tests cover TMDB/media-type uniqueness, fingerprint deduplication,
  read state, and subscription deletion behavior.
- Route tests cover authentication, catalog rendering, creation, duplicate
  creation, toggle/delete/read actions, missing configuration, and TMDB errors.
- Worker tests use injected/controlled time to verify due selection, six-hour
  rescheduling, initial result creation, repeat-scan deduplication, media-type
  filtering, and per-subscription failure isolation.

## Acceptance criteria

1. An authenticated user can find movies and TV series via TMDB and subscribe
   to a result without creating duplicates.
2. Each active subscription is checked at most once per six-hour interval and
   its schedule survives restart.
3. The first scan records currently available matching releases; later scans
   record only new releases.
4. Found releases are visible in the application and can be marked read.
5. No automatic scan starts a download or creates a media job.
6. TMDB and indexer failures are contained, visible, and do not stop other
   application work.
