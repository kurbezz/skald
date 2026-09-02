# TV-Pack Lifecycle Fencing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace path-only TV-pack recovery with token-fenced, identity-aware
staging, publication, recovery, and deletion.

**Architecture:** A multi-video TV job is a `pack` operation. It reserves every
target under a fresh operation token, stages privately, publishes without
replacement, and conditionally finalizes only when its token still owns every
published ledger row. `st_dev`/`st_ino` are required before automatic cleanup
or deletion. Movies and one-video TV jobs remain `scalar` jobs and retain their
existing `library_path` flow.

**Tech Stack:** Python 3.12, SQLModel, SQLite, FastAPI, pytest, Node's built-in
test runner.

---

## File structure

- `src/skald/models.py` — lifecycle columns and typed lifecycle values.
- `src/skald/db.py` — additive, conservative SQLite migrations and indexes.
- `src/skald/lifecycle.py` — shared per-job advisory lock and token/identity
  helpers used by worker and routes.
- `src/skald/organizer.py` — private staging and non-overwriting publication.
- `src/skald/worker.py` — fenced pack transitions and organization/deletion
  recovery.
- `src/skald/routes/jobs.py` — fenced delete-request transition and response
  handling.
- `src/skald/static/active_jobs_payload.mjs` — accepts active `deleting`
  snapshots.
- `tests/test_models.py` — model and lifecycle schema assertions.
- `tests/test_organizer.py` — staging, publication, ownership, and identity
  tests.
- `tests/test_worker.py` — fencing, recovery, concurrency, and scalar
  compatibility tests.
- `tests/test_routes.py` — delete-request and UI-facing deletion behavior.
- `tests/active_jobs_payload.test.mjs` — active snapshot contract.

## Phase 1: Schema, conservative migration, and advisory lock primitive

**Files:**

- Modify: `src/skald/models.py`
- Modify: `src/skald/db.py`
- Add: `src/skald/lifecycle.py`
- Modify: `tests/test_models.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: Write failing schema and lock tests**

  Add tests for these exact additions:

  - `MediaJob.organization_mode`, defaulting to `scalar`, and nullable
    `MediaJob.operation_token`.
  - `OrganizedFile.operation_token`, `lifecycle`, `staging_path`,
    `staging_device`, `staging_inode`, `published_device`, and
    `published_inode`.
  - A migration of a legacy database with duplicate path reservations fails
    with an actionable error and leaves every duplicate row intact.
  - A migrated ledger row without identity is `legacy_unverified`; automatic
    ownership adoption is not permitted.
  - Two concurrent callers for one job cannot hold the same advisory lock;
    a caller that misses the lock performs no side effect and can acquire it
    after release.

- [ ] **Step 2: Run RED**

  Run:

  ```bash
  uv run pytest tests/test_models.py tests/test_worker.py -k 'lifecycle_schema or legacy_unverified or advisory_lock' -v
  ```

  Expected: FAIL because the lifecycle columns, migration policy, and shared
  lock primitive do not exist.

- [ ] **Step 3: Implement the additive foundation**

  1. Add `organization_mode` (`scalar` or `pack`) and `operation_token` to
     `MediaJob`.
  2. Add the per-file token, lifecycle, staging path, and both staged/published
     device/inode pairs to `OrganizedFile`.
  3. Make `migrate_schema` additive and idempotent. Validate duplicate path
     reservations before retaining/creating uniqueness; never delete rows to
     make a migration pass.
  4. Backfill old jobs with ledger rows to `pack` and their rows to
     `legacy_unverified`; backfill all other jobs to `scalar`. Leave legacy
     identity and token fields null.
  5. Add a process-local, per-job advisory lock in `lifecycle.py`, including a
     non-blocking acquisition API and `finally`-safe release. Document in code
     that the lock is an interleaving guard, not a crash/process fencing proof.

- [ ] **Step 4: Run GREEN**

  Run:

  ```bash
  uv run pytest tests/test_models.py tests/test_worker.py -k 'lifecycle_schema or legacy_unverified or advisory_lock' -v
  ```

  Expected: PASS. The migration is conservative, legacy rows are not silently
  trusted, and lock contention has no side effect.

- [ ] **Oracle gate: Phase 1**

  Confirm the schema names and nullability match the design; duplicate legacy
  rows are preserved on migration failure; and no correctness claim depends on
  the process-local lock rather than a persisted token.

## Phase 2: Staging, publication, and per-file ownership

**Files:**

- Modify: `src/skald/organizer.py`
- Modify: `src/skald/lifecycle.py`
- Modify: `tests/test_organizer.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: Write failing staging and publication tests**

  Cover a pack mapping through these cases:

  - Staging is private, same-filesystem, and no public target is visible before
    publication.
  - Hardlink staging succeeds normally; only `EXDEV` takes the copy path;
    staging records the staged `(device, inode)` after fsync.
  - A partial copy failure records an attempt-owned staging identity when one
    exists and preserves the primary write error plus any cleanup error.
  - Public publication is exclusive/non-overwriting. A racing public target is
    explicitly foreign, is neither replaced nor cleaned, and is not claimed by
    this attempt.
  - Successful publication records the public `(device, inode)`, removes the
    private staging file, and advances exactly one row to `published`.

