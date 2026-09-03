# Quality Profiles, Notifications, and Upgrade Proposals Design

**Date:** 2026-09-03  
**Status:** Ready for review

## Goal

Provide globally configured quality profiles, one for movies and one for TV,
that consistently evaluate newly discovered subscription releases. Matching
releases remain durable in-app events and can also notify through Telegram and
email. Once Skald has a recorded downloaded quality for a media item, a newly
discovered strictly better candidate creates one deduplicated in-app upgrade
proposal and external notification; it never deletes or automatically downloads
anything.

## Compatible assumptions

- This design extends the existing `MediaSubscription`, `SubscriptionRelease`,
  `MediaJob`, subscription scan worker, authenticated server-rendered UI, and
  existing global Quality settings introduced by the quality-profile work.
- The current application has no durable release-quality score. A score is
  recorded when a Skald `MediaJob` reaches its existing successful organized
  state. Previously organized jobs and media outside Skald are not inferred or
  retroactively scored; they receive no upgrade proposals until a future
  explicitly scoped backfill/import feature.
- Movie subscriptions identify one movie item. TV quality is evaluated per
  concrete episode or season/pack target only when the existing parser and
  subscription targeting can identify that target. An unscoped TV-series result
  may be a new-release event but is not compared as an upgrade.
- Existing current candidate rules, including media-type parsing and matching,
  remain authoritative. This feature only adds quality eligibility and a stable
  quality preference rank after those rules have accepted a candidate.
- Existing per-subscription `auto_download` behavior, if present, is unchanged.
  Upgrade proposals do not invoke it and do not create `MediaJob` records.

## Scope

- Two editable singleton global profiles: `movie` and `tv`.
- Hard requirements and ordered soft preferences for resolution, audio, HDR,
  and release size.
- Preservation of minimum-seeder and excluded-token filtering as hard
  requirements.
- Deterministic selection/ranking of candidates that pass hard requirements.
- Durable quality scores for successfully downloaded Skald media.
- Durable, deduplicated in-app events for newly matching releases and upgrade
  proposals.
- Best-effort Telegram and email delivery for new matching releases and upgrade
  proposals, with durable delivery-attempt/error audit data.

## Non-goals

- Multiple named, per-user, or per-subscription profiles.
- Automatic replacement, deletion, download, import, or modification of an
  existing torrent/library file in response to an upgrade.
- Scoring media not downloaded and organized by Skald, including filesystem
  discovery or media-library backfill.
- Exact comparison of ambiguous TV series-wide releases, cross-edition
  equivalence, codec bitrates, source quality, language preferences beyond the
  configured audio labels, or subjective release-group quality.
- Guaranteed external-message delivery, a provider queue/broker, digests, or
  notification retries beyond the next independently generated event.
- Changes to the existing scan interval, release discovery query, manual Grab,
  qBittorrent integration, or job organization lifecycle.

## Architecture

### Profile service

Create a `QualityProfileService` used by subscription scanning, automatic
candidate selection, and upgrade evaluation. It owns normalization, validation,
hard-filter evaluation, score calculation, and deterministic ordering. Routes
must not implement profile logic independently.

Each profile contains required/allowed values and ordered preferences. Parsed
release attributes are normalized to a finite vocabulary before comparison:

- resolution: `480p`, `720p`, `1080p`, `2160p` (`4K` normalizes to `2160p`);
- audio: parser-supported normalized labels such as `stereo`, `5.1`, `7.1`, and
  `atmos`;
- HDR: `sdr`, `hdr`, `hdr10`, `hdr10plus`, or `dolby_vision`; and
- size: a non-negative byte count from the indexer result.

The exact supported audio/HDR labels are the parser's established normalized
output. Values it cannot normalize are `unknown`; they can never satisfy a
configured hard requirement and rank below explicit preferred values. This
avoids guessing from arbitrary title text.

### Scan and event pipeline

The subscription worker continues to persist every newly discovered release by
its existing stable fingerprint before profile processing. For each new release
that passes both existing candidate rules and the applicable profile hard
requirements, it creates a `release_match` event. It then evaluates a possible
upgrade for the release's concrete media target. Event persistence completes in
the database transaction before any external notification is attempted.

