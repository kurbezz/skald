# TV-pack lifecycle fencing

Date: 2026-09-02  
Status: Approved — supersedes the existing TV-pack recovery implementation

## Purpose

This design replaces path-only pack recovery with a fenced, identity-aware
lifecycle. A path reservation alone does not prove that a file at that path was
created by a Skald attempt. Every pack mutation must therefore be associated
with an operation token and a per-file lifecycle state; publication records the
filesystem identity that recovery and deletion subsequently require.

The current `ORGANIZING`/`DELETING` recovery implementation, including its
path-existence-based cleanup rules, is superseded by this protocol. It must not
be extended as the compatibility model for new pack operations.

## Scope and modes

`organization_mode` is authoritative:

- `scalar` is used by movies and one-video TV jobs. It retains the existing
  metadata-derived target and `MediaJob.library_path` behavior.
- `pack` is used only by TV jobs with more than one discovered video file. It
  uses the ledger and the protocol below.

Scalar jobs never create pack staging paths or pack ledger lifecycle records.
Pack deletion never falls back to `library_path`; it uses its ledger only. This
keeps scalar compatibility from weakening pack ownership checks.

## Schema

All enum-like values below are stored as checked application values in SQLite;
migrations must not depend on SQLite enum support.

### `mediajob` additions

| Column | Type | Meaning |
| --- | --- | --- |
| `organization_mode` | `VARCHAR NOT NULL DEFAULT 'scalar'` | `scalar` or `pack`. |
| `operation_token` | `VARCHAR NULL` | Opaque UUID fencing token for the current pack organize or delete operation. It is replaced, never reused, for every new operation. |

`status` continues to include `ORGANIZING`, `ORGANIZED`,
`NEEDS_ATTENTION`, and `DELETING`. A non-null `operation_token` is required
for new `pack` rows in `ORGANIZING` or `DELETING`.

### `organizedfile` additions

The existing `path` is the intended public library target. New pack rows add:

| Column | Type | Meaning |
| --- | --- | --- |
| `operation_token` | `VARCHAR NOT NULL` for new pack rows | Token that owns this row's attempt. |
| `lifecycle` | `VARCHAR NOT NULL` | `reserved`, `staged`, `published`, `delete_requested`, or `legacy_unverified`. |
| `staging_path` | `VARCHAR NULL` | Private same-filesystem staging pathname for `reserved`/`staged` work. |
| `staging_device` | `INTEGER NULL` | `st_dev` captured after staging succeeds. |
| `staging_inode` | `INTEGER NULL` | `st_ino` captured after staging succeeds. |
| `published_device` | `INTEGER NULL` | `st_dev` captured after public publication succeeds. |
| `published_inode` | `INTEGER NULL` | `st_ino` captured after public publication succeeds. |

`path` remains globally unique while its ledger row exists, reserving that
public target across pack jobs. The migration also preserves the existing job
lookup index. No
operation may infer ownership from a matching pathname: an owned staged or
published file must have a matching recorded `(device, inode)` identity.

## Per-job advisory lock

Organization, recovery, delete request, and delete recovery acquire the same
process-local advisory lock keyed by `MediaJob.id`. The lock is held across the
database transition and its corresponding filesystem phase, and released in a
`finally` block.

The lock prevents two local workers from interleaving work for one job. It is
not a correctness boundary: it does not survive a crash and is not shared with
another process. The operation token and conditional database writes are the
cross-process/crash fencing boundary. A caller that cannot acquire the lock
does no work and retries on the next poll.

## Pack organization protocol

1. **Preflight without writes.** Discover videos and build every episode
   source-to-public-target mapping. Reject ambiguous markers, duplicate targets,
   public target conflicts, and reservations owned by another job before any
   database or filesystem attempt state is created.
2. **Reserve.** Under the job lock, generate a new UUID token. In one database
   transaction set `organization_mode='pack'`, `operation_token=<token>`,
   `status=ORGANIZING`, clear scalar metadata/error state, and insert one
   `organizedfile` row per target with that token, `lifecycle='reserved'`, and a
   unique private `staging_path`. Commit before the first filesystem call.
3. **Stage.** Create each staging file from its source using the hardlink-first,
   EXDEV-copy fallback. Staging is private and on the public target's filesystem.
   After a staging file is fully written and fsynced, capture its `(st_dev,
   st_ino)` and conditionally update that row from `reserved` to `staged` only
   where `job_id`, row token, and job token still match.
4. **Publish without replacement.** Publish a staged file only with an
   exclusive, non-overwriting operation (for example, a hardlink from staging
   to the public target, followed by removal of staging). Never use a replacing
   rename for public publication. Capture the public `(st_dev, st_ino)` and
   conditionally update the row to `published` with the same token fence.