- [ ] **Step 2: Run RED**

  Run:

  ```bash
  uv run pytest tests/test_organizer.py tests/test_worker.py -k 'staging or publication or published_identity or foreign_race' -v
  ```

  Expected: FAIL because organization currently writes directly to public
  targets and tracks paths rather than staged/published identities.

- [ ] **Step 3: Implement staging and ownership transitions**

  1. Create a unique private staging path for each reserved ledger row on the
     target filesystem.
  2. Stage with hardlink-first/EXDEV-copy behavior and fsync before recording
     `staged` plus its identity.
  3. Publish with an operation that cannot replace an existing public file
     (for example, an exclusive hardlink from staging), then record public
     identity and transition to `published`.
  4. Carry exact owned, explicitly foreign, and cleanup-failure information in
     structured results/errors. Cleanup compares recorded identities before
     unlinking; it never makes a path-only ownership decision.
  5. Fence each row update by job id, row token, and current job token.

- [ ] **Step 4: Run GREEN**

  Run:

  ```bash
  uv run pytest tests/test_organizer.py tests/test_worker.py -k 'staging or publication or published_identity or foreign_race' -v
  ```

  Expected: PASS. No test can observe a partially written public output, and a
  race target remains unchanged and unowned.

- [ ] **Oracle gate: Phase 2**

  Confirm publication has no replacing rename path, every cleanup action is
  identity-checked, and copy/link failure reports do not lose the primary
  filesystem error.

## Phase 3: Fenced pack lifecycle with scalar separation

**Files:**

- Modify: `src/skald/worker.py`
- Modify: `src/skald/lifecycle.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: Write failing lifecycle/fencing tests**

  Add tests proving that:

  - Complete multi-video preflight happens before reservation; one committed
    transaction then sets `pack`, `ORGANIZING`, a new token, and one `reserved`
    row per mapping before the first staging call.
  - The reservation transaction is visible from a distinct session while the
    staging function is paused.
  - A stale organizer cannot update a row, publish, or finalize after another
    operation replaces the job token.
  - Finalization changes to `ORGANIZED` only when all rows for the current
    token are `published`; a conditional finalize returning zero rows is not
    treated as success.
  - Finalization failure retains only matching owned residual rows and returns
    an actionable state; no stale token can set `library_path`.
  - Movie and one-video TV jobs remain `scalar`, do not create ledger/staging
    records, and preserve their existing metadata-derived output paths.

- [ ] **Step 2: Run RED**

  Run:

  ```bash
  uv run pytest tests/test_worker.py -k 'pack_fence or conditional_finalize or stale_token or scalar_compatibility' -v
  ```

  Expected: FAIL because current lifecycle writes are path-ledger based and are
  not fenced by `operation_token`.

- [ ] **Step 3: Implement fenced organization transitions**

  1. Route only multi-video TV jobs into `pack`; retain the current scalar path
     unchanged for movies and single-video TV jobs.
  2. Under the job lock, preflight then commit a new organization token and all
     `reserved` rows before calling staging.
  3. Use token-conditioned state changes for reserved → staged → published and
     for final `ORGANIZING` → `ORGANIZED`.
  4. Set the pack `library_path` only as a display pointer to the first
     published output during a successful conditional finalize.
  5. Treat lost token ownership, zero-row conditional updates, unknown
     lifecycle values, and finalization errors as stopped operations for
     recovery, never as successful organization.

- [ ] **Step 4: Run GREEN**

  Run:

  ```bash
  uv run pytest tests/test_worker.py -k 'pack_fence or conditional_finalize or stale_token or scalar_compatibility' -v
  ```

  Expected: PASS. A stale attempt cannot advance the current operation, while
  scalar behavior remains unchanged.

- [ ] **Oracle gate: Phase 3**

  Confirm every write after reservation is token-conditioned, the job token is
  checked at finalization, and `library_path` is not a pack deletion authority.

## Phase 4: Fenced deletion and recovery

**Files:**

- Modify: `src/skald/lifecycle.py`
- Modify: `src/skald/worker.py`
- Modify: `src/skald/routes/jobs.py`
- Modify: `src/skald/static/active_jobs_payload.mjs`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_routes.py`
- Modify: `tests/active_jobs_payload.test.mjs`

