# Quality Profile and Automatic Grab Design

**Date:** 2026-09-03  
**Status:** Ready for review

## Goal

Add one application-wide quality profile and an opt-in automatic-download switch
per subscription. For eligible movie subscriptions, Skald selects one newly
found release that matches the profile and creates the same safe qBittorrent
job as a manual Grab.

## Scope

- One editable global profile with allowed resolutions, minimum seeders, and
  excluded title tokens.
- Defaults: 1080p and 4K allowed, at least 5 seeders, exclude `CAM`, `TS`, and
  `TeleSync` case-insensitively.
- A disabled-by-default `auto_download` switch on every subscription.
- Automatic selection of exactly one best matching release per eligible scan.
- Reuse the existing validated job-creation path; no direct qBittorrent calls
  from the subscription scanner.
- A protected Quality settings page and visible subscription auto-download
  state.

Out of scope: multiple named profiles, audio/HDR/source/size filters,
replacement of an already grabbed release, and automatic downloads for TV
subscriptions before season/episode targeting is implemented.

## Persistence

Create a singleton `QualityProfile` record. Its only supported row has ID 1 and
stores:

- allowed resolutions as a normalized JSON list from `720p`, `1080p`, and
  `2160p`/`4K` aliases;
- non-negative `minimum_seeders`;
- normalized, non-empty excluded tokens as JSON; and
- `updated_at`.

The settings route creates the default record on first access. This makes old
databases safe to upgrade and provides deterministic behavior without an
environment-only configuration.

`MediaSubscription` receives `auto_download: bool = False` and nullable
`auto_grabbed_release_id`, which references the selected
`SubscriptionRelease`. A subscription with a value in this field has consumed
its one automatic movie download and cannot select a later competing release.
This is durable across restarts and prevents duplicate jobs.

## Candidate selection and job creation

On each due scan, existing discovery continues unchanged: all matching releases
are saved as in-app notifications. After persistence, an active subscription is
eligible for auto-grab only when all are true:

1. `auto_download` is enabled;
2. its type is `movie`;
3. it has no `auto_grabbed_release_id`; and
4. at least one newly discovered release passes the global profile.

A release passes when its title contains an allowed resolution, has at least
the configured number of seeders, and does not contain any excluded token. The
scanner selects the candidate with the greatest seed count; ties use the larger
resolution then the stable release fingerprint. It creates one `MediaJob` by
calling an extracted shared Grab service, which validates parsed movie metadata,
adds the torrent to qBittorrent, and persists the job exactly as the current
manual route does. The selected release and created job are recorded together
only after qBittorrent succeeds.

If no candidate matches, the scanner records notifications but takes no side
effect. If creation fails, it records a concise subscription error and leaves
`auto_grabbed_release_id` empty so a later due scan may retry. Duplicate or
concurrent requests must not create more than one job for the subscription.

## Routes and interface

- `GET /quality` renders the singleton profile.
- `POST /quality` validates and updates all settings, then redirects to
  `/quality`.
- The base navigation includes `Quality`.
- Each subscription row includes an auto-download enabled/disabled status and a
  POST toggle. TV rows explain that the switch becomes actionable after the
  season/episode feature; it remains stored but cannot trigger a broad series
  download.

The Quality page follows the existing server-rendered dark UI: resolution
checkboxes, an integer minimum-seeders input, comma-separated exclusions, an
inline validation error, and a single save action. It must work without
JavaScript.

## Error handling

- Reject empty allowed-resolution selections, negative seed counts, invalid
  resolution values, and blank/duplicate exclusion tokens.
- A malformed release title simply fails the profile match.
- qBittorrent or metadata validation failures are bounded to the subscription
  and cannot stop other scans or job polling.
- Automatic creation never bypasses existing authentication, metadata
  validation, category selection, or `MediaJob` persistence behavior.

## Test strategy

- Model/default tests cover profile initialization and the opt-in subscription
  fields.
- Pure matching tests cover resolution aliases, exclusion case handling,
  seeder threshold, ranking, and malformed titles.
- Service tests cover a single selected candidate, no-match behavior,
  disabled auto-download, TV safety block, retry after failure, and deduplication.
- Route tests cover Quality authorization, defaults, valid updates, invalid
  input, and the subscription toggle.
- Regression tests prove automatic and manual grabs use the same service and
  preserve the existing job validation guarantees.

## Acceptance criteria

1. The global defaults are 1080p/4K, 5 seeders, and CAM/TS/TeleSync excluded.
2. A user can update the profile in the application.
3. Automatic download is explicitly enabled per subscription.
4. A matching movie subscription creates one ordinary job for the highest-seed
   eligible release and never creates a duplicate job for it.
5. Non-matching releases remain notifications only.
6. TV subscriptions cannot auto-download until episode/season scope exists.
7. A qBittorrent failure is visible, isolated, and retryable.