The worker invokes a `NotificationDeliveryService` after commit. The service
uses independently configured Telegram and email adapters and records each
attempt. Provider work is outside the scan transaction and is isolated per
event and channel.

### Downloaded-quality recording

When the existing job worker transitions a `MediaJob` to its successful
organized terminal state, it obtains the release attributes already persisted
on the job (or parses the job's immutable `release_title` using the same
normalizer) and writes a `DownloadedQuality` record. This record establishes
the current quality baseline for that concrete movie or TV target. Failed,
queued, downloading, completed-but-unorganized, and manually removed jobs do
not establish a baseline.

## Profile model and filtering

### Global profiles

`QualityProfile` is keyed by `media_type` and has exactly one row for `movie`
and one for `tv`. The settings route lazily creates missing rows with compatible
defaults, so existing databases remain usable after migration.

Each row stores:

- `media_type` (unique `movie` or `tv`);
- hard allowed resolutions, audio formats, and HDR formats as normalized JSON
  lists; an empty list means no restriction for that dimension;
- hard minimum and maximum size in bytes, each nullable; and
- existing non-negative `minimum_seeders` and normalized non-empty
  `excluded_tokens` fields;
- ordered soft-preference lists for resolution, audio, and HDR; and
- ordered preferred size bands, each `{min_bytes, max_bytes}` with inclusive
  endpoints and no overlap; plus `updated_at`.

Defaults preserve the current profile behavior: resolutions `1080p` and
`2160p`, minimum five seeders, and case-insensitive exclusions `CAM`, `TS`, and
`TeleSync`. Audio, HDR, and size have no hard restriction and no soft
preference by default. The movie and TV rows initially use these same defaults,
but are independently editable.

### Hard requirements

A candidate is eligible only if all applicable hard requirements pass:

1. it has already passed current media-type/target matching rules;
2. its title contains no excluded token, compared case-insensitively using the
   existing token-boundary behavior;
3. its seeder count is at least `minimum_seeders`;
4. each non-empty allowed resolution, audio, or HDR list contains the parsed
   value; and
5. its known size is within configured minimum/maximum bounds.

A missing or unparseable value fails a configured hard requirement for that
dimension. A size requirement similarly rejects a candidate with an unknown
size. An unconstrained dimension does not reject unknown values. Hard failure
never creates a matching-release event, upgrade proposal, or external message;
the underlying `SubscriptionRelease` remains persisted for audit and display.

### Soft preference ranking

Eligible candidates are ranked deterministically. For every configured soft
dimension, the candidate's preference position is used (`0` is best and
unlisted/unknown sorts after all listed values). Size uses the first matching
preferred size-band position, otherwise sorts after configured bands. Candidates
are ordered by the tuple:

1. resolution preference position;
2. audio preference position;
3. HDR preference position;
4. size-band preference position;
5. existing current candidate ordering rules, unchanged; and
6. stable release fingerprint ascending.

Lower tuples are better. A profile with no soft preferences therefore preserves
the current rules and only adds the configured hard filtering. The shared
service supplies this ordering to every caller so notification, proposal, and
existing auto-selection paths cannot diverge.

## Data model and migrations

Add the following tables/columns through an additive migration. JSON fields use
the application's existing SQLite-compatible serialization. The migration must
be safe on populated databases and must not delete or reinterpret existing
release/job rows.

### `QualityProfile`

Create the two-row model described above. The migration creates both default
rows if absent; uniqueness on `media_type` prevents duplicates. Existing legacy
singleton profile data, where present, is copied into both rows for the fields
it supports (resolution, seeders, exclusions); new fields receive compatible
unrestricted defaults. The migration is idempotent for partially upgraded
installations.

### `SubscriptionRelease`

Add nullable normalized observed attributes: `resolution`, `audio`, `hdr`, and
`size_bytes` (the existing source size remains unchanged). They are populated
for future discoveries only; absent values in historical rows remain unknown.

### `DownloadedQuality`

Create one current-baseline row per concrete media target with:

- ID; `media_type`; canonical target key; and optional subscription ID for
  provenance;
- source `media_job_id` (unique);
- normalized resolution, audio, HDR, and observed size bytes;
- immutable computed quality tuple/score version and score value;
- `recorded_at` and `updated_at`.