- [ ] **Step 1: Write failing delete/recovery tests**

  Add tests for:

  - Delete request commits `DELETING`, a fresh delete token, and
    `delete_requested` rows before its first filesystem or qBittorrent call.
  - A matching published identity is deleted; a missing public path is clean;
    a present identity mismatch or `legacy_unverified` row is retained and
    becomes actionable `NEEDS_ATTENTION` without deleting the foreign path.
  - qBittorrent failure after local cleanup retains `DELETING`, the delete
    token, and all ledger rows; an already-absent torrent is success.
  - Recovery after crashes at `reserved`, `staged`, `published`, and
    `delete_requested` cleans only matching identities, honors the current
    token, and never lets an old organizer publish after deletion starts.
  - Recovery database/discovery errors roll back and retry on the next worker
    interval; generic polling never converts `ORGANIZING` or `DELETING` to
    `FAILED`.
  - The active-job payload accepts server-emitted `deleting` status without
    changing the UI's existing rendering behavior.
  - Scalar delete still commits durable delete intent, removes scalar
    `library_path`, then deletes qBittorrent content without pack identity
    fallback.

- [ ] **Step 2: Run RED**

  Run:

  ```bash
  uv run pytest tests/test_routes.py tests/test_worker.py -k 'fenced_delete or delete_identity or deleting_recovery or recovery_token or scalar_delete' -v
  node --test tests/active_jobs_payload.test.mjs
  ```

  Expected: FAIL because current deletion/recovery is path-based rather than
  token- and identity-fenced.

- [ ] **Step 3: Implement delete and recovery protocol**

  1. Acquire the shared job lock and commit the delete token/state before any
     side effect.
  2. For pack files, remove only a matching staged or published identity; keep
     mismatches and `legacy_unverified` rows for operator action.
  3. Retry qBittorrent only after local paths are clean. Preserve `DELETING`
     and its ledger on failure; delete rows and job only after both layers
     succeed.
  4. Reconcile `ORGANIZING` and `DELETING` jobs under the same lock and current
     token. A token mismatch or failed conditional write stops mutation and
     preserves an actionable audit state.
  5. Keep recovery discovery/reconciliation failures isolated so worker loop
     continues after rollback and the poll interval.
  6. Add `deleting` to the client active-status allowlist only; do not alter
     snapshot shape or rendering semantics.

- [ ] **Step 4: Run GREEN**

  Run:

  ```bash
  uv run pytest tests/test_routes.py tests/test_worker.py -k 'fenced_delete or delete_identity or deleting_recovery or recovery_token or scalar_delete' -v
  node --test tests/active_jobs_payload.test.mjs
  ```

  Expected: PASS. Foreign files are never deleted, stale organizers are fenced
  out, durable deletion is resumable, and scalar deletion remains compatible.

- [ ] **Oracle gate: Phase 4**

  Confirm deletion takes precedence over organization by token replacement,
  no pack cleanup uses path existence alone, and recovery cannot turn durable
  recovery states into `FAILED`.

## Phase 5: Full validation and migration review

**Files:**

- Verify only: `src/skald/models.py`, `src/skald/db.py`,
  `src/skald/lifecycle.py`, `src/skald/organizer.py`, `src/skald/worker.py`,
  `src/skald/routes/jobs.py`, `src/skald/static/active_jobs_payload.mjs`, and
  their tests.

- [ ] **Step 1: Run the complete suite**

  Run:

  ```bash
  uv run pytest -q
  node --test tests/active_jobs_payload.test.mjs
  ```

  Expected: PASS. Existing non-pack, scalar, route, and client behavior remain
  green alongside lifecycle tests.

- [ ] **Step 2: Inspect migration and state-transition coverage**

  Run:

  ```bash
  git diff --check
  git diff -- src/skald/models.py src/skald/db.py src/skald/lifecycle.py src/skald/organizer.py src/skald/worker.py src/skald/routes/jobs.py src/skald/static/active_jobs_payload.mjs tests/test_models.py tests/test_organizer.py tests/test_worker.py tests/test_routes.py tests/active_jobs_payload.test.mjs
  ```

  Expected: No whitespace errors, no unrelated files, no destructive legacy
  migration, and every automatic destructive action is both token-fenced and
  identity-checked.

- [ ] **Oracle gate: Phase 5**

  Verify the implementation covers every approved design section: schema,
  migration, per-job locking, staging, non-overwrite publication, conditional
  finalization, delete intent, recovery ownership, scalar separation, and all
  stated failure/idempotence paths.