5. **Finalize.** Set `library_path` only as the compatibility display pointer
   to the first published pack target. Finalize with one conditional update:
   `WHERE id=:job_id AND status='organizing' AND operation_token=:token`, and
   only if every ledger row for that job/token is `published`. A zero-row update
   is a fencing loss, not success; the caller must stop and let recovery own the
   state.

Every conditional row update includes the file row's operation token. A stale
organizer can neither publish a row for a newer operation nor finalize a job
whose token has been replaced by deletion.

## Delete request and deletion protocol

A delete request first acquires the same job lock. In one committed transition
it sets `status=DELETING`, replaces `operation_token` with a new delete token,
and marks each current pack row `delete_requested` with that token. This commit
happens before any library or qBittorrent side effect.

For a pack row, deletion is allowed only when the current public file exists
and its `(st_dev, st_ino)` equals `published_device/published_inode`. A missing
public path is already clean. A present path with a missing or mismatched
identity is foreign: it is never removed, its row is retained, and the job is
left in `NEEDS_ATTENTION` with an actionable ownership-conflict message.

After every owned public and staging file is confirmed absent, qBittorrent
content deletion is attempted. An already-absent torrent is success. On a
qBittorrent failure, `DELETING`, the delete token, and all ledger rows remain
for retry. Only after both filesystem and torrent layers succeed are the ledger
rows and job deleted in one transaction.

For a scalar job, the same durable `DELETING` intent is committed first, then
the scalar `library_path` is removed, then qBittorrent is deleted, then the job
is removed. Scalar deletion does not use pack identity fields.

## Recovery and ownership rules

Recovery runs only under the per-job advisory lock:

- An `ORGANIZING` pack job is recoverable only by its current operation token.
  It may resume a `staged` row only after its staging identity matches. It may
  clean an abandoned staging/public file only after the corresponding recorded
  identity matches. Missing owned paths advance cleanup; mismatched paths are
  foreign and remain untouched.
- A `DELETING` pack job is recoverable only by its delete token. It cleans only
  matching owned identities, then retries qBittorrent deletion. A stale
  organization token can never publish or finalize once delete has replaced
  the job token.
- A token mismatch, identity mismatch, unknown lifecycle, or failed conditional
  update stops automatic mutation. The job becomes `NEEDS_ATTENTION` with the
  token, row, and path in its error message; the ledger is retained for audit
  and operator action.
- Recovery never deletes a file solely because it is at a ledger or staging
  pathname. It never deletes download sources.

Normal torrent polling excludes `ORGANIZING` and `DELETING`. Errors during
recovery roll back and preserve the durable state; generic polling must not
rewrite either state to `FAILED`.

## Migration policy

Migration is additive and transactional where SQLite permits it:

1. Add the columns and indexes above, then validate existing path reservations
   before creating/retaining uniqueness. Never delete duplicate legacy rows to
   make a unique index succeed; abort migration with an actionable duplicate
   reservation error instead.
2. Backfill existing jobs to `organization_mode='scalar'`, except jobs with
   existing `organizedfile` rows, which become `pack`.
3. Existing pack rows have no trustworthy creation identity. Mark them
   `legacy_unverified`, leave identity/token fields null, and do not auto-delete
   their public paths during recovery or deletion. They require explicit
   operator reconciliation or a separately approved identity-adoption flow.
4. New operations never write `legacy_unverified`; they always reserve a fresh
   token and complete the full staging/publication protocol.

This policy favors retaining an uncertain legacy file over deleting a file that
may belong to another job or user.

## Test phases

1. **Schema/migration:** additive upgrade, conservative duplicate detection,
   legacy-unverified backfill, and no destructive duplicate cleanup.
2. **Reservation/fencing:** preflight-before-reserve, reservation commit before
   filesystem work, per-job lock contention, stale-token conditional updates,
   and stale finalization rejection.
3. **Staging/publication:** EXDEV copy, partial staging failure, non-overwrite
   publication race, identity capture, and no public partial visibility.
4. **Recovery:** crash at every lifecycle boundary, matching-identity cleanup,
   foreign identity preservation, token replacement by deletion, and transient
   database failures without worker-loop termination.
5. **Deletion:** committed delete intent before side effects, filesystem and
   qBittorrent retry behavior, absent-path/absent-torrent idempotence, and
   identity-mismatch refusal.
6. **Compatibility:** unchanged movie and single-video TV scalar behavior,
   plus pack-only ledger/staging behavior and client handling of active
   `DELETING` snapshots.