The canonical target key is the existing stable movie identity or the existing
stable TV episode/season-pack identity. It is not a display title. A unique
constraint on `(media_type, target_key)` makes the row the current baseline.
If a later successfully organized job has a strictly better profile-independent
quality tuple, it replaces that row; equal or worse jobs retain the baseline.
This preserves the best known downloaded quality rather than merely the latest
download.

The profile-independent stored score is the lexicographic tuple of normalized
resolution, audio, and HDR ranks from the fixed vocabulary. It deliberately
does not depend on editable profile preferences: changing a profile cannot
rewrite history or manufacture an upgrade. Unknown attributes use the fixed
lowest rank. Observed size is retained for audit and profile filtering, but is
not an intrinsic upgrade dimension because a larger or smaller release is not
universally higher quality. The exact tuple and score version are persisted for
future migration.

### `SubscriptionEvent` and `NotificationDeliveryAttempt`

Create `SubscriptionEvent` as the durable in-app notification record:

- ID, subscription ID, optional subscription-release ID, media type, target
  key, event kind (`release_match` or `upgrade_proposal`), title/body snapshot,
  optional prior/current quality score snapshots, `created_at`, and `read_at`.

`release_match` is unique by `subscription_release_id` and kind. An
`upgrade_proposal` is unique by `(target_key, subscription_release_id, kind)`.
These constraints make repeated scans, concurrent workers, and provider retries
incapable of creating duplicate in-app events. A release that is better than a
baseline but already has an upgrade proposal remains one proposal, even when
multiple subscriptions discover the same release.

Create `NotificationDeliveryAttempt` with event ID, channel (`telegram` or
`email`), attempt time, outcome (`sent`, `failed`, or `skipped`), provider
message identifier when available, and a bounded sanitized error summary. It
is an append-only audit log. A unique `(event_id, channel)` constraint provides
one delivery attempt per configured channel for each event in this version.

## Flows

### Profile administration

1. An authenticated user opens `GET /quality` and sees separately editable
   Movie and TV profile forms.
2. `POST /quality/{media_type}` validates normalized hard values, ordered
   preference lists, non-negative seeders/sizes, valid non-overlapping size
   bands, and existing exclusion-token rules.
3. On success, the service atomically updates only that media-type row and
   redirects to the settings page. Invalid input redisplays the form with a
   field-level error and preserves no partial change.
4. Profile changes apply to future scans and candidate ranking only. They do
   not mutate historical events, scores, proposals, jobs, or downloaded files.

### New matching release

1. A due subscription scan searches and persists each unseen
   `SubscriptionRelease` using its existing fingerprint deduplication.
2. For each new release, the service normalizes observed quality and evaluates
   current matching rules plus the relevant profile's hard requirements.
3. A hard failure ends quality processing for that release.
4. A passing release creates the unique `release_match` event, visible in the
   existing in-app notifications/release history.
5. After commit, configured Telegram and email channels each receive a
   best-effort delivery attempt for that event.

### Upgrade proposal

1. After a new release has passed hard requirements, the service resolves its
   concrete target key.
2. If no `DownloadedQuality` baseline exists, or the result cannot be scoped to
   a concrete target, no proposal is created.
3. The service compares the candidate's fixed stored-quality tuple with the
   baseline. Equal or worse candidates create no proposal.
4. A strictly better candidate atomically creates the unique
   `upgrade_proposal` event with a summary of current versus proposed quality.
5. After commit, configured channels are notified. The proposal gives the user
   a link/action to the existing manual Search/Grab flow; it performs neither
   deletion nor download itself.

### Download completion baseline

1. The existing job worker successfully organizes a media job.
2. It derives normalized quality from the immutable release metadata and
   resolves the canonical target key.
3. In the same durable state transition boundary, it upserts
   `DownloadedQuality` only when the new fixed tuple is strictly better than
   the stored baseline.
4. It records no external notification merely because a baseline changed.

## Failure behavior

- Indexer, parser, or per-subscription scan failure retains existing isolation:
  record the concise subscription error/reschedule as today, log it, and
  continue other subscriptions. No partial event is created for an unpersisted
  release.
- A malformed title or unavailable attribute is a hard failure only if that
  attribute is constrained; otherwise it ranks below known preferred values.
- Database uniqueness conflicts during concurrent discovery/event creation are
  treated as successful deduplication, not scan failure.
- A profile validation failure changes neither profile row nor scan behavior.
- Telegram/email configuration absence records a `skipped` attempt or clear
  application log entry and never blocks event persistence.
- A provider timeout, rejection, or unexpected response records a sanitized
  `failed` attempt and structured error log, then processing continues with the
  next channel/event/subscription. Provider failure never rolls back a release,
  event, proposal, quality baseline, or scan schedule.
- Delivery errors expose no credentials, full provider payloads, recipient
  addresses, or tokens in the UI/audit record. Retention follows the existing
  application database retention policy.
- If normalized quality cannot be derived at organized-job time, the job remains
  successfully organized; Skald logs the condition and omits its baseline rather
  than failing or downgrading the job.

## Security and configuration

- All quality settings, notification-event views, and notification configuration
  routes use the existing authentication dependency and CSRF/form protections.
- Telegram bot token, chat identifier, SMTP credentials, sender, and recipient
  configuration are server-only settings read from environment/secrets. They
  are never rendered into HTML, URLs, event bodies, logs, or API errors.
- Supported optional settings are `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
  `SMTP_FROM`, and `SMTP_TO`. TLS mode follows the existing mail-client
  convention; absent or incomplete channel configuration disables that channel
  without disabling scans or in-app events.
- Provider clients enforce finite connect/read timeouts, validate configured
  endpoints/addresses using their library facilities, and treat notification
  text as plain text. User/indexer release titles are escaped by templates and
  never interpolated into shell commands.
- Profile fields are server-side validated against finite enumerations and
  bounded lengths/counts before storage. Excluded tokens and event error text
  are length-limited to avoid database/log amplification.

## Test strategy

- Model/migration tests cover creation of two default profiles, legacy-profile
  compatibility, uniqueness, valid/invalid size bands, historical null
  attributes, and additive upgrade on populated data.
- Pure profile-service tests cover normalization aliases, all hard-filter
  dimensions, preserved seeders/exclusions, unknown values, deterministic soft
  ranking, stable-fingerprint ties, and preservation of current ordering when
  preferences are empty.
- Downloaded-quality tests cover target-key construction, only-organized-job
  recording, strict better/equal/worse comparisons, stable score versions, and
  profile edits not changing an existing baseline.
- Worker/integration tests cover release persistence before filtering, one
  match event per eligible new release, no event for hard failures, scoped TV
  behavior, proposal creation only for a strict improvement, and all relevant
  database/concurrency deduplication constraints.
- Notification-adapter tests mock Telegram and SMTP success, missing
  configuration, timeout, rejection, and unexpected provider failure; assert
  attempts/errors are persisted and failures cannot prevent scan/event
  persistence or processing of another subscription/channel.
- Route tests cover authorization, independent Movie/TV profile updates,
  validation failures, event read state/display, and upgrade proposal links
  using manual actions only.

## Acceptance criteria

1. Skald maintains exactly one independently editable global profile for movies
   and one for TV, with compatible defaults of 1080p/4K, five seeders, and
   CAM/TS/TeleSync exclusions.
2. Resolution, audio, HDR, and size can each be hard requirements or ordered
   soft preferences; a hard failure excludes a candidate.
3. Minimum seeders and excluded tokens remain hard requirements.
4. Remaining eligible candidates are ranked deterministically by configured
   preferences and then existing current rules with a stable fingerprint tie
   break.
5. Every new eligible subscription release creates one durable in-app matching
   event, while non-eligible releases remain durable discovery records only.
6. A successfully organized Skald download stores a durable quality baseline
   for its concrete media target without altering existing jobs or files.
7. A new eligible candidate creates exactly one in-app upgrade proposal only
   when its fixed quality score is strictly better than the stored baseline.
8. An upgrade proposal never deletes media, changes a current job, or starts a
   download; it routes the user to the existing manual flow.
9. Telegram and email are attempted for both new matching-release events and
   upgrade proposals when configured, and all delivery attempts/errors are
   durably auditable and safely logged.
10. Provider failures, absent provider configuration, and duplicate/concurrent
    event attempts cannot prevent scan completion, release/event persistence,
    or processing of other work.
